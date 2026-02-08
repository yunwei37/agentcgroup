#!/usr/bin/env python3
"""
Extended Insights Analysis for AgentCgroup Paper

This script provides reusable functions to analyze AI agent resource usage patterns
beyond the basic RQ validation. It extracts academically valuable, non-obvious insights
from the experimental traces.

Usage:
    python analyze_extended_insights.py                    # Analyze all datasets
    python analyze_extended_insights.py --haiku            # Analyze Haiku only
    python analyze_extended_insights.py --qwen             # Analyze Qwen only
    python analyze_extended_insights.py --compare          # Compare Haiku vs Qwen

Functions:
    - analyze_disk_and_startup_overhead(): Docker image sizes, pull times, permission fix
    - analyze_transient_bursts(): Peak/avg ratios, spike duration
    - analyze_cpu_memory_correlation(): Correlation between CPU and memory usage
    - analyze_retry_loop_patterns(): Consecutive Bash call patterns
    - analyze_tool_timeline_distribution(): Tool usage across execution phases
    - analyze_local_vs_api_inference(): Compare local (GPU) vs API-based models
    - analyze_concurrency_potential(): Theoretical vs practical concurrency estimates
    - analyze_memory_trajectory(): Aggregated memory patterns across execution phases
    - analyze_tool_semantic_variance(): Same tool type, different resource consumption
"""

import argparse
import json
import os
import glob
import re
import statistics
from datetime import datetime
from collections import defaultdict
from typing import Dict, List, Tuple, Optional, Any

import numpy as np

from filter_valid_tasks import get_valid_task_names

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# =============================================================================
# Utility Functions
# =============================================================================

def parse_mem_mb(mem_str: str) -> float:
    """Parse memory string like '192.4MB / 134.5GB' into MB float."""
    if not mem_str:
        return 0.0
    match = re.match(r"([\d.]+)\s*(KB|MB|GB|TB)", mem_str.split("/")[0].strip())
    if not match:
        return 0.0
    val = float(match.group(1))
    unit = match.group(2)
    if unit == "KB":
        return val / 1024
    elif unit == "MB":
        return val
    elif unit == "GB":
        return val * 1024
    elif unit == "TB":
        return val * 1024 * 1024
    return val


def parse_cpu(cpu_str: str) -> float:
    """Parse CPU string like '18.5%' into float."""
    if not cpu_str:
        return 0.0
    try:
        return float(str(cpu_str).rstrip("%"))
    except (ValueError, TypeError):
        return 0.0


def load_json(path: str) -> Optional[Dict]:
    """Load JSON file, return None on failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def find_attempt_dirs(base_dir: str) -> List[str]:
    """Find attempt directories for valid tasks only (filtered by duration/samples)."""
    valid_names = get_valid_task_names(base_dir)
    attempt_dirs = []
    for name in valid_names:
        full_path = os.path.join(base_dir, name)
        attempts = sorted(glob.glob(os.path.join(full_path, "attempt_*")))
        if attempts:
            attempt_dirs.append(attempts[-1])
    return attempt_dirs


def get_resource_samples(attempt_dir: str) -> Tuple[List[float], List[float]]:
    """Extract CPU and memory samples from resources.json."""
    resources_file = os.path.join(attempt_dir, "resources.json")
    data = load_json(resources_file)
    if not data:
        return [], []

    cpu_samples = []
    mem_samples = []
    for s in data.get("samples", []):
        cpu_samples.append(parse_cpu(s.get("cpu_percent", "")))
        mem_samples.append(parse_mem_mb(s.get("mem_usage", "")))

    return cpu_samples, mem_samples


# =============================================================================
# Analysis Functions
# =============================================================================

def analyze_disk_and_startup_overhead(base_dir: str) -> Dict[str, Any]:
    """
    Analyze Docker image sizes and container startup overhead.

    Returns:
        Dict with image_size, pull_time, and permission_fix_time statistics
    """
    print("\n" + "=" * 70)
    print("DISK AND STARTUP OVERHEAD ANALYSIS")
    print("=" * 70)

    image_sizes = []
    pull_times = []
    perm_times = []

    for attempt_dir in find_attempt_dirs(base_dir):
        results = load_json(os.path.join(attempt_dir, "results.json"))
        if not results:
            continue

        if "image_info" in results and "size_mb" in results["image_info"]:
            image_sizes.append(results["image_info"]["size_mb"])
        if "pull_time" in results:
            pull_times.append(results["pull_time"])
        if "permission_fix_time" in results:
            perm_times.append(results["permission_fix_time"])

    result = {
        "image_size": {},
        "pull_time": {},
        "permission_fix_time": {}
    }

    if image_sizes:
        result["image_size"] = {
            "count": len(image_sizes),
            "min_mb": min(image_sizes),
            "max_mb": max(image_sizes),
            "avg_mb": statistics.mean(image_sizes),
            "median_mb": statistics.median(image_sizes),
            "total_gb": sum(image_sizes) / 1024
        }
        print(f"\nDocker Image Sizes (n={len(image_sizes)}):")
        print(f"  Range: {min(image_sizes):.1f} - {max(image_sizes):.1f} MB")
        print(f"  Avg: {statistics.mean(image_sizes):.1f} MB, Median: {statistics.median(image_sizes):.1f} MB")
        print(f"  Total: {sum(image_sizes)/1024:.1f} GB")

    if pull_times:
        result["pull_time"] = {
            "count": len(pull_times),
            "min_s": min(pull_times),
            "max_s": max(pull_times),
            "avg_s": statistics.mean(pull_times),
            "median_s": statistics.median(pull_times)
        }
        print(f"\nPull Time (n={len(pull_times)}):")
        print(f"  Range: {min(pull_times):.1f} - {max(pull_times):.1f}s")
        print(f"  Avg: {statistics.mean(pull_times):.1f}s, Median: {statistics.median(pull_times):.1f}s")

    if perm_times:
        result["permission_fix_time"] = {
            "count": len(perm_times),
            "min_s": min(perm_times),
            "max_s": max(perm_times),
            "avg_s": statistics.mean(perm_times),
            "median_s": statistics.median(perm_times)
        }
        print(f"\nPermission Fix Time (n={len(perm_times)}):")
        print(f"  Range: {min(perm_times):.1f} - {max(perm_times):.1f}s")
        print(f"  Avg: {statistics.mean(perm_times):.1f}s, Median: {statistics.median(perm_times):.1f}s")

    return result


def analyze_transient_bursts(base_dir: str) -> Dict[str, Any]:
    """
    Analyze transient burst characteristics - peak/avg ratios and spike duration.

    Returns:
        Dict with burst statistics per task
    """
    print("\n" + "=" * 70)
    print("TRANSIENT BURST ANALYSIS")
    print("=" * 70)

    task_bursts = []

    for attempt_dir in find_attempt_dirs(base_dir):
        task_name = os.path.basename(os.path.dirname(attempt_dir))
        resources = load_json(os.path.join(attempt_dir, "resources.json"))
        if not resources or "summary" not in resources:
            continue

        summary = resources["summary"]
        mem_stats = summary.get("memory_mb", {})

        peak_mem = mem_stats.get("max", 0)
        avg_mem = mem_stats.get("avg", 0)

        if avg_mem > 0 and peak_mem > 0:
            overprov_factor = peak_mem / avg_mem

            # Find spike duration (how many consecutive samples near peak)
            samples = resources.get("samples", [])
            mem_values = [parse_mem_mb(s.get("mem_usage", "")) for s in samples]

            spike_threshold = peak_mem * 0.8
            spike_duration = 0
            max_spike_duration = 0
            for mem in mem_values:
                if mem >= spike_threshold:
                    spike_duration += 1
                    max_spike_duration = max(max_spike_duration, spike_duration)
                else:
                    spike_duration = 0

            task_bursts.append({
                "task": task_name,
                "peak_mb": peak_mem,
                "avg_mb": avg_mem,
                "overprov_factor": overprov_factor,
                "spike_duration_samples": max_spike_duration
            })

    if not task_bursts:
        print("  No data available.")
        return {}

    # Find extreme bursts
    task_bursts.sort(key=lambda x: x["overprov_factor"], reverse=True)

    print(f"\nAnalyzed {len(task_bursts)} tasks")
    print("\nTop 5 Most Extreme Bursts:")
    for i, burst in enumerate(task_bursts[:5], 1):
        print(f"  {i}. {burst['task']}")
        print(f"     Peak: {burst['peak_mb']:.0f}MB, Avg: {burst['avg_mb']:.0f}MB")
        print(f"     Overprov Factor: {burst['overprov_factor']:.1f}x")
        print(f"     Spike Duration: ~{burst['spike_duration_samples']}s")

    overprov_factors = [b["overprov_factor"] for b in task_bursts]
    result = {
        "task_count": len(task_bursts),
        "overprov_factor": {
            "min": min(overprov_factors),
            "max": max(overprov_factors),
            "avg": statistics.mean(overprov_factors),
            "median": statistics.median(overprov_factors)
        },
        "top_bursts": task_bursts[:5]
    }

    print(f"\nOverall Overprovisioning Factor:")
    print(f"  Range: {min(overprov_factors):.1f}x - {max(overprov_factors):.1f}x")
    print(f"  Avg: {statistics.mean(overprov_factors):.1f}x, Median: {statistics.median(overprov_factors):.1f}x")

    return result


def analyze_cpu_memory_correlation(base_dir: str) -> Dict[str, Any]:
    """
    Analyze correlation between CPU and memory usage.

    Returns:
        Dict with correlation statistics per task
    """
    print("\n" + "=" * 70)
    print("CPU-MEMORY CORRELATION ANALYSIS")
    print("=" * 70)

    correlations = []

    for attempt_dir in find_attempt_dirs(base_dir):
        task_name = os.path.basename(os.path.dirname(attempt_dir))
        cpu_samples, mem_samples = get_resource_samples(attempt_dir)

        if len(cpu_samples) >= 10 and len(mem_samples) >= 10:
            # Calculate Pearson correlation
            try:
                corr = np.corrcoef(cpu_samples, mem_samples)[0, 1]
                if not np.isnan(corr):
                    correlations.append({
                        "task": task_name,
                        "correlation": corr,
                        "sample_count": len(cpu_samples)
                    })
            except:
                pass

    if not correlations:
        print("  No data available.")
        return {}

    corr_values = [c["correlation"] for c in correlations]

    result = {
        "task_count": len(correlations),
        "correlation": {
            "min": min(corr_values),
            "max": max(corr_values),
            "avg": statistics.mean(corr_values),
            "median": statistics.median(corr_values)
        },
        "positive_count": sum(1 for c in corr_values if c > 0.5),
        "negative_count": sum(1 for c in corr_values if c < -0.5),
        "neutral_count": sum(1 for c in corr_values if -0.5 <= c <= 0.5)
    }

    print(f"\nAnalyzed {len(correlations)} tasks")
    print(f"\nCorrelation Distribution:")
    print(f"  Strong Positive (>0.5): {result['positive_count']} tasks")
    print(f"  Neutral (-0.5 to 0.5): {result['neutral_count']} tasks")
    print(f"  Strong Negative (<-0.5): {result['negative_count']} tasks")
    print(f"\nCorrelation Statistics:")
    print(f"  Range: {min(corr_values):.2f} - {max(corr_values):.2f}")
    print(f"  Avg: {statistics.mean(corr_values):.2f}, Median: {statistics.median(corr_values):.2f}")

    if result["positive_count"] > len(correlations) * 0.7:
        print("\n[INSIGHT] CPU and memory are strongly positively correlated")
        print("  This challenges the 'thinking=low, execution=high' model")

    return result


def analyze_retry_loop_patterns(base_dir: str) -> Dict[str, Any]:
    """
    Analyze retry loop patterns - consecutive Bash calls indicating iterative debugging.

    Returns:
        Dict with retry pattern statistics per task
    """
    print("\n" + "=" * 70)
    print("RETRY LOOP PATTERN ANALYSIS")
    print("=" * 70)

    task_patterns = []

    for attempt_dir in find_attempt_dirs(base_dir):
        task_name = os.path.basename(os.path.dirname(attempt_dir))
        tool_calls = load_json(os.path.join(attempt_dir, "tool_calls.json"))
        if not tool_calls:
            continue

        # Extract tool sequence
        tools = [call.get("tool", "Unknown") for call in tool_calls]

        # Count consecutive Bash calls (retry groups of 3+)
        retry_groups = 0
        consecutive_bash = 0
        max_consecutive = 0
        bash_density = sum(1 for t in tools if t == "Bash") / len(tools) if tools else 0

        for tool in tools:
            if tool == "Bash":
                consecutive_bash += 1
                max_consecutive = max(max_consecutive, consecutive_bash)
            else:
                if consecutive_bash >= 3:
                    retry_groups += 1
                consecutive_bash = 0

        # Check final group
        if consecutive_bash >= 3:
            retry_groups += 1

        task_patterns.append({
            "task": task_name,
            "total_tools": len(tools),
            "bash_count": sum(1 for t in tools if t == "Bash"),
            "bash_density": bash_density,
            "retry_groups": retry_groups,
            "max_consecutive_bash": max_consecutive
        })

    if not task_patterns:
        print("  No data available.")
        return {}

    # Sort by retry groups
    task_patterns.sort(key=lambda x: x["retry_groups"], reverse=True)

    print(f"\nAnalyzed {len(task_patterns)} tasks")
    print("\nTop 5 Tasks with Most Retry Loops:")
    for i, pattern in enumerate(task_patterns[:5], 1):
        print(f"  {i}. {pattern['task']}")
        print(f"     Retry Groups: {pattern['retry_groups']}, Max Consecutive Bash: {pattern['max_consecutive_bash']}")
        print(f"     Bash Density: {pattern['bash_density']*100:.1f}% ({pattern['bash_count']}/{pattern['total_tools']})")

    retry_counts = [p["retry_groups"] for p in task_patterns]
    result = {
        "task_count": len(task_patterns),
        "retry_groups": {
            "min": min(retry_counts),
            "max": max(retry_counts),
            "avg": statistics.mean(retry_counts),
            "total": sum(retry_counts)
        },
        "high_retry_tasks": [p for p in task_patterns if p["retry_groups"] >= 10],
        "top_patterns": task_patterns[:5]
    }

    print(f"\nRetry Group Statistics:")
    print(f"  Total Retry Groups: {sum(retry_counts)}")
    print(f"  Avg per Task: {statistics.mean(retry_counts):.1f}")
    print(f"  Tasks with 10+ Retry Groups: {len(result['high_retry_tasks'])}")

    return result


def analyze_tool_timeline_distribution(base_dir: str) -> Dict[str, Any]:
    """
    Analyze how tool usage is distributed across execution time.

    Returns:
        Dict with tool usage per decile of execution
    """
    print("\n" + "=" * 70)
    print("TOOL TIMELINE DISTRIBUTION ANALYSIS")
    print("=" * 70)

    n_bins = 10
    tool_types = ["Bash", "Read", "Edit", "Grep", "Task", "Write", "TodoWrite"]
    timeline_data = {t: [0] * n_bins for t in tool_types}
    timeline_data["Other"] = [0] * n_bins

    tasks_analyzed = 0

    for attempt_dir in find_attempt_dirs(base_dir):
        tool_calls = load_json(os.path.join(attempt_dir, "tool_calls.json"))
        if not tool_calls or len(tool_calls) < 2:
            continue

        # Parse timestamps
        timestamps = []
        for call in tool_calls:
            ts_str = call.get("timestamp")
            if ts_str:
                try:
                    ts_str = ts_str.replace("Z", "+00:00")
                    ts = datetime.fromisoformat(ts_str)
                    timestamps.append((ts, call.get("tool", "Unknown")))
                except:
                    pass

        if len(timestamps) < 2:
            continue

        tasks_analyzed += 1
        first_ts = min(t[0] for t in timestamps)
        last_ts = max(t[0] for t in timestamps)
        span = (last_ts - first_ts).total_seconds()

        if span <= 0:
            continue

        for ts, tool in timestamps:
            norm_pos = (ts - first_ts).total_seconds() / span
            bin_idx = min(int(norm_pos * n_bins), n_bins - 1)
            if tool in timeline_data:
                timeline_data[tool][bin_idx] += 1
            else:
                timeline_data["Other"][bin_idx] += 1

    if tasks_analyzed == 0:
        print("  No data available.")
        return {}

    print(f"\nAnalyzed {tasks_analyzed} tasks")
    print("\nTool Distribution by Execution Phase:")
    print(f"{'Tool':<12} {'Early(0-30%)':<15} {'Mid(30-70%)':<15} {'Late(70-100%)':<15}")
    print("-" * 60)

    result = {"tasks_analyzed": tasks_analyzed, "timeline": {}}

    for tool in tool_types + ["Other"]:
        counts = timeline_data[tool]
        total = sum(counts)
        if total == 0:
            continue

        early = sum(counts[:3]) / total * 100
        mid = sum(counts[3:7]) / total * 100
        late = sum(counts[7:]) / total * 100

        result["timeline"][tool] = {
            "early_pct": early,
            "mid_pct": mid,
            "late_pct": late,
            "total": total,
            "bins": counts
        }

        print(f"{tool:<12} {early:>12.1f}% {mid:>14.1f}% {late:>14.1f}%")

    # Identify patterns
    print("\n[INSIGHTS]")
    if result["timeline"].get("Read", {}).get("early_pct", 0) > 40:
        print("  - Read operations cluster in early phase (code understanding)")
    if result["timeline"].get("Bash", {}).get("mid_pct", 0) > 35:
        print("  - Bash operations cluster in middle-late phase (testing)")
    if result["timeline"].get("Edit", {}).get("mid_pct", 0) > 30:
        print("  - Edit operations distributed across phases (iterative modification)")

    return result


def analyze_local_vs_api_inference(haiku_dir: str, qwen_dir: str) -> Dict[str, Any]:
    """
    Compare resource usage between API-based (Haiku) and local (Qwen) inference.

    Returns:
        Dict with comparison statistics
    """
    print("\n" + "=" * 70)
    print("LOCAL VS API INFERENCE COMPARISON")
    print("=" * 70)

    def collect_stats(base_dir: str) -> Dict[str, Any]:
        cpu_all = []
        mem_all = []
        high_cpu_count = 0
        total_samples = 0

        for attempt_dir in find_attempt_dirs(base_dir):
            cpu_samples, mem_samples = get_resource_samples(attempt_dir)
            cpu_all.extend(cpu_samples)
            mem_all.extend(mem_samples)

        if not cpu_all:
            return {}

        high_cpu_count = sum(1 for c in cpu_all if c > 50)
        very_high_cpu = sum(1 for c in cpu_all if c > 100)

        return {
            "total_samples": len(cpu_all),
            "cpu_avg": statistics.mean(cpu_all),
            "cpu_max": max(cpu_all),
            "mem_avg": statistics.mean(mem_all) if mem_all else 0,
            "mem_max": max(mem_all) if mem_all else 0,
            "high_cpu_pct": high_cpu_count / len(cpu_all) * 100,
            "very_high_cpu_pct": very_high_cpu / len(cpu_all) * 100
        }

    haiku_stats = collect_stats(haiku_dir) if os.path.exists(haiku_dir) else {}
    qwen_stats = collect_stats(qwen_dir) if os.path.exists(qwen_dir) else {}

    result = {
        "haiku": haiku_stats,
        "qwen": qwen_stats
    }

    if haiku_stats:
        print(f"\nHaiku (API-based inference):")
        print(f"  Samples: {haiku_stats['total_samples']}")
        print(f"  CPU: Avg={haiku_stats['cpu_avg']:.1f}%, Max={haiku_stats['cpu_max']:.1f}%")
        print(f"  Memory: Avg={haiku_stats['mem_avg']:.1f}MB, Max={haiku_stats['mem_max']:.1f}MB")
        print(f"  Samples with CPU > 50%: {haiku_stats['high_cpu_pct']:.1f}%")
        print(f"  Samples with CPU > 100%: {haiku_stats['very_high_cpu_pct']:.1f}%")

    if qwen_stats:
        print(f"\nQwen (Local GPU inference):")
        print(f"  Samples: {qwen_stats['total_samples']}")
        print(f"  CPU: Avg={qwen_stats['cpu_avg']:.1f}%, Max={qwen_stats['cpu_max']:.1f}%")
        print(f"  Memory: Avg={qwen_stats['mem_avg']:.1f}MB, Max={qwen_stats['mem_max']:.1f}MB")
        print(f"  Samples with CPU > 50%: {qwen_stats['high_cpu_pct']:.1f}%")
        print(f"  Samples with CPU > 100%: {qwen_stats['very_high_cpu_pct']:.1f}%")

    if haiku_stats and qwen_stats and qwen_stats['cpu_avg'] > 0:
        cpu_ratio = haiku_stats['cpu_avg'] / qwen_stats['cpu_avg']
        result["cpu_ratio"] = cpu_ratio
        print(f"\n[KEY INSIGHT]")
        print(f"  CPU Ratio (Haiku/Qwen): {cpu_ratio:.1f}x")
        print(f"  Reason: Local inference uses GPU, container only runs tools")
        print(f"  Implication: Local model shifts bottleneck from network to GPU")
        print(f"               API model needs more CPU for network I/O")

    return result


def analyze_concurrency_potential(base_dir: str) -> Dict[str, Any]:
    """
    Analyze theoretical vs practical concurrency potential.

    Returns:
        Dict with concurrency estimates
    """
    print("\n" + "=" * 70)
    print("CONCURRENCY POTENTIAL ANALYSIS")
    print("=" * 70)

    task_stats = []

    for attempt_dir in find_attempt_dirs(base_dir):
        task_name = os.path.basename(os.path.dirname(attempt_dir))
        resources = load_json(os.path.join(attempt_dir, "resources.json"))
        if not resources or "summary" not in resources:
            continue

        summary = resources["summary"]
        cpu_stats = summary.get("cpu_percent", {})
        mem_stats = summary.get("memory_mb", {})

        task_stats.append({
            "task": task_name,
            "cpu_avg": cpu_stats.get("avg", 0),
            "cpu_max": cpu_stats.get("max", 0),
            "mem_avg": mem_stats.get("avg", 0),
            "mem_max": mem_stats.get("max", 0)
        })

    if not task_stats:
        print("  No data available.")
        return {}

    # Calculate concurrency estimates
    avg_cpu = statistics.mean([t["cpu_avg"] for t in task_stats if t["cpu_avg"] > 0])
    max_cpu = max([t["cpu_max"] for t in task_stats if t["cpu_max"] > 0])
    avg_mem = statistics.mean([t["mem_avg"] for t in task_stats if t["mem_avg"] > 0])
    max_mem = max([t["mem_max"] for t in task_stats if t["mem_max"] > 0])

    # Theoretical concurrency (based on average)
    theoretical_cpu = 100.0 / avg_cpu if avg_cpu > 0 else 0

    # Practical concurrency (based on peak)
    practical_cpu = 100.0 / max_cpu if max_cpu > 0 else 0

    result = {
        "task_count": len(task_stats),
        "cpu": {
            "avg": avg_cpu,
            "max": max_cpu,
            "theoretical_concurrency": theoretical_cpu,
            "practical_concurrency": practical_cpu,
            "concurrency_gap": theoretical_cpu / practical_cpu if practical_cpu > 0 else 0
        },
        "memory": {
            "avg_mb": avg_mem,
            "max_mb": max_mem
        }
    }

    print(f"\nAnalyzed {len(task_stats)} tasks")
    print(f"\nCPU Utilization:")
    print(f"  Average: {avg_cpu:.1f}%")
    print(f"  Peak: {max_cpu:.1f}%")
    print(f"\nConcurrency Estimates (at 100% CPU budget):")
    print(f"  Theoretical (based on avg): {theoretical_cpu:.1f} instances")
    print(f"  Practical (based on peak): {practical_cpu:.1f} instances")
    print(f"  Concurrency Gap: {result['cpu']['concurrency_gap']:.1f}x")
    print(f"\n[INSIGHT]")
    print(f"  Dynamic resource control could enable {result['cpu']['concurrency_gap']:.1f}x more concurrency")

    return result


def analyze_memory_trajectory(base_dir: str) -> Dict[str, Any]:
    """
    Analyze aggregated memory trajectory patterns across execution phases.

    Returns:
        Dict with memory statistics per phase
    """
    print("\n" + "=" * 70)
    print("MEMORY TRAJECTORY ANALYSIS")
    print("=" * 70)

    n_points = 100
    all_trajectories = []

    for attempt_dir in find_attempt_dirs(base_dir):
        _, mem_samples = get_resource_samples(attempt_dir)

        if len(mem_samples) >= 10:
            # Normalize to n_points
            x_orig = np.linspace(0, 1, len(mem_samples))
            x_new = np.linspace(0, 1, n_points)
            interp = np.interp(x_new, x_orig, mem_samples)
            all_trajectories.append(interp)

    if not all_trajectories:
        print("  No data available.")
        return {}

    mem_matrix = np.array(all_trajectories)

    # Calculate statistics per phase
    early_phase = mem_matrix[:, :33]  # 0-33%
    mid_phase = mem_matrix[:, 33:66]   # 33-66%
    late_phase = mem_matrix[:, 66:]    # 66-100%

    result = {
        "task_count": len(all_trajectories),
        "phases": {
            "early": {
                "mean": float(np.mean(early_phase)),
                "std": float(np.std(early_phase)),
                "max": float(np.max(early_phase))
            },
            "mid": {
                "mean": float(np.mean(mid_phase)),
                "std": float(np.std(mid_phase)),
                "max": float(np.max(mid_phase))
            },
            "late": {
                "mean": float(np.mean(late_phase)),
                "std": float(np.std(late_phase)),
                "max": float(np.max(late_phase))
            }
        },
        "overall": {
            "mean": list(np.mean(mem_matrix, axis=0)),
            "p25": list(np.percentile(mem_matrix, 25, axis=0)),
            "p75": list(np.percentile(mem_matrix, 75, axis=0))
        }
    }

    print(f"\nAnalyzed {len(all_trajectories)} tasks")
    print(f"\nMemory by Execution Phase:")
    print(f"{'Phase':<15} {'Mean (MB)':<12} {'Std (MB)':<12} {'Max (MB)':<12}")
    print("-" * 50)
    for phase_name, phase in [("Early (0-33%)", "early"), ("Mid (33-66%)", "mid"), ("Late (66-100%)", "late")]:
        stats = result["phases"][phase]
        print(f"{phase_name:<15} {stats['mean']:>10.1f} {stats['std']:>11.1f} {stats['max']:>11.1f}")

    # Identify trend
    early_mean = result["phases"]["early"]["mean"]
    late_mean = result["phases"]["late"]["mean"]

    if late_mean > early_mean * 1.2:
        print(f"\n[INSIGHT] Memory increases from early to late phase")
        print(f"  Early: {early_mean:.1f}MB -> Late: {late_mean:.1f}MB ({(late_mean/early_mean-1)*100:.1f}% increase)")
        print(f"  Suggests: exploration -> accumulation pattern")

    return result


def analyze_tool_semantic_variance(base_dir: str) -> Dict[str, Any]:
    """
    Analyze how the same tool type produces different resource consumption
    based on task category (semantic variance).

    Returns:
        Dict with resource variance by category for same tool type
    """
    print("\n" + "=" * 70)
    print("TOOL SEMANTIC VARIANCE ANALYSIS")
    print("=" * 70)

    category_resources = defaultdict(lambda: {"peak_mem": [], "avg_cpu": []})

    for attempt_dir in find_attempt_dirs(base_dir):
        task_name = os.path.basename(os.path.dirname(attempt_dir))

        # Extract category from task name
        parts = task_name.split("_")
        if len(parts) >= 2 and parts[-1] in ("Easy", "Medium", "Hard"):
            category = "_".join(parts[:-1])
        else:
            category = "Other"

        resources = load_json(os.path.join(attempt_dir, "resources.json"))
        if not resources or "summary" not in resources:
            continue

        summary = resources["summary"]
        peak_mem = summary.get("memory_mb", {}).get("max", 0)
        avg_cpu = summary.get("cpu_percent", {}).get("avg", 0)

        if peak_mem > 0:
            category_resources[category]["peak_mem"].append(peak_mem)
            category_resources[category]["avg_cpu"].append(avg_cpu)

    if not category_resources:
        print("  No data available.")
        return {}

    result = {"categories": {}}

    print(f"\nResource Usage by Task Category (Same Bash Tool, Different Semantics):")
    print(f"{'Category':<20} {'Avg Peak Mem':<15} {'Avg CPU':<12} {'Mem Variance':<12}")
    print("-" * 60)

    for category in sorted(category_resources.keys()):
        data = category_resources[category]
        if data["peak_mem"]:
            avg_peak_mem = statistics.mean(data["peak_mem"])
            avg_cpu = statistics.mean(data["avg_cpu"]) if data["avg_cpu"] else 0
            mem_std = statistics.stdev(data["peak_mem"]) if len(data["peak_mem"]) > 1 else 0

            result["categories"][category] = {
                "avg_peak_mem": avg_peak_mem,
                "avg_cpu": avg_cpu,
                "mem_std": mem_std,
                "count": len(data["peak_mem"])
            }

            print(f"{category:<20} {avg_peak_mem:>12.1f}MB {avg_cpu:>10.1f}% {mem_std:>11.1f}")

    # Calculate variance ratio
    if len(result["categories"]) >= 2:
        all_mems = [c["avg_peak_mem"] for c in result["categories"].values()]
        variance_ratio = max(all_mems) / min(all_mems) if min(all_mems) > 0 else 0
        result["variance_ratio"] = variance_ratio

        print(f"\n[INSIGHT]")
        print(f"  Memory variance ratio across categories: {variance_ratio:.1f}x")
        print(f"  Same tool (Bash) produces vastly different resource consumption")
        print(f"  Resource demand determined by semantics, not tool type")

    return result


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Extended Insights Analysis for AgentCgroup")
    parser.add_argument("--haiku", action="store_true", help="Analyze Haiku dataset only")
    parser.add_argument("--qwen", action="store_true", help="Analyze Qwen dataset only")
    parser.add_argument("--compare", action="store_true", help="Compare Haiku vs Qwen")
    parser.add_argument("--output", default=None, help="Output JSON file for results")
    args = parser.parse_args()

    haiku_dir = os.path.join(SCRIPT_DIR, "..", "experiments", "all_images_haiku")
    qwen_dir = os.path.join(SCRIPT_DIR, "..", "experiments", "all_images_local")

    all_results = {}

    # Determine which datasets to analyze
    datasets = []
    if args.haiku:
        datasets = [("Haiku", haiku_dir)]
    elif args.qwen:
        datasets = [("Qwen", qwen_dir)]
    else:
        datasets = [("Haiku", haiku_dir), ("Qwen", qwen_dir)]

    for name, base_dir in datasets:
        if not os.path.exists(base_dir):
            print(f"Warning: {name} dataset not found at {base_dir}")
            continue

        print("\n" + "#" * 70)
        print(f"# Analyzing: {name}")
        print("#" * 70)

        results = {}
        results["disk_overhead"] = analyze_disk_and_startup_overhead(base_dir)
        results["transient_bursts"] = analyze_transient_bursts(base_dir)
        results["cpu_memory_correlation"] = analyze_cpu_memory_correlation(base_dir)
        results["retry_patterns"] = analyze_retry_loop_patterns(base_dir)
        results["tool_timeline"] = analyze_tool_timeline_distribution(base_dir)
        results["concurrency"] = analyze_concurrency_potential(base_dir)
        results["memory_trajectory"] = analyze_memory_trajectory(base_dir)
        results["tool_semantic_variance"] = analyze_tool_semantic_variance(base_dir)

        all_results[name] = results

    # Compare if requested or both datasets analyzed
    if args.compare or (not args.haiku and not args.qwen):
        if os.path.exists(haiku_dir) and os.path.exists(qwen_dir):
            all_results["comparison"] = analyze_local_vs_api_inference(haiku_dir, qwen_dir)

    # Save results if output specified
    if args.output:
        with open(args.output, "w") as f:
            # Convert numpy types to Python types for JSON serialization
            def convert(obj):
                if isinstance(obj, np.ndarray):
                    return obj.tolist()
                elif isinstance(obj, (np.float32, np.float64)):
                    return float(obj)
                elif isinstance(obj, (np.int32, np.int64)):
                    return int(obj)
                return obj

            json.dump(all_results, f, indent=2, default=convert)
        print(f"\nResults saved to: {args.output}")

    print("\n" + "=" * 70)
    print("ANALYSIS COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
