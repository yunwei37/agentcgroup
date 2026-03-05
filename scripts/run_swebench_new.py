#!/usr/bin/env python3
"""
Unified SWE-bench + eBPF runner.

Compared with run_swebench.py, this script orchestrates container + tracer in
an order that guarantees cgroup hard filtering:

1) Start container first (idle command: sleep infinity)
2) Auto-resolve container cgroup path
3) Start process_new with --cgroup-filter (and optional subtree matching)
4) Wait until tracer emits CLOCK_SYNC start anchor
5) Run Claude workload via podman exec inside the same container
"""

import argparse
import json
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from run_swebench import SWEBenchRunner, ResourceMonitor, WORKFLOW_PROMPT


DEFAULT_TRACE_COMMANDS = "python,node,bash,sh,pip,pytest,git,claude"
TRACE_READY_NEEDLE = b'"event":"CLOCK_SYNC","phase":"start"'


def _now() -> str:
    return datetime.now().isoformat()


def _default_task_name(image: str) -> str:
    safe = image.split("/")[-1].replace(":", "_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{safe}_{ts}"


def _run_checked(cmd: List[str], error_prefix: str) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or f"exit code {result.returncode}"
        raise RuntimeError(f"{error_prefix}: {detail}")
    return result


def _build_container_cmd(
    runner: SWEBenchRunner,
    memory: str,
    cpus: str,
    enable_wrapper: bool,
) -> List[str]:
    cmd = [
        "podman",
        "run",
        "-d",
        "--userns=keep-id",
        "--network=host",
        "-v",
        "/usr:/usr:ro",
        "-v",
        "/lib:/lib:ro",
        "-v",
        "/lib64:/lib64:ro",
        "-v",
        "/bin:/bin:ro",
        "-v",
        "/sbin:/sbin:ro",
        "-v",
        "/home:/home",
        "-v",
        "/tmp:/tmp",
        "-v",
        "/var:/var",
        "-w",
        "/testbed",
        "-e",
        f"HOME={runner.home}",
        "-e",
        f"PATH={runner.home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
    ]

    if enable_wrapper:
        wrapper_path = (
            Path(__file__).resolve().parent.parent / "agentcg" / "bash_wrapper.sh"
        )
        if wrapper_path.exists():
            cmd.extend(["-v", f"{wrapper_path}:/tmp/agentcg/bash_wrapper.sh:ro"])

    if memory:
        cmd.append(f"--memory={memory}")
    if cpus:
        cmd.append(f"--cpus={cpus}")

    cmd.extend([runner.fixed_image_name, "sleep", "infinity"])
    return cmd


def _get_container_cgroup_path(container_id: str) -> str:
    result = _run_checked(
        ["podman", "inspect", "--format", "{{.State.CgroupPath}}", container_id],
        "Failed to inspect container cgroup path",
    )
    cgroup_path = result.stdout.strip()
    if not cgroup_path:
        raise RuntimeError("Container cgroup path is empty")
    return cgroup_path


def _build_trace_cmd(
    args: argparse.Namespace, effective_cgroup_filter: Optional[str]
) -> List[str]:
    cmd = [
        "sudo",
        args.trace_bin,
        "-m",
        str(args.trace_mode),
        "-c",
        args.trace_commands,
    ]

    if args.trace_all:
        cmd.append("--trace-all")
    else:
        any_flag = False
        if args.trace_fs:
            cmd.append("--trace-fs")
            any_flag = True
        if args.trace_net:
            cmd.append("--trace-net")
            any_flag = True
        if args.trace_signals:
            cmd.append("--trace-signals")
            any_flag = True
        if args.trace_mem:
            cmd.append("--trace-mem")
            any_flag = True
        if args.trace_cow:
            cmd.append("--trace-cow")
            any_flag = True
        if not any_flag:
            cmd.append("--trace-all")

    if args.trace_resources:
        cmd.append("--trace-resources")
    if args.resource_detail:
        cmd.append("--resource-detail")
    if args.sample_interval is not None:
        cmd.extend(["--sample-interval", str(args.sample_interval)])

    if effective_cgroup_filter:
        cmd.extend(["--cgroup-filter", effective_cgroup_filter])
        if args.trace_cgroup_children:
            cmd.append("--cgroup-filter-children")

    return cmd


def _start_tracer(
    args: argparse.Namespace,
    task_dir: Path,
    effective_cgroup_filter: Optional[str],
) -> Tuple[subprocess.Popen, List[str], str, Path]:
    trace_out = task_dir / "ebpf_trace.jsonl"
    trace_err = task_dir / "ebpf_trace.stderr"
    trace_pid = task_dir / "ebpf_trace.pid"

    cmd = _build_trace_cmd(args, effective_cgroup_filter)
    out_f = open(trace_out, "w")
    err_f = open(trace_err, "w")

    try:
        proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f)
    except Exception:
        out_f.close()
        err_f.close()
        raise

    out_f.close()
    err_f.close()
    trace_pid.write_text(f"{proc.pid}\n")
    return proc, cmd, str(trace_pid), trace_out


def _wait_tracer_ready(proc: subprocess.Popen, trace_out: Path, timeout_sec: float) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"process_new exited early with code {proc.returncode}; see ebpf_trace.stderr"
            )

        if trace_out.exists() and trace_out.stat().st_size > 0:
            try:
                data = trace_out.read_bytes()
                if TRACE_READY_NEEDLE in data:
                    return
            except OSError:
                pass

        time.sleep(0.1)

    raise TimeoutError(
        f"Tracer did not become ready within {timeout_sec:.1f}s (missing CLOCK_SYNC start anchor)"
    )


def _stop_tracer(proc: subprocess.Popen, timeout_sec: int = 8) -> Dict[str, object]:
    info: Dict[str, object] = {
        "pid": proc.pid,
        "stop_method": None,
        "exit_code": None,
        "timeout": False,
    }

    if proc.poll() is not None:
        info["stop_method"] = "already_exited"
        info["exit_code"] = proc.returncode
        return info

    try:
        proc.send_signal(signal.SIGINT)
        info["stop_method"] = "sigint"
        proc.wait(timeout=timeout_sec)
        info["exit_code"] = proc.returncode
        return info
    except subprocess.TimeoutExpired:
        info["timeout"] = True

    try:
        proc.terminate()
        info["stop_method"] = "sigterm"
        proc.wait(timeout=3)
        info["exit_code"] = proc.returncode
        return info
    except subprocess.TimeoutExpired:
        pass

    proc.kill()
    info["stop_method"] = "sigkill"
    info["exit_code"] = proc.wait(timeout=3)
    return info


def _build_workload_script(runner: SWEBenchRunner, args: argparse.Namespace) -> str:
    prompt = args.prompt if args.prompt is not None else WORKFLOW_PROMPT

    wrapper_setup = ""
    if args.enable_wrapper:
        wrapper_setup = """
# Install AgentCgroup bash wrapper for per-tool-call resource tracking
if [ -f /tmp/agentcg/bash_wrapper.sh ]; then
    cp /usr/bin/bash /usr/bin/real-bash 2>/dev/null || true
    cp /tmp/agentcg/bash_wrapper.sh /usr/bin/bash 2>/dev/null || true
    chmod +x /usr/bin/bash 2>/dev/null || true
    export AGENTCG_LOG="/tmp/agentcg_tools.jsonl"
    echo "[AgentCgroup] Bash wrapper installed for per-tool-call tracking"
fi
"""

    return f"""{wrapper_setup}
git config user.email "test@test.com"
git config user.name "Test"
git config --add safe.directory /testbed

if [ -x "$HOME/.local/bin/claude" ]; then
    CLAUDE_BIN="$HOME/.local/bin/claude"
else
    CLAUDE_BIN="$(command -v claude)"
fi
echo "[Runner] Claude binary: $CLAUDE_BIN"
"$CLAUDE_BIN" --model {args.model} --print --dangerously-skip-permissions "{prompt}"

echo "=== GIT DIFF ==="
git diff

echo "=== DISK USAGE ==="
du -sm /testbed 2>/dev/null || echo "N/A"

echo "=== TOOL CALL LOG ==="
cat /tmp/agentcg_tools.jsonl 2>/dev/null || echo "No tool call log"
"""


def _run_workload_in_container(
    runner: SWEBenchRunner, args: argparse.Namespace
) -> Tuple[Dict[str, object], Dict[str, object]]:
    script = _build_workload_script(runner, args)
    monitor = ResourceMonitor(runner.container_id, interval=1.0)
    monitor.start()

    try:
        result = subprocess.run(
            ["podman", "exec", runner.container_id, "bash", "-lc", script],
            capture_output=True,
            text=True,
        )
    finally:
        monitor.stop()

    if runner.output_dir:
        with open(runner.output_dir / "claude_output.txt", "w") as f:
            f.write(result.stdout)
        if result.stderr:
            with open(runner.output_dir / "claude_stderr.txt", "w") as f:
                f.write(result.stderr)

    resource_data = {"samples": monitor.samples, "summary": monitor.get_summary()}
    if runner.output_dir:
        with open(runner.output_dir / "resources.json", "w") as f:
            json.dump(resource_data, f, indent=2)

    summary = resource_data.get("summary", {})
    print(f"  Collected {len(monitor.samples)} resource samples")
    if "memory_mb" in summary and "cpu_percent" in summary:
        print(
            f"  Memory: avg={summary['memory_mb']['avg']:.1f}MB, max={summary['memory_mb']['max']:.1f}MB"
        )
        print(
            f"  CPU: avg={summary['cpu_percent']['avg']:.1f}%, max={summary['cpu_percent']['max']:.1f}%"
        )

    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }, resource_data


def _save_results(output_file: Path, results: Dict[str, object]) -> None:
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run SWE-bench task with host eBPF tracing (process_new)."
    )
    parser.add_argument("image", help="Docker image, e.g. swerebench/sweb.eval.x86_64....")
    parser.add_argument("--task-name", help="Output task folder name")
    parser.add_argument(
        "--output-root",
        default="experiments/branchfs_motivation",
        help="Root folder for task outputs",
    )

    # SWE-bench workload options.
    parser.add_argument("--prompt", help="Custom prompt")
    parser.add_argument("--run-tests", action="store_true", help="Retained for compatibility")
    parser.add_argument("--memory", default="4g", help="Container memory limit")
    parser.add_argument("--cpus", default="2", help="Container CPU limit")
    parser.add_argument("--model", default="haiku", help="Model to use (default: haiku)")
    parser.add_argument("--enable-wrapper", action="store_true", help="Enable bash wrapper")

    # process_new options.
    parser.add_argument(
        "--trace-bin",
        default="./agentsight/bpf/process_new",
        help="Path to process_new binary",
    )
    parser.add_argument("--trace-mode", type=int, default=2, help="process_new mode (default: 2)")
    parser.add_argument(
        "--trace-commands",
        default=DEFAULT_TRACE_COMMANDS,
        help="Comma-separated commands for -c filter",
    )
    parser.add_argument("--trace-all", action="store_true", help="Enable --trace-all")
    parser.add_argument("--trace-fs", action="store_true", help="Enable --trace-fs")
    parser.add_argument("--trace-net", action="store_true", help="Enable --trace-net")
    parser.add_argument("--trace-signals", action="store_true", help="Enable --trace-signals")
    parser.add_argument("--trace-mem", action="store_true", help="Enable --trace-mem")
    parser.add_argument("--trace-cow", action="store_true", help="Enable --trace-cow")
    parser.add_argument("--trace-resources", action="store_true", help="Enable --trace-resources")
    parser.add_argument("--resource-detail", action="store_true", help="Enable --resource-detail")
    parser.add_argument("--sample-interval", type=int, help="Resource sample interval (ms)")

    # cgroup filtering control.
    parser.add_argument(
        "--trace-cgroup-filter",
        help="Manual cgroup filter path passed to process_new --cgroup-filter",
    )
    parser.add_argument(
        "--trace-cgroup-children",
        action="store_true",
        help="Include descendants of the selected cgroup filter path",
    )
    parser.add_argument(
        "--no-trace-cgroup-auto",
        action="store_true",
        help="Disable auto detection of container cgroup path",
    )
    parser.add_argument(
        "--trace-ready-timeout",
        type=float,
        default=8.0,
        help="Seconds to wait for tracer CLOCK_SYNC start anchor before workload",
    )

    args = parser.parse_args()

    start_time = time.time()
    task_name = args.task_name or _default_task_name(args.image)
    task_dir = Path(args.output_root).expanduser().resolve() / task_name
    swebench_dir = task_dir / "swebench"
    task_dir.mkdir(parents=True, exist_ok=True)
    swebench_dir.mkdir(parents=True, exist_ok=True)

    auto_cgroup = not args.no_trace_cgroup_auto
    effective_cgroup_filter: Optional[str] = None
    cgroup_filter_source: Optional[str] = None

    manifest: Dict[str, object] = {
        "start_time": _now(),
        "image": args.image,
        "model": args.model,
        "task_name": task_name,
        "output_root": args.output_root,
        "trace_bin": args.trace_bin,
        "trace_mode": args.trace_mode,
        "trace_commands": args.trace_commands,
        "argv": sys.argv,
        "container_id": None,
        "container_cgroup_path": None,
        "trace_cgroup_filter_effective": None,
        "trace_cgroup_filter_source": None,
        "trace_cgroup_children": args.trace_cgroup_children,
        "trace_ready_timeout": args.trace_ready_timeout,
        "trace_ready_elapsed_s": None,
        "trace_cmd": None,
        "trace_pid": None,
        "trace_pid_file": None,
        "trace_stop": None,
        "swebench": None,
        "error": None,
    }

    runner = SWEBenchRunner(
        image_name=args.image,
        memory_limit=args.memory,
        cpu_limit=args.cpus,
        output_dir=swebench_dir,
        enable_wrapper=args.enable_wrapper,
    )

    proc: Optional[subprocess.Popen] = None
    swebench_results: Dict[str, object] = {
        "image": args.image,
        "start_time": _now(),
        "memory_limit": args.memory,
        "cpu_limit": args.cpus,
        "model": args.model,
        "model_requested": args.model,
        "output_dir": str(swebench_dir),
    }

    try:
        if not Path(args.trace_bin).exists():
            raise FileNotFoundError(f"trace binary not found: {args.trace_bin}")

        print(f"[1/8] Pulling image: {args.image}")
        t0 = time.time()
        runner._pull_image()
        swebench_results["pull_time"] = time.time() - t0

        print("[2/8] Fixing /testbed permissions...")
        t0 = time.time()
        runner._fix_permissions()
        swebench_results["permission_fix_time"] = time.time() - t0

        print("[3/8] Collecting image and disk info...")
        swebench_results["image_info"] = runner._get_image_info()
        print(f"  Image size: {swebench_results['image_info'].get('size_mb', 'N/A')} MB")

        print("[4/8] Starting idle container...")
        container_cmd = _build_container_cmd(runner, args.memory, args.cpus, args.enable_wrapper)
        result = _run_checked(container_cmd, "Failed to start idle container")
        runner.container_id = result.stdout.strip()
        manifest["container_id"] = runner.container_id
        print(f"  Container started: {runner.container_id[:12]}")

        container_cgroup = _get_container_cgroup_path(runner.container_id)
        manifest["container_cgroup_path"] = container_cgroup
        (task_dir / "container.id").write_text(f"{runner.container_id}\n")
        (task_dir / "container_cgroup_path.txt").write_text(f"{container_cgroup}\n")

        if args.trace_cgroup_filter:
            effective_cgroup_filter = args.trace_cgroup_filter
            cgroup_filter_source = "cli"
        elif auto_cgroup:
            effective_cgroup_filter = container_cgroup
            cgroup_filter_source = "auto"

        if args.trace_cgroup_children and not effective_cgroup_filter:
            raise RuntimeError(
                "--trace-cgroup-children requires a cgroup filter "
                "(set --trace-cgroup-filter or keep auto mode enabled)"
            )

        manifest["trace_cgroup_filter_effective"] = effective_cgroup_filter
        manifest["trace_cgroup_filter_source"] = cgroup_filter_source

        print("[5/8] Starting process_new tracer...")
        proc, trace_cmd, trace_pid_file, trace_out = _start_tracer(
            args, task_dir, effective_cgroup_filter
        )
        manifest["trace_cmd"] = trace_cmd
        manifest["trace_pid"] = proc.pid
        manifest["trace_pid_file"] = trace_pid_file

        ready_t0 = time.time()
        _wait_tracer_ready(proc, trace_out, args.trace_ready_timeout)
        manifest["trace_ready_elapsed_s"] = time.time() - ready_t0
        print(f"  Tracer ready in {manifest['trace_ready_elapsed_s']:.2f}s")

        print(f"[6/8] Running Claude Code ({args.model})...")
        t0 = time.time()
        claude_output, resource_samples = _run_workload_in_container(runner, args)
        swebench_results["claude_time"] = time.time() - t0
        swebench_results["model_actual"] = args.model
        swebench_results["claude_output"] = claude_output
        swebench_results["resource_samples"] = resource_samples
        swebench_results["disk_usage"] = runner._parse_disk_usage(claude_output.get("stdout", ""))
        print(f"  Disk usage (/testbed): {swebench_results['disk_usage'].get('testbed_mb', 'N/A')} MB")

        print("[7/8] Collecting trace logs...")
        traces = runner._collect_traces()
        swebench_results["traces"] = traces

        manifest["swebench"] = {
            "output_dir": str(swebench_dir),
            "error": swebench_results.get("error"),
            "total_time": None,
            "tool_calls": len(traces.get("tool_calls", [])),
            "claude_exit_code": claude_output.get("exit_code"),
        }

    except Exception as e:
        manifest["error"] = str(e)
        swebench_results["error"] = str(e)
        print(f"[run_swebench_new] Error: {e}", file=sys.stderr)
    finally:
        print("[8/8] Cleaning up...")
        if proc is not None:
            manifest["trace_stop"] = _stop_tracer(proc)

        runner._cleanup()

        swebench_results["total_time"] = time.time() - start_time
        swebench_results["end_time"] = _now()
        if manifest.get("swebench") is not None:
            manifest["swebench"]["total_time"] = swebench_results["total_time"]
        elif "traces" in swebench_results:
            manifest["swebench"] = {
                "output_dir": str(swebench_dir),
                "error": swebench_results.get("error"),
                "total_time": swebench_results["total_time"],
                "tool_calls": len(swebench_results.get("traces", {}).get("tool_calls", [])),
                "claude_exit_code": swebench_results.get("claude_output", {}).get("exit_code"),
            }

        _save_results(swebench_dir / "results.json", swebench_results)

        manifest["end_time"] = _now()
        manifest["total_time"] = time.time() - start_time
        manifest_path = task_dir / "run_manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        print("=" * 60)
        print("run_swebench_new summary")
        print("=" * 60)
        print(f"Task dir: {task_dir}")
        print(f"Manifest: {manifest_path}")
        if manifest.get("trace_stop"):
            print(f"Tracer stop: {manifest['trace_stop']}")
        if manifest.get("swebench"):
            print(f"SWE-bench: {manifest['swebench']}")
        if manifest.get("error"):
            print(f"Error: {manifest['error']}")

    return 1 if manifest.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
