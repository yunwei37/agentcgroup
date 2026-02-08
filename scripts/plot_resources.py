#!/usr/bin/env python3
"""
Plot resource usage from SWE-bench runs.

Usage:
    python scripts/plot_resources.py <resources.json> [--tool-calls tool_calls.json] [--output plot.png]
    python scripts/plot_resources.py experiments/batch_test_xxx/SQL_Data_Easy/attempt_1/resources.json
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np


def parse_memory(mem_str: str) -> Optional[float]:
    """Parse memory string to MB."""
    mem_str = mem_str.strip()
    try:
        if mem_str.endswith("GiB"):
            return float(mem_str[:-3]) * 1024
        elif mem_str.endswith("MiB"):
            return float(mem_str[:-3])
        elif mem_str.endswith("KiB"):
            return float(mem_str[:-3]) / 1024
        elif mem_str.endswith("GB"):
            return float(mem_str[:-2]) * 1000
        elif mem_str.endswith("MB"):
            return float(mem_str[:-2])
        elif mem_str.endswith("KB") or mem_str.endswith("kB"):
            return float(mem_str[:-2]) / 1000
        elif mem_str.endswith("B"):
            return float(mem_str[:-1]) / (1024 * 1024)
    except:
        pass
    return None


def load_resources(resources_path: Path) -> Dict:
    """Load resource samples from JSON file."""
    with open(resources_path, "r") as f:
        data = json.load(f)

    samples = data.get("samples", [])
    if not samples:
        return {"times": [], "memory": [], "cpu": [], "summary": data.get("summary", {})}

    # Get start time
    start_epoch = samples[0]["epoch"]

    times = []
    memory = []
    cpu = []

    for s in samples:
        times.append(s["epoch"] - start_epoch)

        # Parse memory
        mem_str = s["mem_usage"].split("/")[0].strip()
        mem_mb = parse_memory(mem_str)
        memory.append(mem_mb if mem_mb else 0)

        # Parse CPU
        cpu_str = s["cpu_percent"].replace("%", "").strip()
        try:
            cpu.append(float(cpu_str))
        except:
            cpu.append(0)

    return {
        "times": times,
        "memory": memory,
        "cpu": cpu,
        "summary": data.get("summary", {}),
        "start_epoch": start_epoch,
    }


def load_tool_calls(tool_calls_path: Path, start_epoch: float) -> List[Dict]:
    """Load tool calls and convert to relative times."""
    with open(tool_calls_path, "r") as f:
        calls = json.load(f)

    result = []
    for call in calls:
        ts = call.get("timestamp", "")
        tool = call.get("tool", "")

        if ts and tool:
            # Parse ISO timestamp
            try:
                # Handle timezone
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                dt = datetime.fromisoformat(ts)
                epoch = dt.timestamp()
                rel_time = epoch - start_epoch

                if rel_time >= 0:  # Only include if after start
                    result.append({
                        "time": rel_time,
                        "tool": tool,
                    })
            except:
                pass

    return result


def plot_resources(
    resources_path: Path,
    tool_calls_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    title: Optional[str] = None,
    memory_limit: Optional[float] = None,
    cpu_limit: Optional[float] = None,
):
    """Generate resource usage plot."""

    # Load data
    data = load_resources(resources_path)
    times = data["times"]
    memory = data["memory"]
    cpu = data["cpu"]
    summary = data["summary"]

    if not times:
        print("No resource samples found")
        return None

    # Load tool calls if provided
    tool_calls = []
    if tool_calls_path and tool_calls_path.exists():
        tool_calls = load_tool_calls(tool_calls_path, data["start_epoch"])

    # Create figure
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

    # Title
    if title:
        fig.suptitle(title, fontsize=14, fontweight='bold')
    else:
        fig.suptitle("Claude Code Resource Usage", fontsize=14, fontweight='bold')

    # Memory plot
    ax1.fill_between(times, memory, alpha=0.3, color='blue')
    ax1.plot(times, memory, 'b-', linewidth=1.5, label='Memory Usage')

    # Memory average line
    avg_mem = summary.get("memory_mb", {}).get("avg", np.mean(memory))
    ax1.axhline(y=avg_mem, color='gray', linestyle='--', alpha=0.7,
                label=f'Avg: {avg_mem:.1f} MB')

    # Memory limit line if specified
    if memory_limit:
        ax1.axhline(y=memory_limit, color='orange', linestyle=':', alpha=0.7,
                    label=f'Limit: {memory_limit:.0f} MB')

    ax1.set_ylabel('Memory (MB)', fontsize=11)
    ax1.legend(loc='upper left', fontsize=9)
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(bottom=0)

    # CPU plot
    ax2.fill_between(times, cpu, alpha=0.3, color='green')
    ax2.plot(times, cpu, 'g-', linewidth=1.5, label='CPU Usage')

    # CPU average line
    avg_cpu = summary.get("cpu_percent", {}).get("avg", np.mean(cpu))
    ax2.axhline(y=avg_cpu, color='gray', linestyle='--', alpha=0.7,
                label=f'Avg: {avg_cpu:.1f}%')

    # CPU limit line if specified
    if cpu_limit:
        ax2.axhline(y=cpu_limit * 100, color='orange', linestyle=':', alpha=0.7,
                    label=f'Limit ({cpu_limit:.0f} CPUs)')

    ax2.set_ylabel('CPU (%)', fontsize=11)
    ax2.set_xlabel('Time (seconds)', fontsize=11)
    ax2.legend(loc='upper right', fontsize=9)
    ax2.grid(True, alpha=0.3)
    ax2.set_ylim(bottom=0)

    # Add tool call markers
    if tool_calls:
        # Group by tool type for coloring
        tool_colors = {
            'Bash': 'red',
            'Read': 'blue',
            'Edit': 'purple',
            'Write': 'purple',
            'Task': 'orange',
            'Grep': 'cyan',
            'Glob': 'cyan',
        }

        # Only show significant tools (Bash commands are usually the interesting ones)
        bash_calls = [c for c in tool_calls if c['tool'] == 'Bash']

        for call in bash_calls:
            t = call["time"]
            if 0 <= t <= max(times):
                ax1.axvline(x=t, color='red', linestyle=':', alpha=0.5, linewidth=0.8)
                ax2.axvline(x=t, color='red', linestyle=':', alpha=0.5, linewidth=0.8)

    # Add summary box
    summary_text = []
    duration = summary.get("duration_seconds", max(times) if times else 0)
    summary_text.append(f"Duration: {duration:.1f}s")
    summary_text.append(f"Samples: {len(times)}")

    mem_stats = summary.get("memory_mb", {})
    if mem_stats:
        summary_text.append(f"Memory: {mem_stats.get('avg', 0):.1f} MB avg, {mem_stats.get('max', 0):.1f} MB max")

    cpu_stats = summary.get("cpu_percent", {})
    if cpu_stats:
        summary_text.append(f"CPU: {cpu_stats.get('avg', 0):.1f}% avg, {cpu_stats.get('max', 0):.1f}% max")

    if tool_calls:
        summary_text.append(f"Tool calls: {len(tool_calls)}")

    props = dict(boxstyle='round', facecolor='wheat', alpha=0.8)
    fig.text(0.02, 0.02, '\n'.join(summary_text), fontsize=9,
             verticalalignment='bottom', bbox=props, family='monospace')

    plt.tight_layout()
    plt.subplots_adjust(bottom=0.15)

    # Save or show
    if output_path:
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        print(f"Plot saved to: {output_path}")
    else:
        plt.show()

    plt.close()
    return output_path


def plot_from_attempt_dir(attempt_dir: Path, title: Optional[str] = None) -> Optional[Path]:
    """Generate plot from an attempt directory."""
    resources_path = attempt_dir / "resources.json"
    tool_calls_path = attempt_dir / "tool_calls.json"

    if not resources_path.exists():
        print(f"No resources.json found in {attempt_dir}")
        return None

    output_path = attempt_dir / "resource_plot.png"

    return plot_resources(
        resources_path=resources_path,
        tool_calls_path=tool_calls_path if tool_calls_path.exists() else None,
        output_path=output_path,
        title=title,
    )


def main():
    parser = argparse.ArgumentParser(description="Plot resource usage from SWE-bench runs")
    parser.add_argument("input", help="Path to resources.json or attempt directory")
    parser.add_argument("--tool-calls", help="Path to tool_calls.json")
    parser.add_argument("--output", "-o", help="Output path for plot")
    parser.add_argument("--title", "-t", help="Plot title")
    parser.add_argument("--memory-limit", type=float, help="Memory limit in MB to show")
    parser.add_argument("--cpu-limit", type=float, help="CPU limit (number of CPUs) to show")

    args = parser.parse_args()

    input_path = Path(args.input)

    if input_path.is_dir():
        # Assume it's an attempt directory
        plot_from_attempt_dir(input_path, title=args.title)
    elif input_path.suffix == ".json":
        # It's a resources.json file
        tool_calls_path = None
        if args.tool_calls:
            tool_calls_path = Path(args.tool_calls)
        else:
            # Try to find tool_calls.json in same directory
            possible_tc = input_path.parent / "tool_calls.json"
            if possible_tc.exists():
                tool_calls_path = possible_tc

        output_path = None
        if args.output:
            output_path = Path(args.output)
        else:
            output_path = input_path.parent / "resource_plot.png"

        plot_resources(
            resources_path=input_path,
            tool_calls_path=tool_calls_path,
            output_path=output_path,
            title=args.title,
            memory_limit=args.memory_limit,
            cpu_limit=args.cpu_limit,
        )
    else:
        print(f"Unknown input type: {input_path}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
