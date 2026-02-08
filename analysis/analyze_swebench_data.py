#!/usr/bin/env python3
"""
Comprehensive analysis script for AgentCgroup SWE-Bench experiment data.

Analyzes experiment data to validate research questions related to:
- RQ1: Resource usage dynamics and burstiness (time-scale mismatch)
- RQ2: Resource usage differences across task categories (domain mismatch)
- RQ3: Tool call patterns and resource consumption
- RQ4: Peak vs average resource gap (over-provisioning problem)

Usage:
    python analyze_swebench_data.py --all                    # Run all analyses (default: haiku)
    python analyze_swebench_data.py --dataset qwen3 --all    # Analyze qwen3 dataset
    python analyze_swebench_data.py --dataset haiku --all    # Analyze haiku dataset
    python analyze_swebench_data.py --dynamics               # RQ1 only
    python analyze_swebench_data.py --categories             # RQ2 only
    python analyze_swebench_data.py --tools                  # RQ3 only
    python analyze_swebench_data.py --overprovisioning       # RQ4 only
    python analyze_swebench_data.py --report                 # Generate markdown report
"""

import argparse
import glob as glob_module
import json
import os
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np

from filter_valid_tasks import get_valid_task_names

# Base paths
SCRIPT_DIR_PATH = Path(os.path.dirname(os.path.abspath(__file__)))
ANALYSIS_DIR = SCRIPT_DIR_PATH

# Dataset configurations
DATASETS = {
    "haiku": {
        "base_dir": SCRIPT_DIR_PATH / ".." / "experiments" / "all_images_haiku",
        "output_dir": ANALYSIS_DIR / "haiku_figures",
        "report_path": ANALYSIS_DIR / "haiku_figures" / "report.md",
        "type": "flat",  # Plain task directories (repo__name-issue)
        "description": "SWE-Bench tasks with Haiku model (all_images_haiku)"
    },
    "qwen3": {
        "base_dir": SCRIPT_DIR_PATH / ".." / "experiments" / "all_images_local",
        "output_dir": ANALYSIS_DIR / "qwen3_figures",
        "report_path": ANALYSIS_DIR / "qwen3_figures" / "report.md",
        "type": "flat",  # Plain task directories (repo__name-issue)
        "description": "SWE-Bench tasks with local GLM model (all_images_local)"
    }
}

# Categories and difficulty levels (for haiku dataset)
CATEGORIES = ["CLI_Tools", "DevOps_Build", "ML_Scientific", "Medical_Bio", "SQL_Data", "Web_Network"]
DIFFICULTIES = ["Easy", "Medium", "Hard"]

# Threshold configurations
CPU_BURST_THRESHOLD = 20.0  # CPU change > 20% in 1 second
MEM_BURST_THRESHOLD = 50.0  # Memory change > 50MB in 1 second

# Global config (set by main)
BASE_DIR = None
OUTPUT_DIR = None
REPORT_PATH = None
DATASET_TYPE = None


@dataclass
class ResourceSample:
    """A single resource usage sample."""
    timestamp: str
    epoch: float
    mem_usage_mb: float
    cpu_percent: float


@dataclass
class ToolCall:
    """A tool call with start and end timestamps."""
    tool: str
    tool_id: str
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: float = 0.0


@dataclass
class BurstEvent:
    """A detected burst event in resource usage."""
    timestamp: float
    delta_cpu: float
    delta_mem: float
    cpu_before: float
    cpu_after: float
    mem_before: float
    mem_after: float


@dataclass
class TaskData:
    """All data for a single task."""
    name: str
    category: str
    difficulty: str
    success: bool
    attempts: int
    total_time: float
    claude_time: float
    resource_samples: List[ResourceSample] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    bursts: List[BurstEvent] = field(default_factory=list)
    # Summary stats
    mem_min: float = 0.0
    mem_max: float = 0.0
    mem_avg: float = 0.0
    cpu_min: float = 0.0
    cpu_max: float = 0.0
    cpu_avg: float = 0.0


def parse_mem_usage(mem_str: str) -> float:
    """Parse memory usage string like '156.2MB / 16.19GB' to MB."""
    match = re.match(r"([\d.]+)(MB|GB|KB)", mem_str.split("/")[0].strip())
    if not match:
        return 0.0
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "GB":
        return value * 1024
    elif unit == "KB":
        return value / 1024
    return value


def parse_iso(ts_str: str) -> Optional[datetime]:
    """Parse ISO format timestamp, handling Z suffix and fractional seconds."""
    if ts_str is None:
        return None
    ts_str = ts_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return None


def load_json(path: Path) -> Optional[Dict]:
    """Load JSON file, return None on failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def load_jsonl(path: Path) -> List[Dict]:
    """Load JSONL file, return list of parsed objects."""
    records = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except (FileNotFoundError, OSError):
        pass
    return records


def extract_tool_times_from_trace(trace_records: List[Dict]) -> Dict[str, ToolCall]:
    """
    Extract tool execution times from trace.jsonl records.

    Tool execution time = tool_result.timestamp - tool_use.timestamp
    """
    tool_starts = {}  # id -> (tool_name, start_time)
    tool_calls = {}  # id -> ToolCall

    for record in trace_records:
        if record.get("type") == "assistant":
            message = record.get("message", {})
            content = message.get("content", [])
            timestamp_str = record.get("timestamp")
            timestamp = parse_iso(timestamp_str)

            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    tool_id = item.get("id")
                    tool_name = item.get("name")
                    if tool_id and tool_name and timestamp:
                        tool_starts[tool_id] = (tool_name, timestamp)

        elif record.get("type") == "user":
            message = record.get("message", {})
            content = message.get("content", [])
            timestamp_str = record.get("timestamp")
            timestamp = parse_iso(timestamp_str)

            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        tool_id = item.get("tool_use_id")
                        if tool_id and tool_id in tool_starts and timestamp:
                            tool_name, start_time = tool_starts[tool_id]
                            duration = (timestamp - start_time).total_seconds()
                            tool_calls[tool_id] = ToolCall(
                                tool=tool_name,
                                tool_id=tool_id,
                                start_time=start_time,
                                end_time=timestamp,
                                duration_seconds=max(0, duration)
                            )

    return tool_calls


def detect_bursts(samples: List[ResourceSample]) -> List[BurstEvent]:
    """
    Detect burst events in resource samples.

    A burst is defined as:
    - CPU change > CPU_BURST_THRESHOLD% in ~1 second
    - Memory change > MEM_BURST_THRESHOLD MB in ~1 second
    """
    bursts = []

    for i in range(1, len(samples)):
        prev = samples[i - 1]
        curr = samples[i]

        time_delta = curr.epoch - prev.epoch
        if time_delta <= 0 or time_delta > 2.0:  # Skip if samples too far apart
            continue

        delta_cpu = abs(curr.cpu_percent - prev.cpu_percent)
        delta_mem = abs(curr.mem_usage_mb - prev.mem_usage_mb)

        # Normalize to per-second change
        delta_cpu_per_sec = delta_cpu / time_delta
        delta_mem_per_sec = delta_mem / time_delta

        if delta_cpu_per_sec > CPU_BURST_THRESHOLD or delta_mem_per_sec > MEM_BURST_THRESHOLD:
            bursts.append(BurstEvent(
                timestamp=curr.epoch,
                delta_cpu=delta_cpu_per_sec,
                delta_mem=delta_mem_per_sec,
                cpu_before=prev.cpu_percent,
                cpu_after=curr.cpu_percent,
                mem_before=prev.mem_usage_mb,
                mem_after=curr.mem_usage_mb
            ))

    return bursts


def load_task_data(task_dir: Path, task_name: str, task_info: Dict,
                   category: str = "unknown", difficulty: str = "unknown",
                   force_attempt_1: bool = False) -> Optional[TaskData]:
    """Load data for a single task."""
    if force_attempt_1:
        attempts = 1
        # For attempt_1, success is True only if total attempts == 1 (no retry needed)
        success = task_info.get("attempts", 1) == 1 and task_info.get("success", False)
    else:
        attempts = task_info.get("attempts", 1)
        success = task_info.get("success", False)
    attempt_dir = task_dir / f"attempt_{attempts}"

    resources_data = load_json(attempt_dir / "resources.json")
    results_data = load_json(attempt_dir / "results.json")
    trace_records = load_jsonl(attempt_dir / "trace.jsonl")

    if not resources_data or not results_data:
        return None

    # Check if claude_time exists
    claude_time = results_data.get("claude_time", 0.0)
    if claude_time <= 0:
        return None

    # Parse resource samples
    samples = []
    for s in resources_data.get("samples", []):
        cpu_str = s.get("cpu_percent", "0")
        if isinstance(cpu_str, str):
            cpu_str = cpu_str.rstrip("%")
        try:
            cpu_val = float(cpu_str)
        except (ValueError, TypeError):
            cpu_val = 0.0

        samples.append(ResourceSample(
            timestamp=s.get("timestamp", ""),
            epoch=s.get("epoch", 0.0),
            mem_usage_mb=parse_mem_usage(s.get("mem_usage", "0MB")),
            cpu_percent=cpu_val
        ))

    if not samples:
        return None

    # Get summary stats
    summary = resources_data.get("summary", {})
    mem_summary = summary.get("memory_mb", {})
    cpu_summary = summary.get("cpu_percent", {})

    # Extract tool times from trace, fall back to tool_calls.json
    tool_calls_dict = extract_tool_times_from_trace(trace_records)
    tool_calls = list(tool_calls_dict.values())
    if not tool_calls:
        tc_json = load_json(attempt_dir / "tool_calls.json")
        if tc_json and isinstance(tc_json, list):
            for entry in tc_json:
                st = parse_iso(entry.get("timestamp"))
                et = parse_iso(entry.get("end_timestamp"))
                dur = (et - st).total_seconds() if st and et else 0.0
                tool_calls.append(ToolCall(
                    tool=entry.get("tool", "Unknown"),
                    tool_id=entry.get("tool_use_id", ""),
                    start_time=st,
                    end_time=et,
                    duration_seconds=max(0, dur),
                ))

    # Detect bursts
    bursts = detect_bursts(samples)

    return TaskData(
        name=task_name,
        category=category,
        difficulty=difficulty,
        success=success,
        attempts=attempts,
        total_time=task_info.get("total_time", 0.0),
        claude_time=claude_time,
        resource_samples=samples,
        tool_calls=tool_calls,
        bursts=bursts,
        mem_min=mem_summary.get("min", 0.0),
        mem_max=mem_summary.get("max", 0.0),
        mem_avg=mem_summary.get("avg", 0.0),
        cpu_min=cpu_summary.get("min", 0.0),
        cpu_max=cpu_summary.get("max", 0.0),
        cpu_avg=cpu_summary.get("avg", 0.0)
    )


def load_all_data() -> Tuple[Dict[str, TaskData], Dict]:
    """Load all task data, using filter_valid_tasks for task discovery."""
    valid_names = get_valid_task_names(str(BASE_DIR))
    print(f"Valid tasks after filtering: {len(valid_names)}")

    progress = load_json(BASE_DIR / "progress.json")
    results_map = {}
    if progress and "results" in progress:
        results_map = progress["results"]

    tasks = {}

    for task_name in valid_names:
        task_dir = BASE_DIR / task_name
        if not task_dir.is_dir():
            continue

        task_info = results_map.get(task_name, {})

        # Infer category/difficulty if it matches the old naming pattern
        category = "swebench"
        difficulty = "unknown"
        parts = task_name.rsplit("_", 1)
        if len(parts) == 2 and parts[0] in CATEGORIES and parts[1] in DIFFICULTIES:
            category = parts[0]
            difficulty = parts[1]

        task = load_task_data(task_dir, task_name, task_info, category, difficulty)
        if task:
            tasks[task_name] = task

    return tasks, progress or {}


# =============================================================================
# RQ1: Resource Usage Dynamics (Time-scale Mismatch)
# =============================================================================

def analyze_dynamics(tasks: Dict[str, TaskData]) -> Dict[str, Any]:
    """
    RQ1: Analyze resource usage dynamics and burstiness.

    Validates: "User-space controllers react in 10-100ms, but resource
    changes happen at millisecond scale"
    """
    print("\n" + "=" * 78)
    print("RQ1: RESOURCE USAGE DYNAMICS (TIME-SCALE MISMATCH)")
    print("=" * 78)

    results = {
        "total_bursts": 0,
        "tasks_with_bursts": 0,
        "burst_details": [],
        "cpu_change_rates": [],
        "mem_change_rates": [],
        "per_task_stats": []
    }

    all_cpu_deltas = []
    all_mem_deltas = []

    for task_name, task in tasks.items():
        # Calculate change rates between consecutive samples
        samples = task.resource_samples
        task_cpu_deltas = []
        task_mem_deltas = []

        for i in range(1, len(samples)):
            prev = samples[i - 1]
            curr = samples[i]
            time_delta = curr.epoch - prev.epoch

            if time_delta > 0 and time_delta < 2.0:
                cpu_rate = abs(curr.cpu_percent - prev.cpu_percent) / time_delta
                mem_rate = abs(curr.mem_usage_mb - prev.mem_usage_mb) / time_delta
                task_cpu_deltas.append(cpu_rate)
                task_mem_deltas.append(mem_rate)
                all_cpu_deltas.append(cpu_rate)
                all_mem_deltas.append(mem_rate)

        results["total_bursts"] += len(task.bursts)
        if task.bursts:
            results["tasks_with_bursts"] += 1

        results["per_task_stats"].append({
            "task": task_name,
            "num_bursts": len(task.bursts),
            "max_cpu_rate": max(task_cpu_deltas) if task_cpu_deltas else 0,
            "max_mem_rate": max(task_mem_deltas) if task_mem_deltas else 0,
            "avg_cpu_rate": statistics.mean(task_cpu_deltas) if task_cpu_deltas else 0,
            "avg_mem_rate": statistics.mean(task_mem_deltas) if task_mem_deltas else 0,
        })

    results["cpu_change_rates"] = all_cpu_deltas
    results["mem_change_rates"] = all_mem_deltas

    # Print summary
    print(f"\n{'Summary Statistics':^40}")
    print("-" * 40)
    print(f"  Total tasks analyzed:       {len(tasks)}")
    print(f"  Tasks with burst events:    {results['tasks_with_bursts']}")
    print(f"  Total burst events:         {results['total_bursts']}")
    print(f"  Avg bursts per task:        {results['total_bursts'] / max(len(tasks), 1):.2f}")

    if all_cpu_deltas:
        print(f"\n{'CPU Change Rate (%/sec)':^40}")
        print("-" * 40)
        print(f"  Mean:                       {statistics.mean(all_cpu_deltas):.2f}")
        print(f"  Median:                     {statistics.median(all_cpu_deltas):.2f}")
        print(f"  Max:                        {max(all_cpu_deltas):.2f}")
        if len(all_cpu_deltas) > 1:
            print(f"  Std Dev:                    {statistics.stdev(all_cpu_deltas):.2f}")
        print(f"  95th percentile:            {np.percentile(all_cpu_deltas, 95):.2f}")

    if all_mem_deltas:
        print(f"\n{'Memory Change Rate (MB/sec)':^40}")
        print("-" * 40)
        print(f"  Mean:                       {statistics.mean(all_mem_deltas):.2f}")
        print(f"  Median:                     {statistics.median(all_mem_deltas):.2f}")
        print(f"  Max:                        {max(all_mem_deltas):.2f}")
        if len(all_mem_deltas) > 1:
            print(f"  Std Dev:                    {statistics.stdev(all_mem_deltas):.2f}")
        print(f"  95th percentile:            {np.percentile(all_mem_deltas, 95):.2f}")

    # Per-task burst summary table (top 20)
    print(f"\n{'Per-Task Burst Statistics (Top 20)':^78}")
    print("-" * 78)
    print(f"  {'Task':<45} {'Bursts':>8} {'MaxCPU%/s':>12} {'MaxMem MB/s':>12}")
    print("-" * 78)

    sorted_tasks = sorted(results["per_task_stats"], key=lambda x: x["num_bursts"], reverse=True)
    for stat in sorted_tasks[:20]:
        task_display = stat['task'][:44]
        print(f"  {task_display:<45} {stat['num_bursts']:>8} {stat['max_cpu_rate']:>12.2f} {stat['max_mem_rate']:>12.2f}")

    # Generate visualizations
    _plot_dynamics(tasks, results)

    return results


def _plot_dynamics(tasks: Dict[str, TaskData], results: Dict):
    """Generate visualizations for RQ1."""

    # Plot 1: Resource time series with burst annotations for a sample task
    # Pick task with most bursts for visualization
    sample_task = max(tasks.values(), key=lambda t: len(t.bursts))

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    samples = sample_task.resource_samples
    if samples:
        times = [(s.epoch - samples[0].epoch) for s in samples]
        cpu_vals = [s.cpu_percent for s in samples]
        mem_vals = [s.mem_usage_mb for s in samples]

        ax1.plot(times, cpu_vals, 'b-', linewidth=1, label='CPU %')
        ax1.set_ylabel('CPU Usage (%)', fontsize=15)
        ax1.set_title(f'Resource Usage Over Time - {sample_task.name[:50]}', fontsize=16)
        ax1.tick_params(axis='both', labelsize=13)
        ax1.grid(True, alpha=0.3)

        # Mark tool call intervals as shaded regions
        t0_epoch = samples[0].epoch
        tool_label_added = False
        for tc in sample_task.tool_calls:
            if tc.start_time is None:
                continue
            tc_start = tc.start_time.timestamp() - t0_epoch
            tc_end = (tc.end_time.timestamp() - t0_epoch) if tc.end_time else tc_start
            lbl = 'Tool Call' if not tool_label_added else None
            ax1.axvspan(tc_start, max(tc_end, tc_start + 0.5),
                        color='r', alpha=0.10, label=lbl)
            ax2.axvspan(tc_start, max(tc_end, tc_start + 0.5),
                        color='r', alpha=0.10)
            tool_label_added = True

        ax1.legend(fontsize=13)

        ax2.plot(times, mem_vals, 'g-', linewidth=1, label='Memory (MB)')
        ax2.set_xlabel('Time (seconds)', fontsize=15)
        ax2.set_ylabel('Memory Usage (MB)', fontsize=15)
        ax2.legend(fontsize=13)
        ax2.tick_params(axis='both', labelsize=13)
        ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / "rq1_resource_timeseries.png", dpi=150)
    plt.close()

    # Plot 2: Distribution of change rates
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    cpu_rates = results["cpu_change_rates"]
    mem_rates = results["mem_change_rates"]

    if cpu_rates:
        ax1.hist(cpu_rates, bins=50, edgecolor='black', alpha=0.7)
        ax1.axvline(x=CPU_BURST_THRESHOLD, color='r', linestyle='--', label=f'Burst threshold ({CPU_BURST_THRESHOLD}%/s)')
        ax1.set_xlabel('CPU Change Rate (%/sec)', fontsize=15)
        ax1.set_ylabel('Frequency', fontsize=15)
        ax1.set_title('Distribution of CPU Change Rates', fontsize=16)
        ax1.legend(fontsize=13)
        ax1.tick_params(axis='both', labelsize=13)
        ax1.set_yscale('log')

    if mem_rates:
        ax2.hist(mem_rates, bins=50, edgecolor='black', alpha=0.7)
        ax2.axvline(x=MEM_BURST_THRESHOLD, color='r', linestyle='--', label=f'Burst threshold ({MEM_BURST_THRESHOLD}MB/s)')
        ax2.set_xlabel('Memory Change Rate (MB/sec)', fontsize=15)
        ax2.set_ylabel('Frequency', fontsize=15)
        ax2.set_title('Distribution of Memory Change Rates', fontsize=16)
        ax2.legend(fontsize=13)
        ax2.tick_params(axis='both', labelsize=13)
        ax2.set_yscale('log')

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / "rq1_change_rate_distribution.png", dpi=150)
    plt.close()

    print(f"\n  Figures saved to: {OUTPUT_DIR}/rq1_*.png")


# =============================================================================
# RQ2: Category-based Resource Usage Differences (Domain Mismatch)
# =============================================================================

def analyze_categories(tasks: Dict[str, TaskData]) -> Dict[str, Any]:
    """
    RQ2: Analyze resource usage differences across task categories.

    Validates: "Static resource limits cannot adapt to different workloads"
    """
    print("\n" + "=" * 78)
    print("RQ2: RESOURCE USAGE BY CATEGORY (DOMAIN MISMATCH)")
    print("=" * 78)

    results = {
        "by_category": defaultdict(list),
        "by_difficulty": defaultdict(list),
        "by_category_difficulty": defaultdict(list)
    }

    # Group tasks by category and difficulty
    for task in tasks.values():
        stats = {
            "name": task.name,
            "cpu_avg": task.cpu_avg,
            "cpu_max": task.cpu_max,
            "mem_avg": task.mem_avg,
            "mem_max": task.mem_max,
            "duration": task.claude_time
        }
        results["by_category"][task.category].append(stats)
        results["by_difficulty"][task.difficulty].append(stats)
        results["by_category_difficulty"][f"{task.category}_{task.difficulty}"].append(stats)

    # Get unique categories from actual data
    actual_categories = sorted(results["by_category"].keys())

    # Print category statistics
    print(f"\n{'Resource Usage by Category':^78}")
    print("-" * 78)
    print(f"  {'Category':<35} {'N':>4} {'AvgCPU':>10} {'MaxCPU':>10} {'AvgMem(MB)':>12} {'MaxMem(MB)':>12}")
    print("-" * 78)

    category_stats = {}
    for cat in actual_categories:
        task_list = results["by_category"].get(cat, [])
        if not task_list:
            continue

        avg_cpu = statistics.mean([t["cpu_avg"] for t in task_list])
        max_cpu = max([t["cpu_max"] for t in task_list])
        avg_mem = statistics.mean([t["mem_avg"] for t in task_list])
        max_mem = max([t["mem_max"] for t in task_list])

        category_stats[cat] = {
            "n": len(task_list),
            "avg_cpu": avg_cpu,
            "max_cpu": max_cpu,
            "avg_mem": avg_mem,
            "max_mem": max_mem
        }

        cat_display = cat[:34]
        print(f"  {cat_display:<35} {len(task_list):>4} {avg_cpu:>10.2f} {max_cpu:>10.2f} {avg_mem:>12.2f} {max_mem:>12.2f}")

    results["category_stats"] = category_stats

    # Print difficulty statistics (only if we have categorized data)
    actual_difficulties = sorted(results["by_difficulty"].keys())
    if len(actual_difficulties) > 1 and "unknown" not in actual_difficulties:
        print(f"\n{'Resource Usage by Difficulty':^78}")
        print("-" * 78)
        print(f"  {'Difficulty':<20} {'N':>4} {'AvgCPU':>10} {'MaxCPU':>10} {'AvgMem(MB)':>12} {'MaxMem(MB)':>12}")
        print("-" * 78)

        difficulty_stats = {}
        for diff in actual_difficulties:
            task_list = results["by_difficulty"].get(diff, [])
            if not task_list:
                continue

            avg_cpu = statistics.mean([t["cpu_avg"] for t in task_list])
            max_cpu = max([t["cpu_max"] for t in task_list])
            avg_mem = statistics.mean([t["mem_avg"] for t in task_list])
            max_mem = max([t["mem_max"] for t in task_list])

            difficulty_stats[diff] = {
                "n": len(task_list),
                "avg_cpu": avg_cpu,
                "max_cpu": max_cpu,
                "avg_mem": avg_mem,
                "max_mem": max_mem
            }

            print(f"  {diff:<20} {len(task_list):>4} {avg_cpu:>10.2f} {max_cpu:>10.2f} {avg_mem:>12.2f} {max_mem:>12.2f}")

        results["difficulty_stats"] = difficulty_stats

    # Calculate variance across categories (demonstrates domain mismatch)
    all_cat_avgs = [s["avg_mem"] for s in category_stats.values()]
    if len(all_cat_avgs) > 1:
        print(f"\n{'Cross-Category Variance':^40}")
        print("-" * 40)
        print(f"  Memory avg range:     {min(all_cat_avgs):.2f} - {max(all_cat_avgs):.2f} MB")
        if len(all_cat_avgs) > 1:
            print(f"  Memory avg std dev:   {statistics.stdev(all_cat_avgs):.2f} MB")

        cpu_avgs = [s["avg_cpu"] for s in category_stats.values()]
        print(f"  CPU avg range:        {min(cpu_avgs):.2f} - {max(cpu_avgs):.2f} %")
        if len(cpu_avgs) > 1:
            print(f"  CPU avg std dev:      {statistics.stdev(cpu_avgs):.2f} %")

    # Generate visualizations
    _plot_categories(tasks, results, actual_categories)

    return results


def _plot_categories(tasks: Dict[str, TaskData], results: Dict, categories: List[str]):
    """Generate visualizations for RQ2."""

    cat_data = results["by_category"]

    # Only plot if we have data
    if not cat_data:
        return

    # Plot 1: Box plots by category (memory usage)
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    cat_names = [c for c in categories if c in cat_data and cat_data[c]]

    if not cat_names:
        plt.close()
        return

    cpu_data = [[t["cpu_avg"] for t in cat_data[c]] for c in cat_names]
    mem_data = [[t["mem_avg"] for t in cat_data[c]] for c in cat_names]
    cpu_max_data = [[t["cpu_max"] for t in cat_data[c]] for c in cat_names]
    mem_max_data = [[t["mem_max"] for t in cat_data[c]] for c in cat_names]

    # Shorten category names for display
    short_names = [c[:15] for c in cat_names]

    if cpu_data and all(d for d in cpu_data):
        axes[0, 0].boxplot(cpu_data, labels=short_names)
        axes[0, 0].set_ylabel('Average CPU (%)', fontsize=15)
        axes[0, 0].set_title('Average CPU Usage by Category', fontsize=16)
        axes[0, 0].tick_params(axis='x', rotation=45, labelsize=13)
        axes[0, 0].tick_params(axis='y', labelsize=13)

    if mem_data and all(d for d in mem_data):
        axes[0, 1].boxplot(mem_data, labels=short_names)
        axes[0, 1].set_ylabel('Average Memory (MB)', fontsize=15)
        axes[0, 1].set_title('Average Memory Usage by Category', fontsize=16)
        axes[0, 1].tick_params(axis='x', rotation=45, labelsize=13)
        axes[0, 1].tick_params(axis='y', labelsize=13)

    if cpu_max_data and all(d for d in cpu_max_data):
        axes[1, 0].boxplot(cpu_max_data, labels=short_names)
        axes[1, 0].set_ylabel('Peak CPU (%)', fontsize=15)
        axes[1, 0].set_title('Peak CPU Usage by Category', fontsize=16)
        axes[1, 0].tick_params(axis='x', rotation=45, labelsize=13)
        axes[1, 0].tick_params(axis='y', labelsize=13)

    if mem_max_data and all(d for d in mem_max_data):
        axes[1, 1].boxplot(mem_max_data, labels=short_names)
        axes[1, 1].set_ylabel('Peak Memory (MB)', fontsize=15)
        axes[1, 1].set_title('Peak Memory Usage by Category', fontsize=16)
        axes[1, 1].tick_params(axis='x', rotation=45, labelsize=13)
        axes[1, 1].tick_params(axis='y', labelsize=13)

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / "rq2_category_boxplots.png", dpi=150)
    plt.close()

    print(f"\n  Figures saved to: {OUTPUT_DIR}/rq2_*.png")


# =============================================================================
# RQ3: Tool Call Patterns and Resource Consumption
# =============================================================================

def analyze_tools(tasks: Dict[str, TaskData]) -> Dict[str, Any]:
    """
    RQ3: Analyze tool call patterns and their relationship to resources.

    Provides data for fine-grained resource control.
    """
    print("\n" + "=" * 78)
    print("RQ3: TOOL CALL PATTERNS AND RESOURCE CONSUMPTION")
    print("=" * 78)

    results = {
        "tool_stats": defaultdict(lambda: {"count": 0, "total_time": 0.0, "durations": []}),
        "per_task_tool_time": [],
        "tool_vs_thinking_ratio": []
    }

    # Aggregate tool call statistics
    for task in tasks.values():
        total_tool_time = 0.0

        for call in task.tool_calls:
            tool_name = call.tool
            results["tool_stats"][tool_name]["count"] += 1
            results["tool_stats"][tool_name]["total_time"] += call.duration_seconds
            results["tool_stats"][tool_name]["durations"].append(call.duration_seconds)
            total_tool_time += call.duration_seconds

        thinking_time = task.claude_time - total_tool_time
        ratio = (total_tool_time / task.claude_time * 100) if task.claude_time > 0 else 0

        results["per_task_tool_time"].append({
            "task": task.name,
            "claude_time": task.claude_time,
            "tool_time": total_tool_time,
            "thinking_time": thinking_time,
            "tool_ratio": ratio,
            "num_calls": len(task.tool_calls)
        })

        results["tool_vs_thinking_ratio"].append(ratio)

    # Print tool statistics
    print(f"\n{'Tool Call Statistics':^78}")
    print("-" * 78)
    print(f"  {'Tool':<20} {'Count':>8} {'TotalTime':>12} {'AvgTime':>10} {'MaxTime':>10}")
    print("-" * 78)

    sorted_tools = sorted(results["tool_stats"].items(), key=lambda x: x[1]["total_time"], reverse=True)
    for tool_name, stats in sorted_tools:
        if stats["count"] > 0:
            avg_time = stats["total_time"] / stats["count"]
            max_time = max(stats["durations"]) if stats["durations"] else 0
            print(f"  {tool_name:<20} {stats['count']:>8} {stats['total_time']:>10.2f}s {avg_time:>9.2f}s {max_time:>9.2f}s")

    # Overall statistics
    total_tool_time = sum(s["total_time"] for s in results["tool_stats"].values())
    total_calls = sum(s["count"] for s in results["tool_stats"].values())
    total_claude_time = sum(t["claude_time"] for t in results["per_task_tool_time"])

    print(f"\n{'Overall Tool Time Analysis':^40}")
    print("-" * 40)
    print(f"  Total tool calls:           {total_calls}")
    print(f"  Total tool execution time:  {total_tool_time:.2f}s ({total_tool_time/60:.1f} min)")
    print(f"  Total Claude time:          {total_claude_time:.2f}s ({total_claude_time/60:.1f} min)")
    if total_claude_time > 0:
        print(f"  Tool time ratio:            {(total_tool_time/total_claude_time*100):.1f}%")

    if results["tool_vs_thinking_ratio"]:
        print(f"\n{'Per-Task Tool Time Ratio':^40}")
        print("-" * 40)
        ratios = results["tool_vs_thinking_ratio"]
        print(f"  Mean:                       {statistics.mean(ratios):.1f}%")
        print(f"  Median:                     {statistics.median(ratios):.1f}%")
        print(f"  Min:                        {min(ratios):.1f}%")
        print(f"  Max:                        {max(ratios):.1f}%")

    # Generate visualizations
    _plot_tools(tasks, results)

    return results


def _plot_tools(tasks: Dict[str, TaskData], results: Dict):
    """Generate visualizations for RQ3."""

    # Plot 1: Tool execution time by tool type
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    tool_names = []
    tool_counts = []
    tool_times = []

    for tool_name, stats in sorted(results["tool_stats"].items(), key=lambda x: x[1]["count"], reverse=True):
        if stats["count"] > 0:
            tool_names.append(tool_name)
            tool_counts.append(stats["count"])
            tool_times.append(stats["total_time"])

    # Bar chart of call counts
    if tool_names:
        axes[0, 0].barh(tool_names[:10], tool_counts[:10])
        axes[0, 0].set_xlabel('Number of Calls')
        axes[0, 0].set_title('Tool Call Frequency (Top 10)')
        axes[0, 0].invert_yaxis()

        # Bar chart of total time
        axes[0, 1].barh(tool_names[:10], tool_times[:10])
        axes[0, 1].set_xlabel('Total Execution Time (seconds)')
        axes[0, 1].set_title('Total Tool Execution Time (Top 10)')
        axes[0, 1].invert_yaxis()

    # Distribution of tool time ratios
    ratios = results["tool_vs_thinking_ratio"]
    if ratios:
        axes[1, 0].hist(ratios, bins=20, edgecolor='black', alpha=0.7)
        axes[1, 0].set_xlabel('Tool Time Ratio (%)')
        axes[1, 0].set_ylabel('Number of Tasks')
        axes[1, 0].set_title('Distribution of Tool Time Ratios')
        axes[1, 0].axvline(x=statistics.mean(ratios), color='r', linestyle='--', label=f'Mean ({statistics.mean(ratios):.1f}%)')
        axes[1, 0].legend()

    # Box plot of tool durations for top tools
    top_tools = [t for t in list(results["tool_stats"].keys())[:6] if results["tool_stats"][t]["durations"]]
    duration_data = [results["tool_stats"][t]["durations"] for t in top_tools]
    if duration_data:
        axes[1, 1].boxplot(duration_data, labels=top_tools[:len(duration_data)])
        axes[1, 1].set_ylabel('Duration (seconds)')
        axes[1, 1].set_title('Tool Execution Time Distribution')
        axes[1, 1].tick_params(axis='x', rotation=45)

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / "rq3_tool_analysis.png", dpi=150)
    plt.close()

    print(f"\n  Figures saved to: {OUTPUT_DIR}/rq3_*.png")


# =============================================================================
# RQ4: Peak vs Average Resource Gap (Over-provisioning)
# =============================================================================

def analyze_overprovisioning(tasks: Dict[str, TaskData]) -> Dict[str, Any]:
    """
    RQ4: Analyze peak vs average resource gap.

    Quantifies how much over-provisioning static limits would require.
    """
    print("\n" + "=" * 78)
    print("RQ4: OVER-PROVISIONING ANALYSIS (PEAK VS AVERAGE)")
    print("=" * 78)

    results = {
        "per_task": [],
        "cpu_peak_avg_ratios": [],
        "mem_peak_avg_ratios": []
    }

    for task in tasks.values():
        cpu_ratio = task.cpu_max / task.cpu_avg if task.cpu_avg > 0 else 1.0
        mem_ratio = task.mem_max / task.mem_avg if task.mem_avg > 0 else 1.0

        results["per_task"].append({
            "task": task.name,
            "category": task.category,
            "difficulty": task.difficulty,
            "cpu_avg": task.cpu_avg,
            "cpu_max": task.cpu_max,
            "cpu_ratio": cpu_ratio,
            "mem_avg": task.mem_avg,
            "mem_max": task.mem_max,
            "mem_ratio": mem_ratio
        })

        results["cpu_peak_avg_ratios"].append(cpu_ratio)
        results["mem_peak_avg_ratios"].append(mem_ratio)

    # Print per-task statistics (top 20 by memory ratio)
    print(f"\n{'Peak/Average Ratio by Task (Top 20 by Mem Ratio)':^78}")
    print("-" * 78)
    print(f"  {'Task':<35} {'CPU Avg':>9} {'CPU Max':>9} {'Ratio':>7} {'Mem Avg':>9} {'Mem Max':>9} {'Ratio':>7}")
    print("-" * 78)

    sorted_tasks = sorted(results["per_task"], key=lambda x: x["mem_ratio"], reverse=True)
    for stat in sorted_tasks[:20]:
        task_display = stat['task'][:34]
        print(f"  {task_display:<35} {stat['cpu_avg']:>8.1f}% {stat['cpu_max']:>8.1f}% {stat['cpu_ratio']:>6.2f}x "
              f"{stat['mem_avg']:>8.1f} {stat['mem_max']:>8.1f} {stat['mem_ratio']:>6.2f}x")

    # Overall statistics
    cpu_ratios = results["cpu_peak_avg_ratios"]
    mem_ratios = results["mem_peak_avg_ratios"]

    print(f"\n{'Over-provisioning Factor Summary':^40}")
    print("-" * 40)

    if cpu_ratios:
        print(f"\n  CPU Peak/Avg Ratio:")
        print(f"    Mean:                     {statistics.mean(cpu_ratios):.2f}x")
        print(f"    Median:                   {statistics.median(cpu_ratios):.2f}x")
        print(f"    Max:                      {max(cpu_ratios):.2f}x")
        print(f"    95th percentile:          {np.percentile(cpu_ratios, 95):.2f}x")

    if mem_ratios:
        print(f"\n  Memory Peak/Avg Ratio:")
        print(f"    Mean:                     {statistics.mean(mem_ratios):.2f}x")
        print(f"    Median:                   {statistics.median(mem_ratios):.2f}x")
        print(f"    Max:                      {max(mem_ratios):.2f}x")
        print(f"    95th percentile:          {np.percentile(mem_ratios, 95):.2f}x")

    # Calculate wasted resources under static provisioning
    if tasks:
        avg_mem_all = statistics.mean([t.mem_avg for t in tasks.values()])
        max_mem_all = max([t.mem_max for t in tasks.values()])

        print(f"\n{'Static Provisioning Waste Analysis':^40}")
        print("-" * 40)
        print(f"  If provisioned at global max memory ({max_mem_all:.1f} MB):")
        print(f"    Average memory used:      {avg_mem_all:.1f} MB")
        print(f"    Waste ratio:              {(max_mem_all - avg_mem_all) / max_mem_all * 100:.1f}%")

    # Generate visualizations
    _plot_overprovisioning(tasks, results)

    return results


def _plot_overprovisioning(tasks: Dict[str, TaskData], results: Dict):
    """Generate visualizations for RQ4."""

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    cpu_ratios = results["cpu_peak_avg_ratios"]
    mem_ratios = results["mem_peak_avg_ratios"]

    # Histogram of CPU peak/avg ratios
    if cpu_ratios:
        axes[0, 0].hist(cpu_ratios, bins=15, edgecolor='black', alpha=0.7)
        axes[0, 0].axvline(x=statistics.mean(cpu_ratios), color='r', linestyle='--',
                           label=f'Mean ({statistics.mean(cpu_ratios):.2f}x)')
        axes[0, 0].set_xlabel('Peak/Average CPU Ratio', fontsize=15)
        axes[0, 0].set_ylabel('Number of Tasks', fontsize=15)
        axes[0, 0].set_title('CPU Over-provisioning Factor', fontsize=16)
        axes[0, 0].legend(fontsize=13)
        axes[0, 0].tick_params(axis='both', labelsize=13)

    # Histogram of Memory peak/avg ratios
    if mem_ratios:
        axes[0, 1].hist(mem_ratios, bins=15, edgecolor='black', alpha=0.7)
        axes[0, 1].axvline(x=statistics.mean(mem_ratios), color='r', linestyle='--',
                           label=f'Mean ({statistics.mean(mem_ratios):.2f}x)')
        axes[0, 1].set_xlabel('Peak/Average Memory Ratio', fontsize=15)
        axes[0, 1].set_ylabel('Number of Tasks', fontsize=15)
        axes[0, 1].set_title('Memory Over-provisioning Factor', fontsize=16)
        axes[0, 1].legend(fontsize=13)
        axes[0, 1].tick_params(axis='both', labelsize=13)

    # Scatter plot: avg vs max memory
    if results["per_task"]:
        for stat in results["per_task"]:
            axes[1, 0].scatter(stat["mem_avg"], stat["mem_max"], s=50, alpha=0.7)

        max_val = max(max([s["mem_max"] for s in results["per_task"]]),
                      max([s["mem_avg"] for s in results["per_task"]]))
        axes[1, 0].plot([0, max_val], [0, max_val], 'k--', alpha=0.3, label='y=x (no waste)')
        axes[1, 0].set_xlabel('Average Memory (MB)', fontsize=15)
        axes[1, 0].set_ylabel('Peak Memory (MB)', fontsize=15)
        axes[1, 0].set_title('Average vs Peak Memory Usage', fontsize=16)
        axes[1, 0].legend(fontsize=13)
        axes[1, 0].tick_params(axis='both', labelsize=13)

    # Distribution summary
    if cpu_ratios and mem_ratios:
        labels = ['CPU Ratio', 'Memory Ratio']
        means = [statistics.mean(cpu_ratios), statistics.mean(mem_ratios)]
        medians = [statistics.median(cpu_ratios), statistics.median(mem_ratios)]

        x = np.arange(len(labels))
        width = 0.35

        axes[1, 1].bar(x - width/2, means, width, label='Mean')
        axes[1, 1].bar(x + width/2, medians, width, label='Median')
        axes[1, 1].set_ylabel('Over-provisioning Factor (x)', fontsize=15)
        axes[1, 1].set_title('Summary: Over-provisioning Factors', fontsize=16)
        axes[1, 1].set_xticks(x)
        axes[1, 1].set_xticklabels(labels, fontsize=14)
        axes[1, 1].legend(fontsize=13)
        axes[1, 1].tick_params(axis='y', labelsize=13)
        axes[1, 1].axhline(y=1, color='r', linestyle='--', alpha=0.5)

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / "rq4_overprovisioning.png", dpi=150)
    plt.close()

    print(f"\n  Figures saved to: {OUTPUT_DIR}/rq4_*.png")


# =============================================================================
# Report Generation
# =============================================================================

def generate_report(tasks: Dict[str, TaskData],
                   dynamics_results: Dict = None,
                   categories_results: Dict = None,
                   tools_results: Dict = None,
                   overprov_results: Dict = None,
                   dataset_name: str = "unknown") -> str:
    """Generate a comprehensive markdown report."""

    print("\n" + "=" * 78)
    print("GENERATING COMPREHENSIVE REPORT")
    print("=" * 78)

    report = []
    report.append(f"# AgentCgroup SWE-Bench Experiment Analysis Report ({dataset_name})\n")
    report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    report.append(f"Data source: `{BASE_DIR}`\n")
    report.append(f"Total tasks analyzed: {len(tasks)}\n")

    # Dataset overview
    report.append("\n## Dataset Overview\n")
    report.append("| Metric | Value |")
    report.append("|--------|-------|")

    success_count = sum(1 for t in tasks.values() if t.success)
    total_time = sum(t.total_time for t in tasks.values())
    report.append(f"| Total tasks | {len(tasks)} |")
    report.append(f"| Successful | {success_count} ({success_count/len(tasks)*100:.1f}%) |")
    report.append(f"| Total execution time | {total_time:.1f}s ({total_time/60:.1f} min) |")

    # RQ1: Dynamics
    if dynamics_results:
        report.append("\n## RQ1: Resource Usage Dynamics (Time-scale Mismatch)\n")
        report.append("**Research Question**: How dynamic are resource changes during AI agent execution?\n")
        report.append("**Paper Claim**: User-space controllers react in 10-100ms, but resource changes happen at millisecond scale.\n")

        report.append("\n### Findings\n")
        report.append(f"- **Total burst events detected**: {dynamics_results['total_bursts']}")
        report.append(f"- **Tasks with bursts**: {dynamics_results['tasks_with_bursts']} / {len(tasks)}")

        if dynamics_results["cpu_change_rates"]:
            cpu_rates = dynamics_results["cpu_change_rates"]
            report.append(f"\n**CPU Change Rate Statistics (%/sec)**:")
            report.append(f"- Mean: {statistics.mean(cpu_rates):.2f}")
            report.append(f"- Max: {max(cpu_rates):.2f}")
            report.append(f"- 95th percentile: {np.percentile(cpu_rates, 95):.2f}")

        report.append(f"\n![Resource Time Series](rq1_resource_timeseries.png)")
        report.append(f"\n![Change Rate Distribution](rq1_change_rate_distribution.png)")

    # RQ2: Categories
    if categories_results:
        report.append("\n## RQ2: Resource Usage by Category (Domain Mismatch)\n")
        report.append("**Research Question**: Do different task categories have significantly different resource needs?\n")
        report.append("**Paper Claim**: Static resource limits cannot adapt to different workloads.\n")

        report.append("\n### Memory Usage by Category\n")
        report.append("| Category | N | Avg Memory (MB) | Peak Memory (MB) |")
        report.append("|----------|---|-----------------|------------------|")

        for cat, stats in categories_results.get("category_stats", {}).items():
            report.append(f"| {cat[:30]} | {stats['n']} | {stats['avg_mem']:.1f} | {stats['max_mem']:.1f} |")

        report.append(f"\n![Category Box Plots](rq2_category_boxplots.png)")

    # RQ3: Tools
    if tools_results:
        report.append("\n## RQ3: Tool Call Patterns\n")
        report.append("**Research Question**: What is the relationship between tool calls and resource consumption?\n")

        report.append("\n### Top Tools by Execution Time\n")
        report.append("| Tool | Call Count | Total Time (s) | Avg Time (s) |")
        report.append("|------|------------|----------------|--------------|")

        sorted_tools = sorted(tools_results["tool_stats"].items(),
                             key=lambda x: x[1]["total_time"], reverse=True)[:10]
        for tool_name, stats in sorted_tools:
            avg_time = stats["total_time"] / stats["count"] if stats["count"] > 0 else 0
            report.append(f"| {tool_name} | {stats['count']} | {stats['total_time']:.2f} | {avg_time:.2f} |")

        if tools_results["tool_vs_thinking_ratio"]:
            ratios = tools_results["tool_vs_thinking_ratio"]
            report.append(f"\n**Tool Time Ratio**: Mean {statistics.mean(ratios):.1f}%, Median {statistics.median(ratios):.1f}%")

        report.append(f"\n![Tool Analysis](rq3_tool_analysis.png)")

    # RQ4: Over-provisioning
    if overprov_results:
        report.append("\n## RQ4: Over-provisioning Analysis\n")
        report.append("**Research Question**: How much over-provisioning would static limits require?\n")

        cpu_ratios = overprov_results["cpu_peak_avg_ratios"]
        mem_ratios = overprov_results["mem_peak_avg_ratios"]

        if cpu_ratios and mem_ratios:
            report.append("\n### Over-provisioning Factors\n")
            report.append("| Metric | CPU Ratio | Memory Ratio |")
            report.append("|--------|-----------|--------------|")
            report.append(f"| Mean | {statistics.mean(cpu_ratios):.2f}x | {statistics.mean(mem_ratios):.2f}x |")
            report.append(f"| Median | {statistics.median(cpu_ratios):.2f}x | {statistics.median(mem_ratios):.2f}x |")
            report.append(f"| Max | {max(cpu_ratios):.2f}x | {max(mem_ratios):.2f}x |")
            report.append(f"| 95th Percentile | {np.percentile(cpu_ratios, 95):.2f}x | {np.percentile(mem_ratios, 95):.2f}x |")

        report.append(f"\n![Over-provisioning Analysis](rq4_overprovisioning.png)")

    # Conclusions
    report.append("\n## Key Conclusions\n")
    report.append("1. **Time-scale Mismatch**: Resource usage exhibits significant burstiness that exceeds ")
    report.append("   the reaction time of typical user-space controllers.")
    report.append("2. **Domain Mismatch**: Different task categories show distinct resource profiles, ")
    report.append("   making static limits suboptimal.")
    report.append("3. **Over-provisioning Waste**: Static provisioning at peak levels wastes significant resources,")
    report.append("   as average usage is typically much lower than peak.")

    report_text = "\n".join(report)

    # Write report to file
    with open(REPORT_PATH, 'w') as f:
        f.write(report_text)

    print(f"\n  Report saved to: {REPORT_PATH}")

    return report_text


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    global BASE_DIR, OUTPUT_DIR, REPORT_PATH, DATASET_TYPE

    parser = argparse.ArgumentParser(
        description="Analyze AgentCgroup SWE-Bench experiment data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python analyze_swebench_data.py --all                    # Run all analyses (haiku)
  python analyze_swebench_data.py --dataset qwen3 --all    # Analyze qwen3 dataset
  python analyze_swebench_data.py --dataset haiku --all    # Analyze haiku dataset
  python analyze_swebench_data.py --dynamics               # RQ1: Time-scale analysis
  python analyze_swebench_data.py --categories             # RQ2: Domain mismatch
  python analyze_swebench_data.py --tools                  # RQ3: Tool patterns
  python analyze_swebench_data.py --overprovisioning       # RQ4: Over-provisioning
  python analyze_swebench_data.py --report                 # Generate markdown report

Available datasets:
  haiku  - 18 tasks with Haiku model (categorized by domain/difficulty)
  qwen3  - SWE-Bench tasks with Qwen3 model
        """
    )

    parser.add_argument("--dataset", choices=["haiku", "qwen3"], default="haiku",
                       help="Dataset to analyze (default: haiku)")
    parser.add_argument("--all", action="store_true", help="Run all analyses")
    parser.add_argument("--dynamics", action="store_true", help="RQ1: Analyze resource dynamics")
    parser.add_argument("--categories", action="store_true", help="RQ2: Analyze by category")
    parser.add_argument("--tools", action="store_true", help="RQ3: Analyze tool patterns")
    parser.add_argument("--overprovisioning", action="store_true", help="RQ4: Analyze over-provisioning")
    parser.add_argument("--report", action="store_true", help="Generate markdown report")

    args = parser.parse_args()

    # Set global config based on dataset
    dataset_config = DATASETS[args.dataset]
    BASE_DIR = dataset_config["base_dir"]
    OUTPUT_DIR = dataset_config["output_dir"]
    REPORT_PATH = dataset_config["report_path"]
    DATASET_TYPE = dataset_config["type"]

    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Default to --all if no specific analysis requested
    if not any([args.all, args.dynamics, args.categories, args.tools,
                args.overprovisioning, args.report]):
        args.all = True

    # Load data
    print("=" * 78)
    print(f"  AgentCgroup SWE-Bench Data Analysis ({args.dataset})")
    print("=" * 78)
    print(f"\nDataset: {dataset_config['description']}")
    print(f"Loading data from: {BASE_DIR}")

    tasks, progress = load_all_data()

    if not tasks:
        print("Error: No task data loaded. Check the data directory.")
        return 1

    print(f"Loaded {len(tasks)} tasks")

    # Run requested analyses
    dynamics_results = None
    categories_results = None
    tools_results = None
    overprov_results = None

    if args.all or args.dynamics:
        dynamics_results = analyze_dynamics(tasks)

    if args.all or args.categories:
        categories_results = analyze_categories(tasks)

    if args.all or args.tools:
        tools_results = analyze_tools(tasks)

    if args.all or args.overprovisioning:
        overprov_results = analyze_overprovisioning(tasks)

    if args.all or args.report:
        # Run all analyses first if not already done
        if not dynamics_results:
            dynamics_results = analyze_dynamics(tasks)
        if not categories_results:
            categories_results = analyze_categories(tasks)
        if not tools_results:
            tools_results = analyze_tools(tasks)
        if not overprov_results:
            overprov_results = analyze_overprovisioning(tasks)

        generate_report(tasks, dynamics_results, categories_results,
                       tools_results, overprov_results, args.dataset)

    print("\n" + "=" * 78)
    print("  Analysis Complete")
    print("=" * 78)

    return 0


if __name__ == "__main__":
    exit(main())
