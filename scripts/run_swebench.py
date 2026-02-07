#!/usr/bin/env python3
"""
SWE-bench Runner with Resource Monitoring

Downloads a SWE-bench Docker image, runs Claude Code haiku to solve the issue,
monitors resource usage (memory/CPU) every second, and collects traces.

Usage:
    python run_swebench.py <image_name> [--prompt "custom prompt"]
    python run_swebench.py swerebench/sweb.eval.x86_64.encode_1776_starlette-1147
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict


# Complete workflow prompt
WORKFLOW_PROMPT = '''Fix this issue: $(cat /issue.md)

IMPORTANT: You must complete the FULL workflow:
1. Read and understand the issue thoroughly
2. Explore the codebase to find relevant files
3. Implement the fix
4. Run the test suite to verify your fix
5. If ANY test fails, analyze the error and fix it
6. Repeat steps 4-5 until ALL tests pass
7. Only stop when tests are passing

DO NOT stop until you have:
- Made code changes that fix the issue
- Run the tests and confirmed they pass
- Shown the final git diff

If you encounter test failures, debug and fix them. Keep trying until successful.

CRITICAL REQUIREMENTS FOR TESTING:
- You MUST run the project's ORIGINAL test suite (pytest, unittest, tox, etc.)
- Do NOT write custom test scripts or verification scripts to bypass tests
- Do NOT claim success based on your own "All checks passed" output
- The test output MUST show real pytest format: "X passed, Y failed in Z seconds"
- If tests fail with ImportError or collection errors, fix the environment/import issue first
- Success means the project's actual test suite passes, not custom verification

WHAT COUNTS AS SUCCESS:
- Real pytest/unittest output showing tests passed
- Example: "===== 150 passed, 0 failed in 10.5s ====="

WHAT DOES NOT COUNT:
- Your own verification scripts saying "All checks passed"
- Manual testing or print statements
- Skipping tests due to import errors

In the output, you need to summary your change and 
summary how your test the application to check the fix,
and what's the test status.
'''

class ResourceMonitor:
    """Monitor container resource usage in a background thread."""

    def __init__(self, container_id: str, interval: float = 1.0):
        self.container_id = container_id
        self.interval = interval
        self.samples = []
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start monitoring in background thread."""
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop monitoring and wait for thread to finish."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _monitor_loop(self):
        """Main monitoring loop."""
        while not self._stop_event.is_set():
            sample = self._get_sample()
            if sample:
                self.samples.append(sample)
            time.sleep(self.interval)

    def _get_sample(self) -> Optional[dict]:
        """Get a single resource sample."""
        try:
            result = subprocess.run(
                ["podman", "stats", "--no-stream", "--format",
                 "{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}", self.container_id],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split('\t')
                if len(parts) >= 3:
                    return {
                        "timestamp": datetime.now().isoformat(),
                        "epoch": time.time(),
                        "mem_usage": parts[0],
                        "mem_percent": parts[1],
                        "cpu_percent": parts[2]
                    }
        except Exception as e:
            pass  # Container may have stopped
        return None

    def get_summary(self) -> dict:
        """Get summary statistics."""
        if not self.samples:
            return {"error": "No samples collected"}

        # Parse memory values (e.g., "123.4MiB / 4GiB")
        mem_values = []
        cpu_values = []

        for s in self.samples:
            try:
                mem_str = s["mem_usage"].split("/")[0].strip()
                mem_mb = self._parse_memory(mem_str)
                if mem_mb:
                    mem_values.append(mem_mb)

                cpu_str = s["cpu_percent"].replace("%", "").strip()
                cpu_values.append(float(cpu_str))
            except:
                pass

        return {
            "sample_count": len(self.samples),
            "duration_seconds": self.samples[-1]["epoch"] - self.samples[0]["epoch"] if len(self.samples) > 1 else 0,
            "memory_mb": {
                "min": min(mem_values) if mem_values else 0,
                "max": max(mem_values) if mem_values else 0,
                "avg": sum(mem_values) / len(mem_values) if mem_values else 0,
            },
            "cpu_percent": {
                "min": min(cpu_values) if cpu_values else 0,
                "max": max(cpu_values) if cpu_values else 0,
                "avg": sum(cpu_values) / len(cpu_values) if cpu_values else 0,
            }
        }

    def _parse_memory(self, mem_str: str) -> Optional[float]:
        """Parse memory string to MB."""
        mem_str = mem_str.strip()
        try:
            # Binary units (MiB, GiB, KiB)
            if mem_str.endswith("GiB"):
                return float(mem_str[:-3]) * 1024
            elif mem_str.endswith("MiB"):
                return float(mem_str[:-3])
            elif mem_str.endswith("KiB"):
                return float(mem_str[:-3]) / 1024
            # Decimal units (MB, GB, KB) - used by podman stats
            elif mem_str.endswith("GB"):
                return float(mem_str[:-2]) * 1000
            elif mem_str.endswith("MB"):
                return float(mem_str[:-2])
            elif mem_str.endswith("KB"):
                return float(mem_str[:-2]) / 1000
            elif mem_str.endswith("kB"):
                return float(mem_str[:-2]) / 1000
            elif mem_str.endswith("B"):
                return float(mem_str[:-1]) / (1024 * 1024)
        except:
            pass
        return None


class SWEBenchRunner:
    """Run Claude Code on SWE-bench tasks with monitoring."""

    def __init__(self, image_name: str, memory_limit: Optional[str] = "4g", cpu_limit: Optional[str] = "2",
                 output_dir: Optional[Path] = None):
        self.image_name = image_name
        self.memory_limit = memory_limit  # None means no limit
        self.cpu_limit = cpu_limit  # None means no limit
        self.home = os.environ.get("HOME", f"/home/{os.environ.get('USER', 'user')}")
        self.fixed_image_name: Optional[str] = None
        self.container_id: Optional[str] = None
        self.output_dir: Optional[Path] = output_dir

    def run(self, prompt: Optional[str] = None, run_tests: bool = False, model: str = "haiku",
              extra_env: Optional[Dict[str, str]] = None) -> dict:
        """Run the complete workflow."""
        start_time = time.time()
        results = {
            "image": self.image_name,
            "start_time": datetime.now().isoformat(),
            "memory_limit": self.memory_limit,
            "cpu_limit": self.cpu_limit,
            "model": model,
        }

        # Set environment variables for local model
        if extra_env:
            for key, value in extra_env.items():
                os.environ[key] = value

        try:
            # Step 1: Pull image
            print(f"[1/7] Pulling image: {self.image_name}")
            self._pull_image()
            results["pull_time"] = time.time() - start_time

            # Step 2: Fix permissions (create modified image)
            print(f"[2/7] Fixing /testbed permissions...")
            step_start = time.time()
            self._fix_permissions()
            results["permission_fix_time"] = time.time() - step_start

            # Step 3: Collect image info
            print(f"[3/7] Collecting image and disk info...")
            results["image_info"] = self._get_image_info()
            print(f"  Image size: {results['image_info'].get('size_mb', 'N/A')} MB")

            # Step 4: Prepare output directory
            print(f"[4/7] Preparing output directory...")
            if self.output_dir is None:
                self.output_dir = self._prepare_output_dir()
            results["output_dir"] = str(self.output_dir)

            # Step 4: Run Claude Code with monitoring
            print(f"[4/6] Running Claude Code ({model}) with resource monitoring...")
            step_start = time.time()
            claude_result, resource_samples = self._run_claude_with_monitoring(prompt, run_tests, model, extra_env)
            results["claude_time"] = time.time() - step_start
            results["claude_output"] = claude_result
            results["resource_samples"] = resource_samples

            # Parse disk usage from output (collected in cmd_script)
            results["disk_usage"] = self._parse_disk_usage(claude_result.get("stdout", ""))
            print(f"  Disk usage (/testbed): {results['disk_usage'].get('testbed_mb', 'N/A')} MB")

            # Step 6: Copy trace logs
            print(f"[6/7] Collecting trace logs...")
            traces = self._collect_traces()
            results["traces"] = traces

            # Step 7: Cleanup
            print(f"[7/7] Cleaning up...")
            self._cleanup()
            results["cleaned"] = True

        except Exception as e:
            results["error"] = str(e)
            print(f"Error: {e}")
            self._cleanup()

        results["total_time"] = time.time() - start_time
        results["end_time"] = datetime.now().isoformat()

        # Save results
        if self.output_dir:
            results_file = self.output_dir / "results.json"
            with open(results_file, "w") as f:
                json.dump(results, f, indent=2)
            print(f"\nResults saved to: {results_file}")

        return results

    def _get_image_info(self) -> dict:
        """Get Docker image size and info."""
        info = {}
        try:
            result = subprocess.run(
                ["podman", "image", "inspect", self.fixed_image_name, "--format", "{{.Size}}"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                size_bytes = int(result.stdout.strip())
                info["size_bytes"] = size_bytes
                info["size_mb"] = round(size_bytes / (1024 * 1024), 2)

            result = subprocess.run(
                ["podman", "image", "inspect", self.fixed_image_name, "--format", "{{.Id}}"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                info["image_id"] = result.stdout.strip()[:12]
        except Exception as e:
            info["error"] = str(e)
        return info

    def _parse_disk_usage(self, stdout: str) -> dict:
        """Parse disk usage from container output."""
        usage = {}
        try:
            # Look for "=== DISK USAGE ===" section
            if "=== DISK USAGE ===" in stdout:
                lines = stdout.split("=== DISK USAGE ===")[1].strip().split('\n')
                if lines and lines[0].strip() != "N/A":
                    # du -sm output: "SIZE /testbed"
                    parts = lines[0].strip().split()
                    if parts and parts[0].isdigit():
                        usage["testbed_mb"] = int(parts[0])
        except Exception as e:
            usage["error"] = str(e)
        return usage

    def _pull_image(self):
        """Pull the Docker image."""
        result = subprocess.run(
            ["podman", "pull", f"docker.io/{self.image_name}"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to pull image: {result.stderr}")

    def _fix_permissions(self):
        """Create a modified image with fixed /testbed permissions."""
        uid = os.getuid()
        gid = os.getgid()

        # Create a unique name for the fixed image
        safe_name = self.image_name.replace("/", "_").replace(":", "_")
        self.fixed_image_name = f"swebench-fixed-{safe_name}"

        # Check if fixed image already exists
        result = subprocess.run(
            ["podman", "image", "exists", self.fixed_image_name],
            capture_output=True
        )
        if result.returncode == 0:
            print(f"  Using existing fixed image: {self.fixed_image_name}")
            return

        # Create temp container
        result = subprocess.run(
            ["podman", "run", "-d", f"docker.io/{self.image_name}", "sleep", "120"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create temp container: {result.stderr}")

        temp_container = result.stdout.strip()

        try:
            # Fix permissions
            subprocess.run(
                ["podman", "exec", temp_container, "chown", "-R", f"{uid}:{gid}", "/testbed"],
                check=True, capture_output=True
            )

            # Commit as new image
            subprocess.run(
                ["podman", "commit", temp_container, self.fixed_image_name],
                check=True, capture_output=True
            )
            print(f"  Created fixed image: {self.fixed_image_name}")
        finally:
            # Cleanup temp container
            subprocess.run(["podman", "stop", temp_container], capture_output=True)
            subprocess.run(["podman", "rm", temp_container], capture_output=True)

    def _prepare_output_dir(self) -> Path:
        """Create output directory for this run."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = self.image_name.split("/")[-1].replace(":", "_")
        output_dir = Path(self.home) / "agentcgroup" / "experiments" / f"{safe_name}_{timestamp}"
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _run_claude_with_monitoring(self, prompt: Optional[str], run_tests: bool, model: str,
                                        extra_env: Optional[Dict[str, str]] = None) -> tuple:
        """Run Claude Code and monitor resources."""

        # Build the prompt
        if prompt is None:
            prompt = WORKFLOW_PROMPT

        # Build the command
        cmd_script = f'''
git config user.email "test@test.com"
git config user.name "Test"
git config --add safe.directory /testbed

claude --model {model} --print --dangerously-skip-permissions "{prompt}"

echo "=== GIT DIFF ==="
git diff

echo "=== DISK USAGE ==="
du -sm /testbed 2>/dev/null || echo "N/A"
'''

        # Start container
        # Mount host system for claude binary, libs, SSL certs
        # Note: Don't mount /etc (conflicts with podman's resolv.conf)
        container_cmd = [
            "podman", "run", "-d",
            "--userns=keep-id",
            "--network=host",
            "-v", "/usr:/usr:ro",      # claude binary, SSL certs, system tools
            "-v", "/lib:/lib:ro",      # system libraries
            "-v", "/lib64:/lib64:ro",  # 64-bit libraries
            "-v", "/bin:/bin:ro",      # basic commands
            "-v", "/sbin:/sbin:ro",    # system commands
            "-v", "/home:/home",       # home dir for ~/.claude config
            "-v", "/tmp:/tmp",         # temp files
            "-v", "/var:/var",         # var data
            "-w", "/testbed",
            "-e", f"HOME={self.home}",
            "-e", "PATH=/usr/local/bin:/usr/bin:/bin",
        ]

        # Add extra environment variables
        if extra_env:
            for key, value in extra_env.items():
                container_cmd.extend(["-e", f"{key}={value}"])
        # Add resource limits only if specified
        if self.memory_limit:
            container_cmd.extend([f"--memory={self.memory_limit}"])
        if self.cpu_limit:
            container_cmd.extend([f"--cpus={self.cpu_limit}"])
        container_cmd.extend([
            self.fixed_image_name,
            "bash", "-c", cmd_script
        ])

        result = subprocess.run(container_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to start container: {result.stderr}")

        self.container_id = result.stdout.strip()
        print(f"  Container started: {self.container_id[:12]}")

        # Start resource monitoring
        monitor = ResourceMonitor(self.container_id, interval=1.0)
        monitor.start()

        # Wait for container to finish
        print("  Waiting for Claude Code to complete...")
        wait_result = subprocess.run(
            ["podman", "wait", self.container_id],
            capture_output=True, text=True
        )

        # Stop monitoring
        monitor.stop()

        # Get container logs
        logs_result = subprocess.run(
            ["podman", "logs", self.container_id],
            capture_output=True, text=True
        )

        # Save raw output
        if self.output_dir:
            with open(self.output_dir / "claude_output.txt", "w") as f:
                f.write(logs_result.stdout)
            if logs_result.stderr:
                with open(self.output_dir / "claude_stderr.txt", "w") as f:
                    f.write(logs_result.stderr)

        resource_data = {
            "samples": monitor.samples,
            "summary": monitor.get_summary()
        }

        # Save resource data
        if self.output_dir:
            with open(self.output_dir / "resources.json", "w") as f:
                json.dump(resource_data, f, indent=2)

        print(f"  Collected {len(monitor.samples)} resource samples")
        summary = resource_data["summary"]
        print(f"  Memory: avg={summary['memory_mb']['avg']:.1f}MB, max={summary['memory_mb']['max']:.1f}MB")
        print(f"  CPU: avg={summary['cpu_percent']['avg']:.1f}%, max={summary['cpu_percent']['max']:.1f}%")

        return {
            "stdout": logs_result.stdout,
            "stderr": logs_result.stderr,
            "exit_code": int(wait_result.stdout.strip()) if wait_result.stdout.strip() else -1
        }, resource_data

    def _collect_traces(self) -> dict:
        """Collect Claude Code trace logs."""
        traces = {"files": [], "tool_calls": []}

        # Claude Code traces are in ~/.claude/projects/-testbed/
        trace_dir = Path(self.home) / ".claude" / "projects" / "-testbed"

        if not trace_dir.exists():
            print(f"  Warning: Trace directory not found: {trace_dir}")
            return traces

        # Find the most recent trace file
        trace_files = sorted(trace_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

        if not trace_files:
            print("  Warning: No trace files found")
            return traces

        # Copy the most recent trace file
        latest_trace = trace_files[0]
        print(f"  Found trace: {latest_trace.name}")

        if self.output_dir:
            dest = self.output_dir / "trace.jsonl"
            shutil.copy(latest_trace, dest)
            traces["files"].append(str(dest))

        # Parse trace for tool calls with full details
        pending_tools = {}
        try:
            with open(latest_trace, "r") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        ts = entry.get("timestamp")
                        msg = entry.get("message", {})
                        content = msg.get("content", [])

                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict):
                                    # Tool use request
                                    if block.get("type") == "tool_use":
                                        tool_id = block.get("id")
                                        pending_tools[tool_id] = {
                                            "timestamp": ts,
                                            "tool": block.get("name"),
                                            "id": tool_id,
                                            "input": block.get("input", {})
                                        }
                                    # Tool result
                                    elif block.get("type") == "tool_result":
                                        tool_id = block.get("tool_use_id")
                                        if tool_id in pending_tools:
                                            tool_info = pending_tools.pop(tool_id)
                                            tool_info["end_timestamp"] = ts
                                            result = block.get("content", "")
                                            if isinstance(result, str) and len(result) > 500:
                                                result = result[:500] + "..."
                                            tool_info["result_preview"] = result
                                            traces["tool_calls"].append(tool_info)
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            print(f"  Warning: Failed to parse trace: {e}")

        print(f"  Found {len(traces['tool_calls'])} tool calls in trace")

        # Save tool call summary
        if self.output_dir and traces["tool_calls"]:
            with open(self.output_dir / "tool_calls.json", "w") as f:
                json.dump(traces["tool_calls"], f, indent=2)

        return traces

    def _cleanup(self):
        """Clean up containers and optionally images."""
        # Stop and remove container
        if self.container_id:
            subprocess.run(["podman", "stop", self.container_id], capture_output=True)
            subprocess.run(["podman", "rm", self.container_id], capture_output=True)
            print(f"  Removed container: {self.container_id[:12]}")

        # Remove the fixed image (optional - keep for faster reruns)
        # if self.fixed_image_name:
        #     subprocess.run(["podman", "rmi", self.fixed_image_name], capture_output=True)

        # Note: We don't remove the original image to allow for reruns
        print(f"  Note: Original image kept for faster reruns. To remove:")
        print(f"    podman rmi docker.io/{self.image_name}")
        if self.fixed_image_name:
            print(f"    podman rmi {self.fixed_image_name}")


def main():
    parser = argparse.ArgumentParser(
        description="Run Claude Code on SWE-bench tasks with resource monitoring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run on starlette issue
  python run_swebench.py swerebench/sweb.eval.x86_64.encode_1776_starlette-1147

  # With custom prompt
  python run_swebench.py swerebench/sweb.eval.x86_64.encode_1776_starlette-1147 \\
      --prompt "Fix the issue and add a test case"

  # With tests
  python run_swebench.py swerebench/sweb.eval.x86_64.encode_1776_starlette-1147 --run-tests

  # With custom resource limits
  python run_swebench.py swerebench/sweb.eval.x86_64.encode_1776_starlette-1147 \\
      --memory 8g --cpus 4
"""
    )
    parser.add_argument("image", help="SWE-bench Docker image name")
    parser.add_argument("--prompt", help="Custom prompt (default: fix issue from /issue.md)")
    parser.add_argument("--run-tests", action="store_true", help="Run tests after fixing")
    parser.add_argument("--memory", default="4g", help="Memory limit (default: 4g)")
    parser.add_argument("--cpus", default="2", help="CPU limit (default: 2)")
    parser.add_argument("--model", default="haiku", help="Model to use (default: haiku)")

    args = parser.parse_args()

    print("=" * 60)
    print("SWE-bench Runner with Resource Monitoring")
    print("=" * 60)
    print(f"Image: {args.image}")
    print(f"Memory limit: {args.memory}")
    print(f"CPU limit: {args.cpus}")
    print(f"Model: {args.model}")
    print(f"Run tests: {args.run_tests}")
    print("=" * 60)

    runner = SWEBenchRunner(
        image_name=args.image,
        memory_limit=args.memory,
        cpu_limit=args.cpus
    )

    results = runner.run(prompt=args.prompt, run_tests=args.run_tests, model=args.model)

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Total time: {results.get('total_time', 0):.1f}s")

    if "resource_samples" in results:
        summary = results["resource_samples"].get("summary", {})
        print(f"Resource samples: {summary.get('sample_count', 0)}")
        if "memory_mb" in summary:
            print(f"Memory (MB): min={summary['memory_mb']['min']:.1f}, "
                  f"max={summary['memory_mb']['max']:.1f}, avg={summary['memory_mb']['avg']:.1f}")
        if "cpu_percent" in summary:
            print(f"CPU (%): min={summary['cpu_percent']['min']:.1f}, "
                  f"max={summary['cpu_percent']['max']:.1f}, avg={summary['cpu_percent']['avg']:.1f}")

    if "traces" in results:
        print(f"Tool calls: {len(results['traces'].get('tool_calls', []))}")

    if "output_dir" in results:
        print(f"\nOutput directory: {results['output_dir']}")

    if "error" in results:
        print(f"\nError: {results['error']}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
