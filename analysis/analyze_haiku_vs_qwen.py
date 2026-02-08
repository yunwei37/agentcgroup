#!/usr/bin/env python3
"""
Haiku vs Local Model Comparison Analysis

Compares tasks from two experiment datasets:
- Haiku: all_images_haiku
- Local (GLM): all_images_local

Auto-discovers common tasks and filters out:
- Tasks missing resources.json or results.json
- Failed runs (no valid resource data)
- Tasks with resource sampling duration < MIN_DURATION_SECONDS

Usage:
    python analyze_haiku_vs_qwen.py
    python analyze_haiku_vs_qwen.py --output report.md
    python analyze_haiku_vs_qwen.py --min-duration 90
"""

import argparse
import json
import os
import re
import statistics
from datetime import datetime
from collections import defaultdict

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from filter_valid_tasks import scan_dataset, load_json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HAIKU_DIR = os.path.join(SCRIPT_DIR, "..", "experiments", "all_images_haiku")
LOCAL_DIR = os.path.join(SCRIPT_DIR, "..", "experiments", "all_images_local")
FIGURES_DIR = os.path.join(SCRIPT_DIR, "comparison_figures")
CHART_DPI = 150

MIN_DURATION_SECONDS = 60  # Minimum resource sampling duration to consider valid
MIN_SAMPLES = 10           # Minimum resource sample points


def parse_mem_mb(mem_str):
    """Parse memory string like '192.4MB / 134.5GB' into MB float."""
    if not mem_str:
        return 0.0
    match = re.match(r"([\d.]+)\s*(KB|MB|GB|TB)", mem_str.split("/")[0].strip())
    if not match:
        return 0.0
    val = float(match.group(1))
    unit = match.group(2)
    if unit == "KB": return val / 1024
    elif unit == "MB": return val
    elif unit == "GB": return val * 1024
    elif unit == "TB": return val * 1024 * 1024
    return val


def parse_cpu(cpu_str):
    """Parse CPU string like '18.5%' into float."""
    if not cpu_str:
        return 0.0
    try:
        return float(str(cpu_str).rstrip("%"))
    except:
        return 0.0


def get_task_metrics(base_dir, task_name):
    """Load detailed metrics for a task (resource dynamics, tool calls, etc.)."""
    import glob
    task_dir = os.path.join(base_dir, task_name)
    attempts = glob.glob(os.path.join(task_dir, "attempt_*"))
    if not attempts:
        return None
    attempt_dir = sorted(attempts)[-1]

    results = load_json(os.path.join(attempt_dir, "results.json"))
    resources = load_json(os.path.join(attempt_dir, "resources.json"))
    tool_calls = load_json(os.path.join(attempt_dir, "tool_calls.json"))

    if not resources:
        return None

    summary = resources.get("summary", {})
    samples = resources.get("samples", [])
    duration = summary.get("duration_seconds", 0)
    sample_count = summary.get("sample_count", 0)

    # Calculate resource dynamics
    cpu_deltas = []
    mem_deltas = []
    for i in range(1, len(samples)):
        prev_cpu = parse_cpu(samples[i-1].get("cpu_percent", ""))
        curr_cpu = parse_cpu(samples[i].get("cpu_percent", ""))
        prev_mem = parse_mem_mb(samples[i-1].get("mem_usage", ""))
        curr_mem = parse_mem_mb(samples[i].get("mem_usage", ""))
        cpu_deltas.append(abs(curr_cpu - prev_cpu))
        mem_deltas.append(abs(curr_mem - prev_mem))

    # Count tool calls by type
    tool_counts = defaultdict(int)
    if tool_calls:
        for call in tool_calls:
            tool_counts[call.get("tool", "Unknown")] += 1

    claude_time = 0
    total_time = 0
    if results:
        claude_time = results.get("claude_time", 0)
        total_time = results.get("total_time", 0)

    return {
        "claude_time": claude_time,
        "total_time": total_time,
        "duration": duration,
        "peak_mem": summary.get("memory_mb", {}).get("max", 0),
        "avg_mem": summary.get("memory_mb", {}).get("avg", 0),
        "min_mem": summary.get("memory_mb", {}).get("min", 0),
        "peak_cpu": summary.get("cpu_percent", {}).get("max", 0),
        "avg_cpu": summary.get("cpu_percent", {}).get("avg", 0),
        "sample_count": sample_count,
        "max_cpu_delta": max(cpu_deltas) if cpu_deltas else 0,
        "max_mem_delta": max(mem_deltas) if mem_deltas else 0,
        "burst_count": sum(1 for d in cpu_deltas if d > 20) + sum(1 for d in mem_deltas if d > 50),
        "tool_counts": dict(tool_counts),
        "total_tool_calls": len(tool_calls) if tool_calls else 0,
    }


def analyze_comparison(min_duration, min_samples):
    """Main comparison analysis. Uses filter_valid_tasks to find common valid tasks."""
    # Use shared filtering logic
    haiku_valid, haiku_invalid = scan_dataset(HAIKU_DIR, min_duration, min_samples)
    local_valid, local_invalid = scan_dataset(LOCAL_DIR, min_duration, min_samples)

    haiku_names = set(t["task"] for t in haiku_valid)
    local_names = set(t["task"] for t in local_valid)
    common_names = sorted(haiku_names & local_names)

    print(f"Haiku valid: {len(haiku_valid)}, invalid: {len(haiku_invalid)}")
    print(f"Local valid: {len(local_valid)}, invalid: {len(local_invalid)}")
    print(f"Common valid tasks: {len(common_names)}")

    # For tasks that exist in both dirs but only valid in one, report them
    haiku_all_dirs = set(
        d for d in os.listdir(HAIKU_DIR)
        if os.path.isdir(os.path.join(HAIKU_DIR, d))
    )
    local_all_dirs = set(
        d for d in os.listdir(LOCAL_DIR)
        if os.path.isdir(os.path.join(LOCAL_DIR, d))
    )
    common_dirs = haiku_all_dirs & local_all_dirs
    skipped = sorted(common_dirs - set(common_names))
    if skipped:
        print(f"Skipped from common dirs ({len(skipped)}):")
        for t in skipped:
            reasons = []
            if t not in haiku_names:
                inv = next((i for i in haiku_invalid if i["task"] == t), None)
                reasons.append(f"haiku: {inv['reason']}" if inv else "haiku: not found")
            if t not in local_names:
                inv = next((i for i in local_invalid if i["task"] == t), None)
                reasons.append(f"local: {inv['reason']}" if inv else "local: not found")
            print(f"  - {t} ({'; '.join(reasons)})")

    # Load detailed metrics for common tasks
    results = []
    for task_name in common_names:
        haiku_metrics = get_task_metrics(HAIKU_DIR, task_name)
        local_metrics = get_task_metrics(LOCAL_DIR, task_name)
        if haiku_metrics and local_metrics:
            results.append({
                "task": task_name,
                "haiku_metrics": haiku_metrics,
                "local_metrics": local_metrics,
            })

    return results


def print_report(results, min_duration):
    """Print comparison report to stdout."""
    sep = "=" * 110
    sep2 = "-" * 110

    n = len(results)
    print()
    print(sep)
    print("HAIKU vs LOCAL MODEL (GLM) COMPARISON")
    print(sep)
    print(f"Analysis time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Haiku data: {HAIKU_DIR}")
    print(f"Local data: {LOCAL_DIR}")
    print(f"Min duration filter: {min_duration}s")
    print(f"Valid task pairs: {n}")
    print()

    if n == 0:
        print("No valid task pairs found. Exiting.")
        return {}

    # Per-task comparison table
    print(sep)
    print("1. Per-Task Comparison")
    print(sep)
    print(f"{'Task':<45} {'H Duration':<12} {'L Duration':<12} {'H PeakMem':<12} {'L PeakMem':<12} {'H AvgCPU':<10} {'L AvgCPU':<10}")
    print(sep2)

    for r in results:
        t = r["task"]
        hm = r["haiku_metrics"]
        lm = r["local_metrics"]
        print(
            f"{t:<45} "
            f"{hm['duration']:.0f}s{'':<7} "
            f"{lm['duration']:.0f}s{'':<7} "
            f"{hm['peak_mem']:.0f}MB{'':<7} "
            f"{lm['peak_mem']:.0f}MB{'':<7} "
            f"{hm['avg_cpu']:.1f}%{'':<5} "
            f"{lm['avg_cpu']:.1f}%"
        )

    print()

    # Aggregate statistics
    haiku_durations = [r["haiku_metrics"]["duration"] for r in results]
    local_durations = [r["local_metrics"]["duration"] for r in results]
    haiku_times = [r["haiku_metrics"]["claude_time"] for r in results if r["haiku_metrics"]["claude_time"] > 0]
    local_times = [r["local_metrics"]["claude_time"] for r in results if r["local_metrics"]["claude_time"] > 0]
    haiku_mems = [r["haiku_metrics"]["peak_mem"] for r in results]
    local_mems = [r["local_metrics"]["peak_mem"] for r in results]
    haiku_cpus = [r["haiku_metrics"]["avg_cpu"] for r in results]
    local_cpus = [r["local_metrics"]["avg_cpu"] for r in results]

    print(sep)
    print("2. Aggregate Statistics")
    print(sep)

    print(f"  Resource Sampling Duration:")
    print(f"    Haiku: mean={statistics.mean(haiku_durations):.0f}s, median={statistics.median(haiku_durations):.0f}s, range={min(haiku_durations):.0f}-{max(haiku_durations):.0f}s")
    print(f"    Local: mean={statistics.mean(local_durations):.0f}s, median={statistics.median(local_durations):.0f}s, range={min(local_durations):.0f}-{max(local_durations):.0f}s")
    print()

    if haiku_times and local_times:
        print(f"  Claude Execution Time:")
        print(f"    Haiku: mean={statistics.mean(haiku_times):.0f}s, median={statistics.median(haiku_times):.0f}s, range={min(haiku_times):.0f}-{max(haiku_times):.0f}s")
        print(f"    Local: mean={statistics.mean(local_times):.0f}s, median={statistics.median(local_times):.0f}s, range={min(local_times):.0f}-{max(local_times):.0f}s")
        print(f"    Ratio: Local/Haiku = {statistics.mean(local_times)/statistics.mean(haiku_times):.2f}x")
        print()

    print(f"  Peak Memory:")
    print(f"    Haiku: mean={statistics.mean(haiku_mems):.0f}MB, median={statistics.median(haiku_mems):.0f}MB, range={min(haiku_mems):.0f}-{max(haiku_mems):.0f}MB")
    print(f"    Local: mean={statistics.mean(local_mems):.0f}MB, median={statistics.median(local_mems):.0f}MB, range={min(local_mems):.0f}-{max(local_mems):.0f}MB")
    if statistics.mean(local_mems) > 0:
        print(f"    Ratio: Haiku/Local = {statistics.mean(haiku_mems)/statistics.mean(local_mems):.2f}x")
    print()

    print(f"  Average CPU Utilization:")
    print(f"    Haiku: mean={statistics.mean(haiku_cpus):.1f}%, range={min(haiku_cpus):.1f}-{max(haiku_cpus):.1f}%")
    print(f"    Local: mean={statistics.mean(local_cpus):.1f}%, range={min(local_cpus):.1f}-{max(local_cpus):.1f}%")
    if statistics.mean(local_cpus) > 0:
        print(f"    Ratio: Haiku/Local = {statistics.mean(haiku_cpus)/statistics.mean(local_cpus):.2f}x")
    print()

    # Resource dynamics comparison
    print(sep)
    print("3. Resource Dynamics")
    print(sep)

    haiku_bursts = [r["haiku_metrics"]["burst_count"] for r in results]
    local_bursts = [r["local_metrics"]["burst_count"] for r in results]
    haiku_max_cpu_delta = [r["haiku_metrics"]["max_cpu_delta"] for r in results]
    local_max_cpu_delta = [r["local_metrics"]["max_cpu_delta"] for r in results]
    haiku_max_mem_delta = [r["haiku_metrics"]["max_mem_delta"] for r in results]
    local_max_mem_delta = [r["local_metrics"]["max_mem_delta"] for r in results]

    print(f"  Burst events (CPU>20% or Mem>50MB per second):")
    print(f"    Haiku: total={sum(haiku_bursts)}, mean={statistics.mean(haiku_bursts):.1f}/task")
    print(f"    Local: total={sum(local_bursts)}, mean={statistics.mean(local_bursts):.1f}/task")
    print()
    print(f"  Max CPU change rate (per second):")
    print(f"    Haiku: mean={statistics.mean(haiku_max_cpu_delta):.1f}%, max={max(haiku_max_cpu_delta):.1f}%")
    print(f"    Local: mean={statistics.mean(local_max_cpu_delta):.1f}%, max={max(local_max_cpu_delta):.1f}%")
    print()
    print(f"  Max memory change rate (per second):")
    print(f"    Haiku: mean={statistics.mean(haiku_max_mem_delta):.1f}MB, max={max(haiku_max_mem_delta):.1f}MB")
    print(f"    Local: mean={statistics.mean(local_max_mem_delta):.1f}MB, max={max(local_max_mem_delta):.1f}MB")
    print()

    # Tool call comparison
    print(sep)
    print("4. Tool Call Comparison")
    print(sep)

    haiku_tool_totals = defaultdict(int)
    local_tool_totals = defaultdict(int)

    for r in results:
        for tool, count in r["haiku_metrics"]["tool_counts"].items():
            haiku_tool_totals[tool] += count
        for tool, count in r["local_metrics"]["tool_counts"].items():
            local_tool_totals[tool] += count

    all_tools = set(haiku_tool_totals.keys()) | set(local_tool_totals.keys())

    print(f"  {'Tool Type':<15} {'Haiku':<10} {'Local':<10} {'Diff':<10}")
    print(f"  {'-'*45}")
    for tool in sorted(all_tools):
        h_count = haiku_tool_totals.get(tool, 0)
        l_count = local_tool_totals.get(tool, 0)
        diff = l_count - h_count
        diff_str = f"+{diff}" if diff > 0 else str(diff)
        print(f"  {tool:<15} {h_count:<10} {l_count:<10} {diff_str:<10}")

    print()
    print(sep)
    print("Analysis complete")
    print(sep)

    stats = {
        "n_tasks": n,
        "haiku_avg_duration": statistics.mean(haiku_durations),
        "local_avg_duration": statistics.mean(local_durations),
        "haiku_avg_mem": statistics.mean(haiku_mems),
        "local_avg_mem": statistics.mean(local_mems),
        "haiku_avg_cpu": statistics.mean(haiku_cpus),
        "local_avg_cpu": statistics.mean(local_cpus),
    }
    if haiku_times and local_times:
        stats["haiku_avg_time"] = statistics.mean(haiku_times)
        stats["local_avg_time"] = statistics.mean(local_times)
        stats["time_ratio"] = statistics.mean(local_times) / statistics.mean(haiku_times)
    if statistics.mean(local_mems) > 0:
        stats["mem_ratio"] = statistics.mean(haiku_mems) / statistics.mean(local_mems)
    if statistics.mean(local_cpus) > 0:
        stats["cpu_ratio"] = statistics.mean(haiku_cpus) / statistics.mean(local_cpus)

    return stats


def generate_charts(results):
    """Generate comparison charts."""
    os.makedirs(FIGURES_DIR, exist_ok=True)

    tasks = [r["task"] for r in results]
    n = len(tasks)

    haiku_durations = [r["haiku_metrics"]["duration"] for r in results]
    local_durations = [r["local_metrics"]["duration"] for r in results]
    haiku_mems = [r["haiku_metrics"]["peak_mem"] for r in results]
    local_mems = [r["local_metrics"]["peak_mem"] for r in results]
    haiku_cpus = [r["haiku_metrics"]["avg_cpu"] for r in results]
    local_cpus = [r["local_metrics"]["avg_cpu"] for r in results]

    # Short labels: take the last part after __ and truncate
    short_labels = []
    for t in tasks:
        parts = t.split("__")
        label = parts[-1] if len(parts) > 1 else t
        if len(label) > 25:
            label = label[:22] + "..."
        short_labels.append(label)

    # Chart 1: Resource sampling duration comparison
    fig, ax = plt.subplots(figsize=(max(12, n * 0.5), 6))
    x = np.arange(n)
    width = 0.35
    ax.bar(x - width/2, haiku_durations, width, label='Haiku', color='#3498db', alpha=0.8)
    ax.bar(x + width/2, local_durations, width, label='Local (GLM)', color='#e74c3c', alpha=0.8)
    ax.set_ylabel('Resource Sampling Duration (seconds)')
    ax.set_title(f'Resource Sampling Duration: Haiku vs Local ({n} tasks)')
    ax.set_xticks(x)
    ax.set_xticklabels(short_labels, fontsize=7, rotation=60, ha='right')
    ax.legend()
    ax.grid(axis='y', alpha=0.3)
    fig.tight_layout()
    path = os.path.join(FIGURES_DIR, "01_duration_comparison.png")
    fig.savefig(path, dpi=CHART_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  [CHART] Saved: {path}")

    # Chart 2: Execution time comparison (claude_time)
    haiku_times = [r["haiku_metrics"]["claude_time"] for r in results]
    local_times = [r["local_metrics"]["claude_time"] for r in results]
    fig, ax = plt.subplots(figsize=(max(12, n * 0.5), 6))
    ax.bar(x - width/2, haiku_times, width, label='Haiku', color='#3498db', alpha=0.8)
    ax.bar(x + width/2, local_times, width, label='Local (GLM)', color='#e74c3c', alpha=0.8)
    ax.set_ylabel('Execution Time (seconds)')
    ax.set_title(f'Execution Time Comparison: Haiku vs Local ({n} tasks)')
    ax.set_xticks(x)
    ax.set_xticklabels(short_labels, fontsize=7, rotation=60, ha='right')
    ax.legend()
    ax.grid(axis='y', alpha=0.3)
    fig.tight_layout()
    path = os.path.join(FIGURES_DIR, "02_execution_time_comparison.png")
    fig.savefig(path, dpi=CHART_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  [CHART] Saved: {path}")

    # Chart 3: Peak memory comparison
    fig, ax = plt.subplots(figsize=(max(12, n * 0.5), 6))
    ax.bar(x - width/2, haiku_mems, width, label='Haiku', color='#3498db', alpha=0.8)
    ax.bar(x + width/2, local_mems, width, label='Local (GLM)', color='#e74c3c', alpha=0.8)
    ax.set_ylabel('Peak Memory (MB)')
    ax.set_title(f'Peak Memory Comparison: Haiku vs Local ({n} tasks)')
    ax.set_xticks(x)
    ax.set_xticklabels(short_labels, fontsize=7, rotation=60, ha='right')
    ax.legend()
    ax.grid(axis='y', alpha=0.3)
    fig.tight_layout()
    path = os.path.join(FIGURES_DIR, "03_peak_memory_comparison.png")
    fig.savefig(path, dpi=CHART_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  [CHART] Saved: {path}")

    # Chart 4: CPU utilization comparison
    fig, ax = plt.subplots(figsize=(max(12, n * 0.5), 6))
    ax.bar(x - width/2, haiku_cpus, width, label='Haiku', color='#3498db', alpha=0.8)
    ax.bar(x + width/2, local_cpus, width, label='Local (GLM)', color='#e74c3c', alpha=0.8)
    ax.set_ylabel('Average CPU Utilization (%)', fontsize=15)
    ax.set_title(f'CPU Utilization Comparison: Haiku vs Local ({n} tasks)', fontsize=16)
    ax.set_xticks(x)
    ax.set_xticklabels(short_labels, fontsize=10, rotation=60, ha='right')
    ax.legend(fontsize=13)
    ax.tick_params(axis='y', labelsize=13)
    ax.grid(axis='y', alpha=0.3)
    fig.tight_layout()
    path = os.path.join(FIGURES_DIR, "04_cpu_utilization_comparison.png")
    fig.savefig(path, dpi=CHART_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  [CHART] Saved: {path}")

    # Chart 5: Scatter plot - Duration vs Peak Memory
    fig, ax = plt.subplots(figsize=(10, 8))
    ax.scatter(haiku_durations, haiku_mems, c='#3498db', marker='o', s=80, alpha=0.7, label='Haiku')
    ax.scatter(local_durations, local_mems, c='#e74c3c', marker='s', s=80, alpha=0.7, label='Local (GLM)')
    for i in range(n):
        ax.plot(
            [haiku_durations[i], local_durations[i]],
            [haiku_mems[i], local_mems[i]],
            'k-', alpha=0.15
        )
    ax.set_xlabel('Resource Sampling Duration (seconds)')
    ax.set_ylabel('Peak Memory (MB)')
    ax.set_title('Duration vs Peak Memory (Haiku vs Local)')
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    path = os.path.join(FIGURES_DIR, "05_time_vs_memory_scatter.png")
    fig.savefig(path, dpi=CHART_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  [CHART] Saved: {path}")

    # Chart 6: Summary comparison bar chart
    haiku_times_valid = [r["haiku_metrics"]["claude_time"] for r in results if r["haiku_metrics"]["claude_time"] > 0]
    local_times_valid = [r["local_metrics"]["claude_time"] for r in results if r["local_metrics"]["claude_time"] > 0]

    metrics_labels = ['Avg Duration\n(s/10)', 'Avg Peak Mem\n(MB/100)', 'Avg CPU\n(%)']
    haiku_vals = [
        statistics.mean(haiku_durations) / 10,
        statistics.mean(haiku_mems) / 100,
        statistics.mean(haiku_cpus),
    ]
    local_vals = [
        statistics.mean(local_durations) / 10,
        statistics.mean(local_mems) / 100,
        statistics.mean(local_cpus),
    ]
    if haiku_times_valid and local_times_valid:
        metrics_labels.insert(0, 'Avg Exec Time\n(s/10)')
        haiku_vals.insert(0, statistics.mean(haiku_times_valid) / 10)
        local_vals.insert(0, statistics.mean(local_times_valid) / 10)

    fig, ax = plt.subplots(figsize=(10, 6))
    xm = np.arange(len(metrics_labels))
    ax.bar(xm - width/2, haiku_vals, width, label='Haiku', color='#3498db', alpha=0.8)
    ax.bar(xm + width/2, local_vals, width, label='Local (GLM)', color='#e74c3c', alpha=0.8)
    ax.set_ylabel('Value')
    ax.set_title(f'Overall Comparison: Haiku vs Local ({n} tasks)')
    ax.set_xticks(xm)
    ax.set_xticklabels(metrics_labels)
    ax.legend()
    ax.grid(axis='y', alpha=0.3)
    fig.tight_layout()
    path = os.path.join(FIGURES_DIR, "06_overall_comparison.png")
    fig.savefig(path, dpi=CHART_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  [CHART] Saved: {path}")


def generate_markdown_report(results, stats, output_path, min_duration):
    """Generate markdown report."""
    n = stats.get("n_tasks", 0)
    report = f"""# Haiku vs Local Model (GLM) Comparison Report

**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## Overview

This report compares resource usage between Haiku (cloud API) and Local GLM (on-device GPU) agents
across {n} common SWE-rebench tasks (filtered: sampling duration >= {min_duration}s, valid resource data required).

- **Haiku data**: `experiments/all_images_haiku/`
- **Local data**: `experiments/all_images_local/`

## 1. Aggregate Statistics

| Metric | Haiku | Local (GLM) | Ratio |
|--------|-------|-------------|-------|
| Valid tasks | {n} | {n} | - |
| Avg sampling duration | {stats.get('haiku_avg_duration', 0):.0f}s | {stats.get('local_avg_duration', 0):.0f}s | {stats.get('local_avg_duration', 0) / max(stats.get('haiku_avg_duration', 1), 1):.2f}x |
"""

    if "haiku_avg_time" in stats:
        report += f"| Avg execution time | {stats['haiku_avg_time']:.0f}s | {stats['local_avg_time']:.0f}s | Local {stats['time_ratio']:.2f}x |\n"

    report += f"| Avg peak memory | {stats.get('haiku_avg_mem', 0):.0f}MB | {stats.get('local_avg_mem', 0):.0f}MB | Haiku {stats.get('mem_ratio', 0):.2f}x |\n"
    report += f"| Avg CPU utilization | {stats.get('haiku_avg_cpu', 0):.1f}% | {stats.get('local_avg_cpu', 0):.1f}% | Haiku {stats.get('cpu_ratio', 0):.1f}x |\n"

    report += f"""
![Duration Comparison](comparison_figures/01_duration_comparison.png)

![Execution Time Comparison](comparison_figures/02_execution_time_comparison.png)

![Peak Memory Comparison](comparison_figures/03_peak_memory_comparison.png)

![CPU Utilization Comparison](comparison_figures/04_cpu_utilization_comparison.png)

## 2. Per-Task Details

| Task | H Duration | L Duration | H Peak Mem | L Peak Mem | H Avg CPU | L Avg CPU |
|------|-----------|-----------|-----------|-----------|----------|----------|
"""

    for r in results:
        t = r["task"]
        hm = r["haiku_metrics"]
        lm = r["local_metrics"]
        report += (
            f"| {t} | {hm['duration']:.0f}s | {lm['duration']:.0f}s "
            f"| {hm['peak_mem']:.0f}MB | {lm['peak_mem']:.0f}MB "
            f"| {hm['avg_cpu']:.1f}% | {lm['avg_cpu']:.1f}% |\n"
        )

    report += f"""
## 3. Key Findings

### Resource Usage Patterns
1. **CPU Utilization**: Haiku avg {stats.get('haiku_avg_cpu', 0):.1f}% vs Local {stats.get('local_avg_cpu', 0):.1f}% ({stats.get('cpu_ratio', 0):.1f}x difference)
2. **Peak Memory**: Haiku avg {stats.get('haiku_avg_mem', 0):.0f}MB vs Local {stats.get('local_avg_mem', 0):.0f}MB
3. API-based agents (Haiku) consume more CPU for network I/O and protocol overhead
4. Local inference agents offload compute to GPU (not captured by cgroup CPU metrics)

### Implications for Resource Management
- Different agent architectures require fundamentally different resource profiles
- Static resource limits cannot accommodate both agent types efficiently
- This heterogeneity reinforces the **domain mismatch** argument

![Time vs Memory Scatter](comparison_figures/05_time_vs_memory_scatter.png)

![Overall Comparison](comparison_figures/06_overall_comparison.png)
"""

    with open(output_path, 'w') as f:
        f.write(report)

    print(f"  [REPORT] Saved: {output_path}")


def main():
    global HAIKU_DIR, LOCAL_DIR

    parser = argparse.ArgumentParser(description="Haiku vs Local Model Comparison Analysis")
    parser.add_argument("--output", default=None, help="Output markdown report path")
    parser.add_argument(
        "--min-duration", type=int, default=MIN_DURATION_SECONDS,
        help=f"Minimum resource sampling duration in seconds (default: {MIN_DURATION_SECONDS})"
    )
    parser.add_argument(
        "--min-samples", type=int, default=MIN_SAMPLES,
        help=f"Minimum number of resource samples (default: {MIN_SAMPLES})"
    )
    parser.add_argument("--haiku-dir", default=None, help="Haiku experiment directory")
    parser.add_argument("--local-dir", default=None, help="Local model experiment directory")
    args = parser.parse_args()

    if args.haiku_dir:
        HAIKU_DIR = args.haiku_dir
    if args.local_dir:
        LOCAL_DIR = args.local_dir

    print("=" * 70)
    print("Haiku vs Local Model Comparison Analysis")
    print("=" * 70)
    print()

    # Run analysis
    results = analyze_comparison(args.min_duration, args.min_samples)

    if not results:
        print("No valid task pairs found after filtering. Exiting.")
        return

    # Print report
    stats = print_report(results, args.min_duration)

    # Generate charts
    print()
    print("=" * 70)
    print("Generating charts...")
    print("=" * 70)
    generate_charts(results)

    # Generate markdown report
    output_path = args.output or os.path.join(SCRIPT_DIR, "haiku_vs_qwen_report.md")
    print()
    print("=" * 70)
    print("Generating Markdown report...")
    print("=" * 70)
    generate_markdown_report(results, stats, output_path, args.min_duration)

    print()
    print("=" * 70)
    print("Analysis complete!")
    print("=" * 70)


if __name__ == "__main__":
    main()
