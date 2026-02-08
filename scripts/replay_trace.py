#!/usr/bin/env python3
"""
Trace Replay Runner for AgentCgroup experiments.

This script replays pre-collected agent traces and collects resource metrics.
It demonstrates the trace-driven evaluation methodology.

Usage:
    python scripts/replay_trace.py data/sample_traces/trace.json [--dry-run]
"""

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class StepMetrics:
    step_id: int
    tool: str
    command: str
    latency_ms: float
    exit_code: int
    cpu_time_ms: Optional[float] = None
    max_rss_kb: Optional[float] = None
    error: Optional[str] = None


def read_cgroup_cpu_time(cgroup_path: str) -> Optional[float]:
    """Read CPU time from cgroup (cgroup v2)."""
    try:
        stat_path = f"{cgroup_path}/cpu.stat"
        if os.path.exists(stat_path):
            with open(stat_path) as f:
                for line in f:
                    if line.startswith("usage_usec"):
                        return int(line.split()[1]) / 1000  # convert to ms
    except Exception:
        pass
    return None


def read_memory_current(cgroup_path: str) -> Optional[int]:
    """Read current memory usage from cgroup."""
    try:
        mem_path = f"{cgroup_path}/memory.current"
        if os.path.exists(mem_path):
            with open(mem_path) as f:
                return int(f.read().strip()) // 1024  # convert to KB
    except Exception:
        pass
    return None


def replay_bash_step(command: str, timeout_ms: int, cgroup_path: Optional[str] = None) -> StepMetrics:
    """Execute a bash command and collect metrics."""
    step_metrics = StepMetrics(
        step_id=0,
        tool="bash",
        command=command,
        latency_ms=0,
        exit_code=0
    )

    start = time.perf_counter()

    try:
        # Optionally run in cgroup (requires root)
        if cgroup_path and os.path.exists(cgroup_path):
            # Use cgexec or echo $$ to cgroup.procs
            full_cmd = f"echo $$ > {cgroup_path}/cgroup.procs && {command}"
        else:
            full_cmd = command

        result = subprocess.run(
            full_cmd,
            shell=True,
            capture_output=True,
            timeout=timeout_ms / 1000,
            cwd="/tmp"
        )
        step_metrics.exit_code = result.returncode

        if cgroup_path:
            step_metrics.cpu_time_ms = read_cgroup_cpu_time(cgroup_path)
            step_metrics.max_rss_kb = read_memory_current(cgroup_path)

    except subprocess.TimeoutExpired:
        step_metrics.exit_code = -1
        step_metrics.error = "timeout"
    except Exception as e:
        step_metrics.exit_code = -1
        step_metrics.error = str(e)

    step_metrics.latency_ms = (time.perf_counter() - start) * 1000
    return step_metrics


def replay_trace(trace_path: Path, dry_run: bool = False, cgroup_path: Optional[str] = None) -> list:
    """Replay a trace and collect metrics."""
    with open(trace_path) as f:
        trace = json.load(f)

    print(f"Replaying trace: {trace['trace_id']}")
    print(f"Source: {trace['source']}, Steps: {len(trace['steps'])}")
    print("-" * 60)

    results = []

    for step in trace["steps"]:
        step_id = step["step_id"]
        tool = step["tool"]
        command = step.get("command", "")
        timeout_ms = step.get("timeout_ms", 60000)

        # Only replay bash commands (skip editor commands for now)
        if tool != "bash":
            print(f"[{step_id:3d}] SKIP {tool}: {command[:50]}...")
            continue

        if dry_run:
            print(f"[{step_id:3d}] DRY-RUN bash: {command[:50]}...")
            metrics = StepMetrics(
                step_id=step_id,
                tool=tool,
                command=command,
                latency_ms=0,
                exit_code=0
            )
        else:
            print(f"[{step_id:3d}] EXEC bash: {command[:50]}...")
            metrics = replay_bash_step(command, timeout_ms, cgroup_path)
            metrics.step_id = step_id
            print(f"       -> latency={metrics.latency_ms:.1f}ms, exit={metrics.exit_code}")

        results.append(metrics)

    return results


def main():
    parser = argparse.ArgumentParser(description="Replay agent trace")
    parser.add_argument("trace_path", type=Path, help="Path to trace IR JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    parser.add_argument("--cgroup", type=str, help="Cgroup path for resource accounting")
    parser.add_argument("--output", type=Path, help="Output metrics file (JSONL)")
    args = parser.parse_args()

    results = replay_trace(args.trace_path, args.dry_run, args.cgroup)

    # Output summary
    print("-" * 60)
    bash_steps = [r for r in results if r.tool == "bash"]
    print(f"Executed {len(bash_steps)} bash steps")

    if bash_steps and not args.dry_run:
        latencies = [r.latency_ms for r in bash_steps]
        print(f"Latency: min={min(latencies):.1f}ms, max={max(latencies):.1f}ms, avg={sum(latencies)/len(latencies):.1f}ms")

    # Save metrics
    if args.output:
        with open(args.output, 'w') as f:
            for r in results:
                f.write(json.dumps({
                    "step_id": r.step_id,
                    "tool": r.tool,
                    "cmd": r.command[:100],
                    "latency_ms": r.latency_ms,
                    "exit_code": r.exit_code,
                    "cpu_time_ms": r.cpu_time_ms,
                    "max_rss_kb": r.max_rss_kb,
                    "error": r.error
                }) + "\n")
        print(f"Metrics saved to {args.output}")


if __name__ == "__main__":
    main()
