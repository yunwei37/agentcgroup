#!/usr/bin/env python3
"""
Analyze the proportion of tool call time in total execution time.
Reads tool_calls.json, results.json, and resources.json from each task directory.
Generates text report + 14 PNG charts.

Usage:
    python analyze_tool_time_ratio.py                          # defaults: all_images_local -> qwen3_figures
    python analyze_tool_time_ratio.py --data-dir /path/to/exp --figures-dir /path/to/figs
"""

import argparse
import json
import glob
import os
import re
import statistics
from datetime import datetime
from collections import defaultdict

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

from filter_valid_tasks import get_valid_task_names

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CHART_DPI = 150
COLORS = {"success": "#2ecc71", "failure": "#e74c3c", "neutral": "#3498db"}


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def parse_iso(ts_str):
    """Parse ISO format timestamp, handling Z suffix and fractional seconds."""
    if ts_str is None:
        return None
    ts_str = ts_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return None


def load_json(path):
    """Load JSON file, return None on failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def get_task_name_from_dir(dirname):
    """Extract task identifier from dir name.
    Handles 'task_N_instance_id' -> 'instance_id' and plain dir names like 'CLI_Tools_Easy'.
    """
    if re.match(r"^task_\d+_", dirname):
        parts = dirname.split("_", 2)
        if len(parts) >= 3:
            return parts[2]
    return dirname


def extract_repo_name(task_key):
    """Extract repo name from task key like '12rambau__sepal_ui-411' -> '12rambau/sepal_ui'."""
    parts = task_key.rsplit("-", 1)
    if len(parts) == 2:
        repo_part = parts[0]
        return repo_part.replace("__", "/")
    return task_key


def parse_mem_mb(mem_str):
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


def parse_cpu(cpu_str):
    """Parse CPU string like '18.5%' into float."""
    if not cpu_str:
        return 0.0
    try:
        return float(str(cpu_str).rstrip("%"))
    except (ValueError, TypeError):
        return 0.0


def categorize_bash_command(cmd):
    """Categorize a bash command into a human-readable category."""
    cmd_lower = cmd.strip().lower()
    if re.search(r"\bpytest\b|\bpython\s+-m\s+pytest\b|\btox\b|\bnose\b|\bunittest\b", cmd_lower):
        return "Test Execution"
    if re.search(r"\bgit\s+(diff|log|status|show|add|commit|checkout|stash|branch|reset)\b", cmd_lower):
        return "Git Operations"
    if re.search(r"\bpip\s+install\b|\bconda\s+install\b|\bapt\b|\byum\b", cmd_lower):
        return "Package Install"
    if re.search(r"\bls\b|\bfind\b|\btree\b|\bwc\b|\bdu\b|\bdf\b", cmd_lower):
        return "File Exploration"
    if re.search(r"\bpython\s+-c\b|\bpython3\s+-c\b", cmd_lower):
        return "Python Snippet"
    if re.search(r"\bpython\b|\bpython3\b", cmd_lower):
        return "Python Run"
    if re.search(r"\bcat\b|\bhead\b|\btail\b|\bgrep\b|\bsed\b|\bawk\b", cmd_lower):
        return "Text Processing"
    if re.search(r"\bcd\b|\bsource\b|\bexport\b|\bchmod\b|\bmkdir\b", cmd_lower):
        return "Shell/Environment"
    return "Other"


def save_chart(fig, name, chart_dir):
    """Save a matplotlib figure to the specified directory."""
    os.makedirs(chart_dir, exist_ok=True)
    path = os.path.join(chart_dir, name)
    fig.savefig(path, dpi=CHART_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  [CHART] Saved: {path}")


def find_best_attempt_dir(task_dir):
    """Find the best attempt directory (highest numbered) for a task."""
    attempts = sorted(glob.glob(os.path.join(task_dir, "attempt_*")))
    if attempts:
        return attempts[-1]
    return os.path.join(task_dir, "attempt_1")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Analyze tool call time ratios from SWE-bench experiments")
    parser.add_argument("--data-dir", default=None,
                        help="Experiment data directory (default: experiments/all_images_local)")
    parser.add_argument("--figures-dir", default=None,
                        help="Output directory for charts (default: analysis/qwen3_figures)")
    args = parser.parse_args()

    base_dir = args.data_dir or os.path.join(SCRIPT_DIR, "..", "experiments", "all_images_local")
    base_dir = os.path.abspath(base_dir)
    chart_dir = args.figures_dir or os.path.join(SCRIPT_DIR, "qwen3_figures")
    chart_dir = os.path.abspath(chart_dir)

    print(f"  Data directory:    {base_dir}")
    print(f"  Figures directory: {chart_dir}")
    print()

    # -------------------------------------------------------------------------
    # 1. Load progress.json for success/failure mapping
    # -------------------------------------------------------------------------
    progress = load_json(os.path.join(base_dir, "progress.json"))
    success_map = {}
    if progress and "results" in progress:
        for task_name, info in progress["results"].items():
            success_map[task_name] = info.get("success", False)

    # -------------------------------------------------------------------------
    # 2. Discover valid task directories (filtered by duration/samples)
    # -------------------------------------------------------------------------
    valid_names = get_valid_task_names(base_dir)
    task_dirs = [os.path.join(base_dir, name) for name in valid_names]
    print(f"  Valid tasks after filtering: {len(task_dirs)}")

    all_data = []
    tasks_loaded = 0
    tasks_skipped = 0

    for task_dir in task_dirs:
        dir_name = os.path.basename(task_dir)
        task_name = get_task_name_from_dir(dir_name)

        attempt_dir = find_best_attempt_dir(task_dir)
        tool_calls_path = os.path.join(attempt_dir, "tool_calls.json")
        results_path = os.path.join(attempt_dir, "results.json")
        resources_path = os.path.join(attempt_dir, "resources.json")

        tool_calls = load_json(tool_calls_path)
        results = load_json(results_path)

        if tool_calls is None or results is None:
            tasks_skipped += 1
            continue

        claude_time = results.get("claude_time")
        if claude_time is None or claude_time <= 0:
            tasks_skipped += 1
            continue

        tasks_loaded += 1

        # --- Original tool time computation ---
        tool_total_time = 0.0
        valid_calls = 0
        tool_call_times = []

        for call in tool_calls:
            ts_start = parse_iso(call.get("timestamp"))
            ts_end = parse_iso(call.get("end_timestamp"))
            if ts_start is None or ts_end is None:
                continue
            duration = (ts_end - ts_start).total_seconds()
            if duration < 0:
                continue
            valid_calls += 1
            tool_total_time += duration
            tool_call_times.append(duration)

        thinking_time = claude_time - tool_total_time
        tool_ratio = (tool_total_time / claude_time * 100) if claude_time > 0 else 0
        thinking_ratio = (thinking_time / claude_time * 100) if claude_time > 0 else 0

        # --- Tool sequence & transitions ---
        tool_sequence = [call.get("tool", "Unknown") for call in tool_calls]
        transitions = []
        for i in range(len(tool_sequence) - 1):
            transitions.append((tool_sequence[i], tool_sequence[i + 1]))

        # --- Bash command categorization ---
        bash_categories = []
        bash_cat_times = defaultdict(float)
        bash_cat_counts = defaultdict(int)
        for call in tool_calls:
            if call.get("tool") == "Bash":
                cmd = call.get("input", {}).get("command", "")
                cat = categorize_bash_command(cmd)
                bash_categories.append(cat)
                ts_s = parse_iso(call.get("timestamp"))
                ts_e = parse_iso(call.get("end_timestamp"))
                if ts_s and ts_e:
                    dur = (ts_e - ts_s).total_seconds()
                    if dur >= 0:
                        bash_cat_times[cat] += dur
                        bash_cat_counts[cat] += 1

        # --- Per-tool time breakdown ---
        per_tool_time = defaultdict(float)
        per_tool_count = defaultdict(int)
        for call in tool_calls:
            tn = call.get("tool", "Unknown")
            ts_s = parse_iso(call.get("timestamp"))
            ts_e = parse_iso(call.get("end_timestamp"))
            if ts_s and ts_e:
                dur = (ts_e - ts_s).total_seconds()
                if dur >= 0:
                    per_tool_time[tn] += dur
                    per_tool_count[tn] += 1

        # --- Tool diversity ---
        unique_tools = len(set(call.get("tool") for call in tool_calls if call.get("tool")))

        # --- Tool timeline (normalized 0..1) ---
        tool_timeline = []
        if tool_calls:
            all_ts = [parse_iso(c.get("timestamp")) for c in tool_calls]
            all_ts = [t for t in all_ts if t is not None]
            if len(all_ts) >= 2:
                first_ts = min(all_ts)
                last_ts = max(all_ts)
                span = (last_ts - first_ts).total_seconds()
                if span > 0:
                    for call in tool_calls:
                        ts = parse_iso(call.get("timestamp"))
                        if ts:
                            norm = (ts - first_ts).total_seconds() / span
                            tool_timeline.append((norm, call.get("tool", "Unknown")))

        # --- Results-based fields ---
        image_size_mb = results.get("image_info", {}).get("size_mb", 0)
        pull_time_val = results.get("pull_time", 0)
        perm_fix_time = results.get("permission_fix_time", 0)
        total_time_val = results.get("total_time", 0)
        output_text = results.get("claude_output", {}).get("stdout", "")
        output_length = len(output_text)

        # --- Resource data ---
        resources = load_json(resources_path)
        peak_mem_mb = 0.0
        avg_cpu = 0.0
        mem_trajectory = []
        cpu_trajectory = []
        peak_mem_position = 0.5

        if resources and "samples" in resources:
            samples = resources["samples"]
            for s in samples:
                mem_trajectory.append(parse_mem_mb(s.get("mem_usage", "")))
                cpu_trajectory.append(parse_cpu(s.get("cpu_percent", "")))
            if mem_trajectory:
                peak_mem_mb = max(mem_trajectory)
                peak_idx = mem_trajectory.index(peak_mem_mb)
                peak_mem_position = peak_idx / max(len(mem_trajectory) - 1, 1)
            if cpu_trajectory:
                avg_cpu = statistics.mean(cpu_trajectory)
        if resources and "summary" in resources:
            summary = resources["summary"]
            peak_mem_mb = max(peak_mem_mb, summary.get("memory_mb", {}).get("max", 0))
            if not cpu_trajectory:
                avg_cpu = summary.get("cpu_percent", {}).get("avg", 0)

        # --- Repo name ---
        repo_name = extract_repo_name(task_name)

        all_data.append({
            "dir_name": dir_name,
            "task_name": task_name,
            "claude_time": claude_time,
            "tool_time": tool_total_time,
            "thinking_time": thinking_time,
            "tool_ratio": tool_ratio,
            "thinking_ratio": thinking_ratio,
            "valid_calls": valid_calls,
            "total_calls": len(tool_calls),
            "success": success_map.get(task_name, None),
            # New fields
            "tool_sequence": tool_sequence,
            "transitions": transitions,
            "bash_categories": bash_categories,
            "bash_cat_times": dict(bash_cat_times),
            "bash_cat_counts": dict(bash_cat_counts),
            "per_tool_time": dict(per_tool_time),
            "per_tool_count": dict(per_tool_count),
            "unique_tools": unique_tools,
            "tool_timeline": tool_timeline,
            "image_size_mb": image_size_mb,
            "pull_time": pull_time_val,
            "permission_fix_time": perm_fix_time,
            "total_time": total_time_val,
            "output_length": output_length,
            "peak_mem_mb": peak_mem_mb,
            "avg_cpu": avg_cpu,
            "mem_trajectory": mem_trajectory,
            "cpu_trajectory": cpu_trajectory,
            "peak_mem_position": peak_mem_position,
            "repo_name": repo_name,
        })

    # =========================================================================
    # REPORT (original sections preserved)
    # =========================================================================
    sep = "=" * 78
    sep2 = "-" * 78

    print(sep)
    print("       TOOL CALL TIME RATIO ANALYSIS")
    print(sep)
    print(f"  Tasks loaded:  {tasks_loaded}")
    print(f"  Tasks skipped: {tasks_skipped}")
    print()

    if not all_data:
        print("  No data to analyze.")
        return

    # -------------------------------------------------------------------------
    # Overall statistics
    # -------------------------------------------------------------------------
    total_claude_time = sum(d["claude_time"] for d in all_data)
    total_tool_time = sum(d["tool_time"] for d in all_data)
    total_thinking_time = sum(d["thinking_time"] for d in all_data)

    print(sep)
    print("  OVERALL STATISTICS")
    print(sep)
    print(f"  Total execution time (all tasks):  {total_claude_time:.1f}s ({total_claude_time / 60:.1f} min)")
    print(f"  Total tool execution time:        {total_tool_time:.1f}s ({total_tool_time / 60:.1f} min)")
    print(f"  Total thinking time:             {total_thinking_time:.1f}s ({total_thinking_time / 60:.1f} min)")
    print()
    print(f"  Tool time ratio:                  {total_tool_time / total_claude_time * 100:.1f}%")
    print(f"  Thinking time ratio:              {total_thinking_time / total_claude_time * 100:.1f}%")
    print()

    # -------------------------------------------------------------------------
    # Per-task statistics
    # -------------------------------------------------------------------------
    tool_ratios = [d["tool_ratio"] for d in all_data]
    thinking_ratios = [d["thinking_ratio"] for d in all_data]

    print(sep)
    print("  PER-TASK STATISTICS")
    print(sep)
    print(f"  Avg tool time ratio:    {statistics.mean(tool_ratios):.1f}%")
    print(f"  Median tool time ratio: {statistics.median(tool_ratios):.1f}%")
    print(f"  Min tool time ratio:    {min(tool_ratios):.1f}%")
    print(f"  Max tool time ratio:    {max(tool_ratios):.1f}%")
    print()
    print(f"  Avg thinking time ratio:    {statistics.mean(thinking_ratios):.1f}%")
    print(f"  Median thinking time ratio: {statistics.median(thinking_ratios):.1f}%")
    print(f"  Min thinking time ratio:    {min(thinking_ratios):.1f}%")
    print(f"  Max thinking time ratio:    {max(thinking_ratios):.1f}%")
    print()

    # -------------------------------------------------------------------------
    # Distribution buckets
    # -------------------------------------------------------------------------
    buckets = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50),
               (50, 60), (60, 70), (70, 80), (80, 90), (90, 100)]
    print(sep)
    print("  TOOL TIME RATIO DISTRIBUTION")
    print(sep)
    for lo, hi in buckets:
        count_in_bucket = sum(1 for d in all_data if lo <= d["tool_ratio"] < hi)
        pct_bucket = count_in_bucket / len(all_data) * 100
        label = f"{lo:>2.0f}% - {hi:>3.0f}%"
        bar = "#" * int(pct_bucket / 2)
        print(f"  {label}: {count_in_bucket:>3} ({pct_bucket:>5.1f}%) {bar}")
    count_eq_100 = sum(1 for d in all_data if d["tool_ratio"] >= 100)
    pct_eq_100 = count_eq_100 / len(all_data) * 100
    bar = "#" * int(pct_eq_100 / 2)
    print(f"  >= 100%: {count_eq_100:>3} ({pct_eq_100:>5.1f}%) {bar}")
    print()

    # -------------------------------------------------------------------------
    # Top and bottom tasks by tool time ratio
    # -------------------------------------------------------------------------
    sorted_by_tool_ratio = sorted(all_data, key=lambda d: d["tool_ratio"], reverse=True)

    print(sep)
    print("  TOP 10 TASKS BY TOOL TIME RATIO")
    print(sep)
    print(f"  {'Rank':<6} {'Task':<55} {'Tool(s)':>8} {'Think(s)':>9} {'Ratio':>7} {'Status':>7}")
    print(f"  {sep2}")
    for i, d in enumerate(sorted_by_tool_ratio[:10], 1):
        status = "PASS" if d["success"] else ("FAIL" if d["success"] is not None else "???")
        print(f"  {i:<6} {d['dir_name']:<55} {d['tool_time']:>7.1f} {d['thinking_time']:>8.1f} {d['tool_ratio']:>5.1f}% {status:>7}")

    print()
    print(sep)
    print("  BOTTOM 10 TASKS BY TOOL TIME RATIO")
    print(sep)
    print(f"  {'Rank':<6} {'Task':<55} {'Tool(s)':>8} {'Think(s)':>9} {'Ratio':>7} {'Status':>7}")
    print(f"  {sep2}")
    for i, d in enumerate(sorted_by_tool_ratio[-10:], 1):
        status = "PASS" if d["success"] else ("FAIL" if d["success"] is not None else "???")
        print(f"  {i:<6} {d['dir_name']:<55} {d['tool_time']:>7.1f} {d['thinking_time']:>8.1f} {d['tool_ratio']:>5.1f}% {status:>7}")
    print()

    # -------------------------------------------------------------------------
    # Successful vs Failed tasks comparison
    # -------------------------------------------------------------------------
    successful_tasks = [d for d in all_data if d["success"] is True]
    failed_tasks = [d for d in all_data if d["success"] is False]

    print(sep)
    print("  SUCCESSFUL vs FAILED TASKS")
    print(sep)

    def summarize_group(tasks, label):
        if not tasks:
            print(f"  {label}: No tasks in this group.")
            return
        total_ct = sum(d["claude_time"] for d in tasks)
        total_tt = sum(d["tool_time"] for d in tasks)
        avg_tool_ratio = statistics.mean([d["tool_ratio"] for d in tasks])
        median_tool_ratio = statistics.median([d["tool_ratio"] for d in tasks])
        print(f"  {label} ({len(tasks)} tasks):")
        print(f"    Total execution time:     {total_ct:.1f}s ({total_ct / 60:.1f} min)")
        print(f"    Total tool time:          {total_tt:.1f}s ({total_tt / 60:.1f} min)")
        print(f"    Avg tool time ratio:      {avg_tool_ratio:.1f}%")
        print(f"    Median tool time ratio:   {median_tool_ratio:.1f}%")

    summarize_group(successful_tasks, "SUCCESSFUL")
    print()
    summarize_group(failed_tasks, "FAILED")
    print()

    if successful_tasks and failed_tasks:
        s_avg = statistics.mean([d["tool_ratio"] for d in successful_tasks])
        f_avg = statistics.mean([d["tool_ratio"] for d in failed_tasks])
        s_med = statistics.median([d["tool_ratio"] for d in successful_tasks])
        f_med = statistics.median([d["tool_ratio"] for d in failed_tasks])
        print(f"  KEY DIFFERENCES:")
        print(f"    Avg tool time ratio:  Successful {s_avg:.1f}% vs Failed {f_avg:.1f}% (diff: {f_avg - s_avg:+.1f}%)")
        print(f"    Median tool time ratio: Successful {s_med:.1f}% vs Failed {f_med:.1f}% (diff: {f_med - s_med:+.1f}%)")
    print()

    # -------------------------------------------------------------------------
    # Per-task summary table
    # -------------------------------------------------------------------------
    print(sep)
    print("  APPENDIX: PER-TASK SUMMARY")
    print(sep)
    all_data_sorted = sorted(all_data, key=lambda d: d["tool_ratio"], reverse=True)
    print(f"  {'Task':<55} {'Total(s)':>10} {'Tool(s)':>8} {'Think(s)':>9} {'Tool%':>7} {'Status':>7}")
    print(f"  {sep2}")
    for d in all_data_sorted:
        status = "PASS" if d["success"] else ("FAIL" if d["success"] is not None else "???")
        print(f"  {d['dir_name']:<55} {d['claude_time']:>9.1f} {d['tool_time']:>7.1f} {d['thinking_time']:>8.1f} {d['tool_ratio']:>5.1f}% {status:>7}")
    print()

    # =========================================================================
    # NEW ANALYSES
    # =========================================================================

    # -------------------------------------------------------------------------
    # Tool Transition Patterns
    # -------------------------------------------------------------------------
    print(sep)
    print("  TOOL TRANSITION PATTERNS (Success vs Failure)")
    print(sep)

    for label, group in [("SUCCESSFUL", successful_tasks), ("FAILED", failed_tasks)]:
        trans_counts = defaultdict(int)
        for d in group:
            for t in d["transitions"]:
                trans_counts[t] += 1
        top = sorted(trans_counts.items(), key=lambda x: x[1], reverse=True)[:15]
        print(f"\n  {label} (top 15 transitions):")
        for (a, b), cnt in top:
            print(f"    {a:>12} -> {b:<12}  {cnt:>5}")
    print()

    # -------------------------------------------------------------------------
    # Bash Command Categorization
    # -------------------------------------------------------------------------
    print(sep)
    print("  BASH COMMAND CATEGORIZATION")
    print(sep)

    agg_bash_time = defaultdict(float)
    agg_bash_count = defaultdict(int)
    s_bash_time = defaultdict(float)
    f_bash_time = defaultdict(float)
    for d in all_data:
        for cat, t in d["bash_cat_times"].items():
            agg_bash_time[cat] += t
            agg_bash_count[cat] += d["bash_cat_counts"].get(cat, 0)
            if d["success"]:
                s_bash_time[cat] += t
            elif d["success"] is not None:
                f_bash_time[cat] += t

    total_bash = sum(agg_bash_time.values())
    print(f"\n  {'Category':<20} {'Time(s)':>10} {'%':>7} {'Count':>7} {'Avg(s)':>8}")
    print(f"  {sep2}")
    for cat in sorted(agg_bash_time, key=agg_bash_time.get, reverse=True):
        t = agg_bash_time[cat]
        c = agg_bash_count[cat]
        pct = t / total_bash * 100 if total_bash > 0 else 0
        avg = t / c if c > 0 else 0
        print(f"  {cat:<20} {t:>9.1f} {pct:>6.1f}% {c:>7} {avg:>7.1f}")
    print()

    # -------------------------------------------------------------------------
    # Tool Diversity vs Success
    # -------------------------------------------------------------------------
    print(sep)
    print("  TOOL DIVERSITY vs SUCCESS")
    print(sep)
    if successful_tasks:
        s_div = statistics.mean([d["unique_tools"] for d in successful_tasks])
        print(f"  Successful avg unique tools: {s_div:.1f}")
    if failed_tasks:
        f_div = statistics.mean([d["unique_tools"] for d in failed_tasks])
        print(f"  Failed avg unique tools:     {f_div:.1f}")
    print()

    # -------------------------------------------------------------------------
    # Output Length vs Success
    # -------------------------------------------------------------------------
    print(sep)
    print("  OUTPUT LENGTH vs SUCCESS")
    print(sep)
    if successful_tasks:
        s_len = statistics.mean([d["output_length"] for d in successful_tasks])
        print(f"  Successful avg output length: {s_len:.0f} chars")
    if failed_tasks:
        f_len = statistics.mean([d["output_length"] for d in failed_tasks])
        print(f"  Failed avg output length:     {f_len:.0f} chars")
    print()

    # =========================================================================
    # SYSTEM-LEVEL ANALYSIS
    # =========================================================================

    # -------------------------------------------------------------------------
    # Container Overhead Analysis
    # -------------------------------------------------------------------------
    print(sep)
    print("  CONTAINER OVERHEAD ANALYSIS")
    print(sep)
    tasks_with_times = [d for d in all_data if d["total_time"] > 0]
    if tasks_with_times:
        pull_times = [d["pull_time"] for d in tasks_with_times]
        perm_times = [d["permission_fix_time"] for d in tasks_with_times]
        overheads = [d["pull_time"] + d["permission_fix_time"] for d in tasks_with_times]
        overhead_pcts = [(d["pull_time"] + d["permission_fix_time"]) / d["total_time"] * 100
                         for d in tasks_with_times if d["total_time"] > 0]
        claude_pcts = [d["claude_time"] / d["total_time"] * 100
                       for d in tasks_with_times if d["total_time"] > 0]

        print(f"  pull_time:           avg={statistics.mean(pull_times):.1f}s  median={statistics.median(pull_times):.1f}s  max={max(pull_times):.1f}s")
        print(f"  permission_fix_time: avg={statistics.mean(perm_times):.1f}s  median={statistics.median(perm_times):.1f}s  max={max(perm_times):.1f}s")
        print(f"  total overhead:      avg={statistics.mean(overheads):.1f}s  ({statistics.mean(overhead_pcts):.1f}% of total)")
        print(f"  claude_time ratio:   avg={statistics.mean(claude_pcts):.1f}%  (actual work)")
    print()

    # -------------------------------------------------------------------------
    # CPU Utilization Analysis
    # -------------------------------------------------------------------------
    print(sep)
    print("  CPU UTILIZATION ANALYSIS")
    print(sep)
    tasks_with_cpu = [d for d in all_data if d["avg_cpu"] > 0]
    if tasks_with_cpu:
        cpus = [d["avg_cpu"] for d in tasks_with_cpu]
        print(f"  Avg CPU utilization:    {statistics.mean(cpus):.1f}%")
        print(f"  Median CPU utilization: {statistics.median(cpus):.1f}%")
        print(f"  Max avg CPU:            {max(cpus):.1f}%")
        print(f"  Min avg CPU:            {min(cpus):.1f}%")
        print()
        # Concurrency estimate
        avg_cpu = statistics.mean(cpus)
        if avg_cpu > 0:
            concurrent = 100.0 / avg_cpu
            print(f"  Estimated concurrent containers at 100% CPU: ~{concurrent:.0f}")
            print(f"  Estimated concurrent containers at 80% CPU:  ~{concurrent * 0.8:.0f}")
    print()

    # -------------------------------------------------------------------------
    # Memory Usage Analysis
    # -------------------------------------------------------------------------
    print(sep)
    print("  MEMORY USAGE ANALYSIS")
    print(sep)
    tasks_with_mem = [d for d in all_data if d["peak_mem_mb"] > 0]
    if tasks_with_mem:
        peaks = [d["peak_mem_mb"] for d in tasks_with_mem]
        positions = [d["peak_mem_position"] for d in tasks_with_mem]
        print(f"  Peak memory:   avg={statistics.mean(peaks):.0f}MB  median={statistics.median(peaks):.0f}MB  max={max(peaks):.0f}MB")
        print(f"  Peak timing:   avg={statistics.mean(positions)*100:.0f}% through execution")
        early = sum(1 for p in positions if p < 0.33)
        mid = sum(1 for p in positions if 0.33 <= p < 0.66)
        late = sum(1 for p in positions if p >= 0.66)
        total = len(positions)
        print(f"  Peak in early third: {early}/{total} ({early/total*100:.0f}%)")
        print(f"  Peak in middle third: {mid}/{total} ({mid/total*100:.0f}%)")
        print(f"  Peak in late third:   {late}/{total} ({late/total*100:.0f}%)")
        print()
        # Success vs failure
        s_mem = [d["peak_mem_mb"] for d in successful_tasks if d["peak_mem_mb"] > 0]
        f_mem = [d["peak_mem_mb"] for d in failed_tasks if d["peak_mem_mb"] > 0]
        if s_mem:
            print(f"  Successful peak mem: avg={statistics.mean(s_mem):.0f}MB  max={max(s_mem):.0f}MB")
        if f_mem:
            print(f"  Failed peak mem:     avg={statistics.mean(f_mem):.0f}MB  max={max(f_mem):.0f}MB")
    print()

    # -------------------------------------------------------------------------
    # Docker Image Size Impact
    # -------------------------------------------------------------------------
    print(sep)
    print("  DOCKER IMAGE SIZE IMPACT")
    print(sep)
    tasks_with_img = [d for d in all_data if d["image_size_mb"] > 0]
    if len(tasks_with_img) >= 3:
        sizes = [d["image_size_mb"] for d in tasks_with_img]
        times = [d["claude_time"] for d in tasks_with_img]
        successes = [1 if d["success"] else 0 for d in tasks_with_img]
        print(f"  Image sizes: avg={statistics.mean(sizes):.0f}MB  range={min(sizes):.0f}-{max(sizes):.0f}MB")
        # Correlation
        if len(sizes) > 2:
            r_time = np.corrcoef(sizes, times)[0, 1]
            r_success = np.corrcoef(sizes, successes)[0, 1]
            print(f"  Correlation image_size vs claude_time: r={r_time:.3f}")
            print(f"  Correlation image_size vs success:     r={r_success:.3f}")
    print()

    # =========================================================================
    # CHART GENERATION
    # =========================================================================
    print(sep)
    print("  GENERATING CHARTS")
    print(sep)
    generate_charts(all_data, successful_tasks, failed_tasks, chart_dir)

    print()
    print(sep)
    print("  END OF REPORT")
    print(sep)


# ---------------------------------------------------------------------------
# Chart generation
# ---------------------------------------------------------------------------

def generate_charts(all_data, successful_tasks, failed_tasks, chart_dir):
    """Generate all analysis charts."""

    # --- Chart 1: Per-Repo Success Rate ---
    try:
        repos = defaultdict(lambda: {"pass": 0, "fail": 0})
        for d in all_data:
            if d["success"] is True:
                repos[d["repo_name"]]["pass"] += 1
            elif d["success"] is False:
                repos[d["repo_name"]]["fail"] += 1

        repo_names = sorted(repos.keys(), key=lambda r: repos[r]["pass"] / max(repos[r]["pass"] + repos[r]["fail"], 1))
        rates = [repos[r]["pass"] / max(repos[r]["pass"] + repos[r]["fail"], 1) * 100 for r in repo_names]
        totals = [repos[r]["pass"] + repos[r]["fail"] for r in repo_names]
        passes = [repos[r]["pass"] for r in repo_names]

        fig, ax = plt.subplots(figsize=(10, max(6, len(repo_names) * 0.4)))
        colors = [COLORS["success"] if r >= 50 else COLORS["failure"] for r in rates]
        bars = ax.barh(range(len(repo_names)), rates, color=colors, alpha=0.8)
        ax.set_yticks(range(len(repo_names)))
        ax.set_yticklabels(repo_names, fontsize=9)
        ax.set_xlabel("Success Rate (%)", fontsize=12)
        ax.set_title("Success Rate by Repository", fontsize=14)
        ax.set_xlim(0, 105)
        ax.axvline(x=50, color="gray", linestyle="--", alpha=0.5)
        for i, (rate, total, p) in enumerate(zip(rates, totals, passes)):
            ax.text(rate + 1, i, f"{p}/{total}", va="center", fontsize=8)
        ax.grid(axis="x", alpha=0.3)
        fig.tight_layout()
        save_chart(fig, "chart_01_repo_success_rate.png", chart_dir)
    except Exception as e:
        print(f"  [WARN] Chart 1 failed: {e}")

    # --- Chart 2: Time Distribution ---
    try:
        s_times = [d["claude_time"] for d in successful_tasks]
        f_times = [d["claude_time"] for d in failed_tasks]

        fig, ax = plt.subplots(figsize=(10, 6))
        bins = np.linspace(0, max(d["claude_time"] for d in all_data) + 50, 20)
        if s_times:
            ax.hist(s_times, bins=bins, alpha=0.6, color=COLORS["success"], label=f"Success (n={len(s_times)})", edgecolor="white")
        if f_times:
            ax.hist(f_times, bins=bins, alpha=0.6, color=COLORS["failure"], label=f"Failure (n={len(f_times)})", edgecolor="white")
        ax.set_xlabel("Claude Time (seconds)", fontsize=12)
        ax.set_ylabel("Count", fontsize=12)
        ax.set_title("Execution Time Distribution: Success vs Failure", fontsize=14)
        ax.legend(fontsize=11)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax.grid(alpha=0.3)
        save_chart(fig, "chart_02_time_distribution.png", chart_dir)
    except Exception as e:
        print(f"  [WARN] Chart 2 failed: {e}")

    # --- Chart 3: Tool Ratio Distribution ---
    try:
        s_ratios = [d["tool_ratio"] for d in successful_tasks]
        f_ratios = [d["tool_ratio"] for d in failed_tasks]

        fig, ax = plt.subplots(figsize=(10, 6))
        bins = np.linspace(0, 80, 16)
        if s_ratios:
            ax.hist(s_ratios, bins=bins, alpha=0.6, color=COLORS["success"], label="Success", edgecolor="white")
        if f_ratios:
            ax.hist(f_ratios, bins=bins, alpha=0.6, color=COLORS["failure"], label="Failure", edgecolor="white")
        med = statistics.median([d["tool_ratio"] for d in all_data])
        ax.axvline(x=med, color="black", linestyle="--", alpha=0.7, label=f"Median ({med:.1f}%)")
        ax.set_xlabel("Tool Time Ratio (%)", fontsize=12)
        ax.set_ylabel("Count", fontsize=12)
        ax.set_title("Tool Time Ratio Distribution", fontsize=14)
        ax.legend(fontsize=11)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax.grid(alpha=0.3)
        save_chart(fig, "chart_03_tool_ratio_distribution.png", chart_dir)
    except Exception as e:
        print(f"  [WARN] Chart 3 failed: {e}")

    # --- Chart 4: Tool Usage Breakdown (calls + time) ---
    try:
        agg_time = defaultdict(float)
        agg_count = defaultdict(int)
        for d in all_data:
            for tn, t in d["per_tool_time"].items():
                agg_time[tn] += t
            for tn, c in d["per_tool_count"].items():
                agg_count[tn] += c

        tools = sorted(agg_time.keys(), key=lambda t: agg_time[t], reverse=True)

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
        y_pos = range(len(tools))
        ax1.barh(y_pos, [agg_count[t] for t in tools], color=COLORS["neutral"], alpha=0.8)
        ax1.set_yticks(y_pos)
        ax1.set_yticklabels(tools)
        ax1.set_xlabel("Call Count")
        ax1.set_title("Tool Calls by Count")
        ax1.grid(axis="x", alpha=0.3)

        ax2.barh(y_pos, [agg_time[t] for t in tools], color=COLORS["neutral"], alpha=0.8)
        ax2.set_yticks(y_pos)
        ax2.set_yticklabels(tools)
        ax2.set_xlabel("Total Time (seconds)")
        ax2.set_title("Tool Calls by Time")
        ax2.grid(axis="x", alpha=0.3)

        fig.suptitle("Tool Usage Breakdown", fontsize=14, y=1.02)
        fig.tight_layout()
        save_chart(fig, "chart_04_tool_usage_breakdown.png", chart_dir)
    except Exception as e:
        print(f"  [WARN] Chart 4 failed: {e}")

    # --- Chart 5: Tool Timeline (stacked area) ---
    try:
        n_bins = 10
        tool_types = ["Bash", "Read", "Edit", "Grep", "Glob", "Write", "TodoWrite"]
        timeline_data = {t: np.zeros(n_bins) for t in tool_types}
        timeline_data["Other"] = np.zeros(n_bins)

        for d in all_data:
            for norm_pos, tool_name in d["tool_timeline"]:
                b = min(int(norm_pos * n_bins), n_bins - 1)
                if tool_name in timeline_data:
                    timeline_data[tool_name][b] += 1
                else:
                    timeline_data["Other"][b] += 1

        x = np.arange(n_bins)
        x_labels = [f"{i * 10}-{(i + 1) * 10}%" for i in range(n_bins)]
        cmap = plt.cm.tab10
        active_tools = [t for t in list(tool_types) + ["Other"] if timeline_data[t].sum() > 0]
        stacks = [timeline_data[t] for t in active_tools]
        colors = [cmap(i) for i in range(len(active_tools))]

        fig, ax = plt.subplots(figsize=(12, 6))
        ax.stackplot(x, *stacks, labels=active_tools, colors=colors, alpha=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels(x_labels, fontsize=13)
        ax.set_xlabel("Normalized Execution Time", fontsize=15)
        ax.set_ylabel("Tool Call Count", fontsize=15)
        ax.set_title("Tool Usage Over Execution Timeline", fontsize=16)
        ax.legend(loc="upper right", fontsize=13)
        ax.tick_params(axis="y", labelsize=13)
        ax.grid(alpha=0.3)
        save_chart(fig, "chart_05_tool_timeline.png", chart_dir)
    except Exception as e:
        print(f"  [WARN] Chart 5 failed: {e}")

    # --- Chart 6: Bash Categories ---
    try:
        agg_bash_time = defaultdict(float)
        agg_bash_count = defaultdict(int)
        s_bash_t = defaultdict(float)
        f_bash_t = defaultdict(float)
        for d in all_data:
            for cat, t in d["bash_cat_times"].items():
                agg_bash_time[cat] += t
                agg_bash_count[cat] += d["bash_cat_counts"].get(cat, 0)
                if d["success"]:
                    s_bash_t[cat] += t
                elif d["success"] is not None:
                    f_bash_t[cat] += t

        cats = sorted(agg_bash_time.keys(), key=lambda c: agg_bash_time[c], reverse=True)
        total_bash = sum(agg_bash_time.values())

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

        # Pie chart
        sizes = [agg_bash_time[c] for c in cats]
        labels_pie = [f"{c}\n({agg_bash_time[c]/total_bash*100:.1f}%)" for c in cats]
        cmap = plt.cm.Set3
        colors_pie = [cmap(i) for i in range(len(cats))]
        ax1.pie(sizes, labels=labels_pie, colors=colors_pie, startangle=90, textprops={"fontsize": 8})
        ax1.set_title("Bash Time by Category", fontsize=12)

        # Grouped bar: success vs failure
        x = np.arange(len(cats))
        w = 0.35
        s_vals = [s_bash_t.get(c, 0) for c in cats]
        f_vals = [f_bash_t.get(c, 0) for c in cats]
        ax2.barh(x - w / 2, s_vals, w, color=COLORS["success"], label="Success", alpha=0.8)
        ax2.barh(x + w / 2, f_vals, w, color=COLORS["failure"], label="Failure", alpha=0.8)
        ax2.set_yticks(x)
        ax2.set_yticklabels(cats, fontsize=9)
        ax2.set_xlabel("Time (seconds)")
        ax2.set_title("Bash Category Time: Success vs Failure", fontsize=12)
        ax2.legend()
        ax2.grid(axis="x", alpha=0.3)

        fig.suptitle("Bash Command Analysis", fontsize=14, y=1.02)
        fig.tight_layout()
        save_chart(fig, "chart_06_bash_categories.png", chart_dir)
    except Exception as e:
        print(f"  [WARN] Chart 6 failed: {e}")

    # --- Chart 7: Resource Boxplots ---
    try:
        s_mem = [d["peak_mem_mb"] for d in successful_tasks if d["peak_mem_mb"] > 0]
        f_mem = [d["peak_mem_mb"] for d in failed_tasks if d["peak_mem_mb"] > 0]
        s_cpu = [d["avg_cpu"] for d in successful_tasks if d["avg_cpu"] > 0]
        f_cpu = [d["avg_cpu"] for d in failed_tasks if d["avg_cpu"] > 0]

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

        bp1 = ax1.boxplot([s_mem, f_mem], labels=["Success", "Failure"], patch_artist=True)
        bp1["boxes"][0].set_facecolor(COLORS["success"])
        bp1["boxes"][0].set_alpha(0.6)
        bp1["boxes"][1].set_facecolor(COLORS["failure"])
        bp1["boxes"][1].set_alpha(0.6)
        ax1.set_ylabel("Peak Memory (MB)")
        ax1.set_title("Peak Memory Usage")
        ax1.grid(alpha=0.3)

        bp2 = ax2.boxplot([s_cpu, f_cpu], labels=["Success", "Failure"], patch_artist=True)
        bp2["boxes"][0].set_facecolor(COLORS["success"])
        bp2["boxes"][0].set_alpha(0.6)
        bp2["boxes"][1].set_facecolor(COLORS["failure"])
        bp2["boxes"][1].set_alpha(0.6)
        ax2.set_ylabel("Average CPU (%)")
        ax2.set_title("CPU Utilization")
        ax2.grid(alpha=0.3)

        fig.suptitle("Resource Usage: Success vs Failure", fontsize=14)
        fig.tight_layout()
        save_chart(fig, "chart_07_resource_boxplots.png", chart_dir)
    except Exception as e:
        print(f"  [WARN] Chart 7 failed: {e}")

    # --- Chart 8: Time Breakdown Stacked Bar ---
    try:
        sorted_tasks = sorted(all_data, key=lambda d: d["total_time"], reverse=True)[:30]
        names = [d["task_name"][:35] for d in sorted_tasks]
        pull = [d["pull_time"] for d in sorted_tasks]
        perm = [d["permission_fix_time"] for d in sorted_tasks]
        think = [d["thinking_time"] for d in sorted_tasks]
        tool = [d["tool_time"] for d in sorted_tasks]

        fig, ax = plt.subplots(figsize=(12, max(6, len(names) * 0.35)))
        y = np.arange(len(names))
        ax.barh(y, pull, color="#3498db", label="Pull", alpha=0.8)
        ax.barh(y, perm, left=pull, color="#e67e22", label="Permission Fix", alpha=0.8)
        ax.barh(y, think, left=[p + pe for p, pe in zip(pull, perm)], color="#9b59b6", label="Thinking (LLM)", alpha=0.8)
        ax.barh(y, tool, left=[p + pe + t for p, pe, t in zip(pull, perm, think)], color="#2ecc71", label="Tool Exec", alpha=0.8)
        ax.set_yticks(y)
        ax.set_yticklabels(names, fontsize=8)
        ax.set_xlabel("Time (seconds)")
        ax.set_title("Time Breakdown per Task (top 30 by total time)", fontsize=14)
        ax.legend(loc="lower right", fontsize=9)
        ax.grid(axis="x", alpha=0.3)
        ax.invert_yaxis()
        fig.tight_layout()
        save_chart(fig, "chart_08_time_breakdown.png", chart_dir)
    except Exception as e:
        print(f"  [WARN] Chart 8 failed: {e}")

    # --- Chart 9: Overhead Analysis ---
    try:
        tasks_ov = [d for d in all_data if d["total_time"] > 0]

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

        # Permission fix time distribution
        perm_times = [d["permission_fix_time"] for d in tasks_ov]
        ax1.hist(perm_times, bins=15, color="#e67e22", alpha=0.8, edgecolor="white")
        ax1.axvline(statistics.mean(perm_times), color="red", linestyle="--",
                    label=f"Mean: {statistics.mean(perm_times):.1f}s")
        ax1.set_xlabel("Permission Fix Time (seconds)")
        ax1.set_ylabel("Count")
        ax1.set_title("Permission Fix Time Distribution")
        ax1.legend()
        ax1.grid(alpha=0.3)

        # Overhead % scatter
        overhead_pcts = [(d["pull_time"] + d["permission_fix_time"]) / d["total_time"] * 100
                         for d in tasks_ov]
        total_times = [d["total_time"] for d in tasks_ov]
        ax2.scatter(total_times, overhead_pcts, c=COLORS["neutral"], alpha=0.6, s=50)
        ax2.set_xlabel("Total Time (seconds)")
        ax2.set_ylabel("Overhead %")
        ax2.set_title("Container Overhead vs Total Time")
        ax2.grid(alpha=0.3)

        fig.suptitle("Container Infrastructure Overhead", fontsize=14, y=1.02)
        fig.tight_layout()
        save_chart(fig, "chart_09_overhead_analysis.png", chart_dir)
    except Exception as e:
        print(f"  [WARN] Chart 9 failed: {e}")

    # --- Chart 10: Aggregated Memory Trajectory ---
    try:
        n_points = 100
        all_mem_interp = []
        for d in all_data:
            traj = d["mem_trajectory"]
            if len(traj) >= 10:
                x_orig = np.linspace(0, 1, len(traj))
                x_new = np.linspace(0, 1, n_points)
                interp = np.interp(x_new, x_orig, traj)
                all_mem_interp.append(interp)

        if all_mem_interp:
            mem_matrix = np.array(all_mem_interp)
            mean_mem = np.mean(mem_matrix, axis=0)
            p25 = np.percentile(mem_matrix, 25, axis=0)
            p75 = np.percentile(mem_matrix, 75, axis=0)
            p10 = np.percentile(mem_matrix, 10, axis=0)
            p90 = np.percentile(mem_matrix, 90, axis=0)

            x = np.linspace(0, 100, n_points)
            fig, ax = plt.subplots(figsize=(12, 6))
            ax.fill_between(x, p10, p90, alpha=0.15, color=COLORS["neutral"], label="P10-P90")
            ax.fill_between(x, p25, p75, alpha=0.3, color=COLORS["neutral"], label="P25-P75")
            ax.plot(x, mean_mem, color=COLORS["neutral"], linewidth=2, label="Mean")
            ax.set_xlabel("Execution Progress (%)", fontsize=15)
            ax.set_ylabel("Memory Usage (MB)", fontsize=15)
            ax.set_title(f"Aggregated Memory Trajectory (n={len(all_mem_interp)} tasks)", fontsize=16)
            ax.legend(fontsize=13)
            ax.tick_params(axis="both", labelsize=13)
            ax.grid(alpha=0.3)
            save_chart(fig, "chart_10_memory_trajectory.png", chart_dir)
    except Exception as e:
        print(f"  [WARN] Chart 10 failed: {e}")

    # --- Chart 11: CPU Utilization ---
    try:
        n_points = 100
        all_cpu_interp = []
        for d in all_data:
            traj = d["cpu_trajectory"]
            if len(traj) >= 10:
                x_orig = np.linspace(0, 1, len(traj))
                x_new = np.linspace(0, 1, n_points)
                interp = np.interp(x_new, x_orig, traj)
                all_cpu_interp.append(interp)

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

        # Histogram of average CPU
        avg_cpus = [d["avg_cpu"] for d in all_data if d["avg_cpu"] > 0]
        ax1.hist(avg_cpus, bins=15, color=COLORS["neutral"], alpha=0.8, edgecolor="white")
        if avg_cpus:
            ax1.axvline(statistics.mean(avg_cpus), color="red", linestyle="--",
                        label=f"Mean: {statistics.mean(avg_cpus):.1f}%")
        ax1.set_xlabel("Average CPU Utilization (%)")
        ax1.set_ylabel("Count")
        ax1.set_title("CPU Utilization Distribution")
        ax1.legend()
        ax1.grid(alpha=0.3)

        # Aggregated CPU trajectory
        if all_cpu_interp:
            cpu_matrix = np.array(all_cpu_interp)
            mean_cpu = np.mean(cpu_matrix, axis=0)
            p25 = np.percentile(cpu_matrix, 25, axis=0)
            p75 = np.percentile(cpu_matrix, 75, axis=0)
            x = np.linspace(0, 100, n_points)
            ax2.fill_between(x, p25, p75, alpha=0.3, color=COLORS["neutral"], label="P25-P75")
            ax2.plot(x, mean_cpu, color=COLORS["neutral"], linewidth=2, label="Mean")
            ax2.set_xlabel("Execution Progress (%)")
            ax2.set_ylabel("CPU (%)")
            ax2.set_title(f"CPU Trajectory (n={len(all_cpu_interp)})")
            ax2.legend()
            ax2.grid(alpha=0.3)

        fig.suptitle("CPU Utilization Analysis", fontsize=14, y=1.02)
        fig.tight_layout()
        save_chart(fig, "chart_11_cpu_utilization.png", chart_dir)
    except Exception as e:
        print(f"  [WARN] Chart 11 failed: {e}")

    # --- Chart 12: Bash Time by Category ---
    try:
        agg_bash_time = defaultdict(float)
        for d in all_data:
            for cat, t in d["bash_cat_times"].items():
                agg_bash_time[cat] += t

        cats = sorted(agg_bash_time.keys(), key=lambda c: agg_bash_time[c])
        total_bash = sum(agg_bash_time.values())

        fig, ax = plt.subplots(figsize=(10, max(5, len(cats) * 0.5)))
        vals = [agg_bash_time[c] for c in cats]
        colors_bar = [plt.cm.Set2(i) for i in range(len(cats))]
        bars = ax.barh(range(len(cats)), vals, color=colors_bar, alpha=0.8)
        ax.set_yticks(range(len(cats)))
        ax.set_yticklabels(cats, fontsize=10)
        ax.set_xlabel("Total Time (seconds)", fontsize=12)
        ax.set_title("Bash Execution Time by Command Category", fontsize=14)
        for i, (c, v) in enumerate(zip(cats, vals)):
            pct = v / total_bash * 100 if total_bash > 0 else 0
            ax.text(v + 5, i, f"{v:.0f}s ({pct:.1f}%)", va="center", fontsize=9)
        ax.grid(axis="x", alpha=0.3)
        fig.tight_layout()
        save_chart(fig, "chart_12_bash_time_by_category.png", chart_dir)
    except Exception as e:
        print(f"  [WARN] Chart 12 failed: {e}")

    # --- Chart 13: Memory Peak Timing ---
    try:
        positions = [d["peak_mem_position"] * 100 for d in all_data if d["peak_mem_mb"] > 0]

        fig, ax = plt.subplots(figsize=(10, 6))
        ax.hist(positions, bins=10, range=(0, 100), color="#9b59b6", alpha=0.8, edgecolor="white")
        ax.axvline(statistics.mean(positions), color="red", linestyle="--",
                   label=f"Mean: {statistics.mean(positions):.0f}%")
        ax.axvline(statistics.median(positions), color="orange", linestyle="--",
                   label=f"Median: {statistics.median(positions):.0f}%")
        ax.set_xlabel("Execution Progress When Peak Memory Occurs (%)", fontsize=15)
        ax.set_ylabel("Number of Tasks", fontsize=15)
        ax.set_title("Memory Peak Timing Distribution", fontsize=16)
        ax.legend(fontsize=13)
        ax.tick_params(axis="both", labelsize=13)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax.grid(alpha=0.3)
        save_chart(fig, "chart_13_memory_peak_timing.png", chart_dir)
    except Exception as e:
        print(f"  [WARN] Chart 13 failed: {e}")

    # --- Chart 14: Scatter total_time vs tool_ratio ---
    try:
        fig, ax = plt.subplots(figsize=(10, 7))
        for d in all_data:
            color = COLORS["success"] if d["success"] else COLORS["failure"]
            size = max(20, min(200, d["peak_mem_mb"] / 3))
            ax.scatter(d["claude_time"], d["tool_ratio"], c=color, s=size, alpha=0.6, edgecolors="gray", linewidth=0.5)

        # Legend
        ax.scatter([], [], c=COLORS["success"], s=60, label="Success", edgecolors="gray")
        ax.scatter([], [], c=COLORS["failure"], s=60, label="Failure", edgecolors="gray")
        ax.scatter([], [], c="gray", s=30, alpha=0.5, label="Size = Peak Mem")
        ax.legend(fontsize=10)
        ax.set_xlabel("Claude Time (seconds)", fontsize=12)
        ax.set_ylabel("Tool Time Ratio (%)", fontsize=12)
        ax.set_title("Execution Time vs Tool Ratio (size = peak memory)", fontsize=14)
        ax.grid(alpha=0.3)
        save_chart(fig, "chart_14_scatter_time_ratio.png", chart_dir)
    except Exception as e:
        print(f"  [WARN] Chart 14 failed: {e}")


if __name__ == "__main__":
    main()
