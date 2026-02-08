#!/usr/bin/env python3
"""
Characterization Analysis Runner

Generates all figures and numerical data for Section 3 (Characterization)
of the AgentCgroup paper, by importing and orchestrating the individual
analysis scripts.

Uses:
- experiments/all_images_haiku  (Haiku / cloud API agent)
- experiments/all_images_local  (GLM 4.7 flash / local GPU agent)

Outputs:
- analysis/haiku_figures/       (primary characterization figures)
- analysis/qwen3_figures/       (local model figures)
- analysis/comparison_figures/  (Haiku vs Local comparison)
- Prints all numerical values referenced in characterization.md

Usage:
    python analysis/characterization.py              # full run
    python analysis/characterization.py --haiku-only # Haiku dataset only
    python analysis/characterization.py --local-only # Local dataset only
    python analysis/characterization.py --skip-extended --skip-rq  # fast
"""

import argparse
import os
import sys
import statistics
from pathlib import Path

import numpy as np
import json as _json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

# Ensure analysis/ is importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
EXPERIMENTS_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "experiments"))
HAIKU_DIR = os.path.join(EXPERIMENTS_DIR, "all_images_haiku")
LOCAL_DIR = os.path.join(EXPERIMENTS_DIR, "all_images_local")
HAIKU_FIGURES = os.path.abspath(os.path.join(SCRIPT_DIR, "haiku_figures"))
QWEN3_FIGURES = os.path.abspath(os.path.join(SCRIPT_DIR, "qwen3_figures"))
COMPARISON_FIGURES = os.path.abspath(os.path.join(SCRIPT_DIR, "comparison_figures"))


# ============================================================================
# Step 1: analyze_swebench_data  →  rq1_*, rq2_*, rq3_*, rq4_* figures
# ============================================================================

def step_swebench_analysis(dataset_name, base_dir, output_dir):
    """Run analyze_swebench_data.py for one dataset.

    Generates: rq1_resource_timeseries.png, rq1_change_rate_distribution.png,
               rq2_category_boxplots.png, rq3_tool_analysis.png,
               rq4_overprovisioning.png, report.md
    """
    import analyze_swebench_data as asd

    _section(f"analyze_swebench_data.py  →  {dataset_name}")
    print(f"  Data:   {base_dir}")
    print(f"  Output: {output_dir}")

    asd.BASE_DIR = Path(base_dir)
    asd.OUTPUT_DIR = Path(output_dir)
    asd.REPORT_PATH = Path(output_dir) / "report.md"
    asd.DATASET_TYPE = "flat"
    os.makedirs(output_dir, exist_ok=True)

    tasks, progress = asd.load_all_data()
    if not tasks:
        print(f"  WARNING: No tasks loaded for {dataset_name}")
        return None, {}

    print(f"  Loaded {len(tasks)} tasks")

    dynamics = asd.analyze_dynamics(tasks)
    categories = asd.analyze_categories(tasks)
    tools = asd.analyze_tools(tasks)
    overprov = asd.analyze_overprovisioning(tasks)
    asd.generate_report(tasks, dynamics, categories, tools, overprov, dataset_name)

    return tasks, {
        "dynamics": dynamics,
        "categories": categories,
        "tools": tools,
        "overprovisioning": overprov,
    }


# ============================================================================
# Step 2: analyze_tool_time_ratio  →  chart_01 … chart_14
# ============================================================================

def step_tool_time_analysis(base_dir, figures_dir):
    """Run analyze_tool_time_ratio.py (via sys.argv patching).

    Generates: chart_01_repo_success_rate.png … chart_14_scatter_time_ratio.png
    """
    _section("analyze_tool_time_ratio.py")
    print(f"  Data:   {base_dir}")
    print(f"  Output: {figures_dir}")

    saved_argv = sys.argv
    sys.argv = [
        "analyze_tool_time_ratio.py",
        "--data-dir", base_dir,
        "--figures-dir", figures_dir,
    ]
    try:
        import analyze_tool_time_ratio as att
        att.main()
    finally:
        sys.argv = saved_argv


# ============================================================================
# Step 3: analyze_haiku_vs_qwen  →  comparison_figures/
# ============================================================================

def step_comparison():
    """Run analyze_haiku_vs_qwen.py for Haiku vs Local comparison.

    Generates: 01_duration_comparison.png … 06_overall_comparison.png
    """
    import analyze_haiku_vs_qwen as ahq

    _section("analyze_haiku_vs_qwen.py  →  comparison_figures/")

    ahq.HAIKU_DIR = HAIKU_DIR
    ahq.LOCAL_DIR = LOCAL_DIR
    ahq.FIGURES_DIR = COMPARISON_FIGURES

    results = ahq.analyze_comparison(60, 10)
    if not results:
        print("  WARNING: No valid task pairs found")
        return [], {}

    stats = ahq.print_report(results, 60)
    ahq.generate_charts(results)
    report_path = os.path.join(SCRIPT_DIR, "haiku_vs_qwen_report.md")
    ahq.generate_markdown_report(results, stats, report_path, 60)

    return results, stats


# ============================================================================
# Step 4: analyze_extended_insights  →  textual insights & data
# ============================================================================

def step_extended_insights(run_haiku=True, run_local=True):
    """Run extended insight analyses for paper data.

    Returns a dict keyed by dataset name with sub-analysis results.
    """
    import analyze_extended_insights as aei

    _section("analyze_extended_insights.py")

    all_results = {}

    datasets = []
    if run_haiku:
        datasets.append(("Haiku", HAIKU_DIR))
    if run_local:
        datasets.append(("Local", LOCAL_DIR))

    for name, base_dir in datasets:
        if not os.path.exists(base_dir):
            print(f"  WARNING: {name} dir not found: {base_dir}")
            continue

        print(f"\n  --- {name} ---")
        all_results[name] = {
            "disk_overhead": aei.analyze_disk_and_startup_overhead(base_dir),
            "transient_bursts": aei.analyze_transient_bursts(base_dir),
            "cpu_memory_correlation": aei.analyze_cpu_memory_correlation(base_dir),
            "retry_patterns": aei.analyze_retry_loop_patterns(base_dir),
            "tool_timeline": aei.analyze_tool_timeline_distribution(base_dir),
            "concurrency": aei.analyze_concurrency_potential(base_dir),
            "memory_trajectory": aei.analyze_memory_trajectory(base_dir),
            "tool_semantic_variance": aei.analyze_tool_semantic_variance(base_dir),
        }

    if run_haiku and run_local and os.path.exists(HAIKU_DIR) and os.path.exists(LOCAL_DIR):
        all_results["comparison"] = aei.analyze_local_vs_api_inference(HAIKU_DIR, LOCAL_DIR)

    return all_results


# ============================================================================
# Step 5: analyze_rq_validation  →  validation charts
# ============================================================================

def step_rq_validation(run_haiku=True, run_local=True):
    """Run RQ validation analysis and generate charts.

    Generates: rq1_timescale_mismatch.png, rq2_domain_mismatch.png,
               rq4_overprovisioning.png  (per dataset)
    """
    import analyze_rq_validation as arv

    _section("analyze_rq_validation.py")

    results = {}
    datasets = []
    if run_haiku:
        datasets.append(("Haiku", HAIKU_DIR, HAIKU_FIGURES))
    if run_local:
        datasets.append(("Local", LOCAL_DIR, QWEN3_FIGURES))

    for name, data_dir, figures_dir in datasets:
        if not os.path.exists(data_dir):
            continue
        print(f"\n  --- RQ Validation: {name} ---")
        results[name] = {
            "rq1": arv.analyze_timescale_mismatch(data_dir),
            "rq2": arv.analyze_domain_mismatch(data_dir),
            "rq4": arv.analyze_overprovisioning(data_dir),
        }
        arv.generate_rq_charts(data_dir, figures_dir, name)

    return results


# ============================================================================
# Summary: Print all key numerical values for characterization.md
# ============================================================================

def print_summary(haiku_tasks, haiku_results, local_tasks, local_results,
                  comp_results, comp_stats, extended, rq_results):
    """Print every numerical value referenced in characterization.md."""
    sep = "=" * 70

    print(f"\n\n{sep}")
    print("  CHARACTERIZATION NUMERICAL VALUES SUMMARY")
    print(f"  (Values for characterization.md)")
    print(f"{sep}")

    # ---- 3.1 Experimental Setup ----
    _heading("3.1 Experimental Setup")
    if haiku_tasks:
        print(f"  Haiku valid tasks: {len(haiku_tasks)}")
    if local_tasks:
        print(f"  Local valid tasks: {len(local_tasks)}")

    for ds_name in ["Haiku", "Local"]:
        disk = extended.get(ds_name, {}).get("disk_overhead", {})
        img = disk.get("image_size", {})
        if img:
            print(f"\n  [{ds_name}] Docker Images (n={img.get('count', 0)}):")
            print(f"    Range: {img.get('min_mb', 0)/1024:.1f} – {img.get('max_mb', 0)/1024:.1f} GB")
            print(f"    Avg:   {img.get('avg_mb', 0)/1024:.1f} GB,  Median: {img.get('median_mb', 0)/1024:.1f} GB")
            print(f"    Total: {img.get('total_gb', 0):.1f} GB")
        perm = disk.get("permission_fix_time", {})
        if perm:
            print(f"    Permission fix:  avg {perm.get('avg_s', 0):.1f}s,  max {perm.get('max_s', 0):.1f}s")

    # ---- 3.2 RQ1: Agent Execution Model ----
    _heading("3.2 RQ1: Agent Execution Model")

    # Average execution time
    for ds_name, tasks in [("Haiku", haiku_tasks), ("Local", local_tasks)]:
        if not tasks:
            continue
        durations = [t.claude_time for t in tasks.values() if t.claude_time > 0]
        if durations:
            print(f"  [{ds_name}] Avg execution time: {statistics.mean(durations):.0f}s "
                  f"({statistics.mean(durations)/60:.1f} min)")

    # Tool time ratio
    for ds_name, res in [("Haiku", haiku_results), ("Local", local_results)]:
        ratios = res.get("tools", {}).get("tool_vs_thinking_ratio", [])
        if ratios:
            print(f"  [{ds_name}] Tool time ratio: avg {statistics.mean(ratios):.1f}%, "
                  f"range {min(ratios):.1f}%–{max(ratios):.1f}%")

    # Per-tool average execution time
    for ds_name, res in [("Haiku", haiku_results), ("Local", local_results)]:
        tool_stats = res.get("tools", {}).get("tool_stats", {})
        if not tool_stats:
            continue
        print(f"\n  [{ds_name}] Tool avg execution times:")
        sorted_tools = sorted(tool_stats.items(),
                              key=lambda x: x[1]["total_time"], reverse=True)
        for tname, tstats in sorted_tools:
            if tstats["count"] > 0:
                avg_t = tstats["total_time"] / tstats["count"]
                print(f"    {tname:<15} avg={avg_t:.2f}s  count={tstats['count']}")

    # ---- 3.3 RQ2: Resource Unpredictability ----
    _heading("3.3 RQ2: Resource Unpredictability")

    # Dynamics (change rates)
    for ds_name, res in [("Haiku", haiku_results), ("Local", local_results)]:
        dyn = res.get("dynamics", {})
        cpu_rates = dyn.get("cpu_change_rates", [])
        mem_rates = dyn.get("mem_change_rates", [])
        if cpu_rates:
            print(f"  [{ds_name}] CPU change rate:  max {max(cpu_rates):.1f}%/s,  "
                  f"p95 {np.percentile(cpu_rates, 95):.1f}%/s")
        if mem_rates:
            print(f"  [{ds_name}] Mem change rate:  max {max(mem_rates):.1f}MB/s, "
                  f"p95 {np.percentile(mem_rates, 95):.1f}MB/s")
        total_samples = len(cpu_rates)
        burst_total = dyn.get("total_bursts", 0)
        if total_samples > 0:
            print(f"  [{ds_name}] Burst events: {burst_total} "
                  f"({burst_total/total_samples*100:.1f}% of sample pairs)")

    # Transient bursts (peak/avg)
    for ds_name in ["Haiku", "Local"]:
        bursts = extended.get(ds_name, {}).get("transient_bursts", {})
        top = bursts.get("top_bursts", [])
        if top:
            b = top[0]
            print(f"  [{ds_name}] Most extreme burst: {b['task']}")
            print(f"           Peak={b['peak_mb']:.0f}MB, Avg={b['avg_mb']:.0f}MB, "
                  f"Factor={b['overprov_factor']:.1f}x, Spike~{b['spike_duration_samples']}s")

    # CPU–memory correlation
    for ds_name in ["Haiku", "Local"]:
        corr = extended.get(ds_name, {}).get("cpu_memory_correlation", {}).get("correlation", {})
        if corr:
            print(f"  [{ds_name}] CPU-Mem correlation: avg {corr.get('avg', 0):.2f}, "
                  f"range {corr.get('min', 0):.2f}–{corr.get('max', 0):.2f}")

    # Domain mismatch (peak memory range / CV)
    for ds_name in ["Haiku", "Local"]:
        rq2 = rq_results.get(ds_name, {}).get("rq2", {})
        if rq2:
            print(f"  [{ds_name}] Peak memory range: "
                  f"{rq2.get('peak_mem_min', 0):.0f}–{rq2.get('peak_mem_max', 0):.0f}MB, "
                  f"CV={rq2.get('peak_mem_cv', 0):.1f}%")

    # Haiku vs Local comparison
    if comp_stats:
        n = comp_stats.get("n_tasks", 0)
        print(f"\n  Haiku vs Local ({n} common tasks):")
        print(f"    Haiku avg CPU: {comp_stats.get('haiku_avg_cpu', 0):.1f}%")
        print(f"    Local avg CPU: {comp_stats.get('local_avg_cpu', 0):.1f}%")
        print(f"    CPU ratio:     {comp_stats.get('cpu_ratio', 0):.1f}x")
        if "haiku_avg_time" in comp_stats:
            print(f"    Haiku avg exec time: {comp_stats['haiku_avg_time']:.0f}s")
            print(f"    Local avg exec time: {comp_stats['local_avg_time']:.0f}s")
            print(f"    Time ratio (Local/Haiku): {comp_stats.get('time_ratio', 0):.2f}x")

    # High-CPU sample percentages
    comp_ext = extended.get("comparison", {})
    h_high = comp_ext.get("haiku", {}).get("high_cpu_pct", 0)
    l_high = comp_ext.get("qwen", {}).get("high_cpu_pct", 0)
    if h_high or l_high:
        print(f"    CPU>50% samples:  Haiku {h_high:.1f}%,  Local {l_high:.1f}%")

    # Retry loop patterns
    for ds_name in ["Haiku", "Local"]:
        retry = extended.get(ds_name, {}).get("retry_patterns", {})
        rg = retry.get("retry_groups", {})
        if rg:
            print(f"  [{ds_name}] Retry groups: total={rg.get('total', 0)}, "
                  f"avg={rg.get('avg', 0):.1f}/task, max={rg.get('max', 0)}")

    # Concurrency potential
    for ds_name in ["Haiku", "Local"]:
        cpu_info = extended.get(ds_name, {}).get("concurrency", {}).get("cpu", {})
        if cpu_info:
            print(f"  [{ds_name}] Concurrency: "
                  f"theoretical ~{cpu_info.get('theoretical_concurrency', 0):.0f} instances, "
                  f"practical ~{cpu_info.get('practical_concurrency', 0):.1f} instances, "
                  f"gap {cpu_info.get('concurrency_gap', 0):.1f}x")

    # ---- 3.4 RQ3: Provisioning Efficiency ----
    _heading("3.4 RQ3: Provisioning Efficiency")

    for ds_name in ["Haiku", "Local"]:
        rq4 = rq_results.get(ds_name, {}).get("rq4", {})
        if rq4:
            cpu_util = rq4.get("cpu_utilization", 0)
            mem_util = rq4.get("mem_utilization", 0)
            print(f"  [{ds_name}] Overprovisioning factor:")
            print(f"    CPU:  {rq4.get('cpu_overprov_mean', 0):.1f}x "
                  f"(max {rq4.get('cpu_overprov_max', 0):.1f}x)  "
                  f"→ utilization {cpu_util:.0f}%, waste {100 - cpu_util:.0f}%")
            print(f"    Mem:  {rq4.get('mem_overprov_mean', 0):.1f}x "
                  f"(max {rq4.get('mem_overprov_max', 0):.1f}x)  "
                  f"→ utilization {mem_util:.0f}%, waste {100 - mem_util:.0f}%")

    print(f"\n{sep}")
    print("  END OF SUMMARY")
    print(f"{sep}")


# ============================================================================
# Step 6: Execution overview chart (exec time + phase breakdown)
# ============================================================================

def step_exec_overview_chart(haiku_results, local_results):
    """Generate execution overview figure (2 subplots).

    (a) Execution time distribution (Haiku vs GLM)
    (b) Execution phase breakdown (LLM vs Tool time, stacked bars)

    Saved to comparison_figures/exec_overview.png
    """
    _section("Execution Overview Chart")

    haiku = _scan_setup_data(HAIKU_DIR)
    local = _scan_setup_data(LOCAL_DIR)
    haiku_ratios = haiku_results.get("tools", {}).get("tool_vs_thinking_ratio", [])
    local_ratios = local_results.get("tools", {}).get("tool_vs_thinking_ratio", [])

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.5))

    # ---- (a) Execution time distribution ----
    h_time = [r["claude_time"] / 60 for r in haiku]
    l_time = [r["claude_time"] / 60 for r in local]
    max_min = max((max(h_time) if h_time else 0), (max(l_time) if l_time else 0))
    bins_time = np.linspace(0, min(max_min + 2, 50), 21)
    if h_time:
        ax1.hist(h_time, bins=bins_time, alpha=0.55, color="#2196F3",
                 label=f"Haiku (n={len(h_time)})", edgecolor="white")
    if l_time:
        ax1.hist(l_time, bins=bins_time, alpha=0.55, color="#4CAF50",
                 label=f"GLM (n={len(l_time)})", edgecolor="white")
    all_time = h_time + l_time
    if all_time:
        ax1.axvline(statistics.mean(all_time), color="red", ls="--", lw=1.5,
                    label=f"Mean ({statistics.mean(all_time):.1f} min)")
        ax1.axvline(statistics.median(all_time), color="black", ls=":", lw=1.5,
                    label=f"Median ({statistics.median(all_time):.1f} min)")
    ax1.set_xlabel("Execution Time (minutes)", fontsize=15)
    ax1.set_ylabel("Number of Tasks", fontsize=15)
    ax1.set_title("(a) Task Execution Time", fontsize=16)
    ax1.legend(fontsize=13)
    ax1.tick_params(axis="both", labelsize=13)
    ax1.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax1.grid(alpha=0.3)

    # ---- (b) Phase breakdown stacked bars ----
    haiku_tool_avg = statistics.mean(haiku_ratios) if haiku_ratios else 0
    local_tool_avg = statistics.mean(local_ratios) if local_ratios else 0
    haiku_llm_avg = 100 - haiku_tool_avg
    local_llm_avg = 100 - local_tool_avg

    agents = [f"Haiku (Cloud API)\nn={len(haiku_ratios)}",
              f"GLM (Local GPU)\nn={len(local_ratios)}"]
    tool_vals = [haiku_tool_avg, local_tool_avg]
    llm_vals = [haiku_llm_avg, local_llm_avg]

    x = np.arange(len(agents))
    width = 0.5

    ax2.bar(x, tool_vals, width, label="Tool Execution",
            color="#2196F3", alpha=0.85)
    ax2.bar(x, llm_vals, width, bottom=tool_vals, label="LLM Thinking",
            color="#FF9800", alpha=0.85)

    for i, (tv, lv) in enumerate(zip(tool_vals, llm_vals)):
        ax2.text(i, tv / 2, f"{tv:.1f}%",
                 ha="center", va="center", fontsize=16,
                 fontweight="bold", color="white")
        ax2.text(i, tv + lv / 2, f"{lv:.1f}%",
                 ha="center", va="center", fontsize=16,
                 fontweight="bold", color="white")

    ax2.set_ylabel("Percentage of Execution Time (%)", fontsize=15)
    ax2.set_title("(b) Execution Phase Breakdown", fontsize=16)
    ax2.set_xticks(x)
    ax2.set_xticklabels(agents, fontsize=14)
    ax2.set_ylim(0, 108)
    ax2.legend(loc="upper right", fontsize=13)
    ax2.tick_params(axis="y", labelsize=13)
    ax2.grid(axis="y", alpha=0.3)

    plt.tight_layout()
    os.makedirs(COMPARISON_FIGURES, exist_ok=True)
    out_path = os.path.join(COMPARISON_FIGURES, "exec_overview.png")
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out_path}")

    # Print summary
    for tag, rows in [("Haiku", haiku), ("Local", local)]:
        times = [r["claude_time"] for r in rows]
        if times:
            print(f"  [{tag}] Execution time (n={len(times)}):")
            print(f"    Mean: {statistics.mean(times):.0f}s ({statistics.mean(times)/60:.1f} min),  "
                  f"Median: {statistics.median(times):.0f}s")


# ============================================================================
# Step 6b: Resource distribution boxplots (Haiku vs GLM, single figure)
# ============================================================================

def step_resource_boxplots(haiku_tasks, local_tasks):
    """Combined 2x2 boxplots: Haiku vs GLM for avg/peak CPU/Memory.

    Saved to comparison_figures/resource_boxplots_comparison.png
    """
    _section("Resource Distribution Boxplots (Haiku vs GLM)")

    if not haiku_tasks or not local_tasks:
        print("  WARNING: Need both datasets — skipping")
        return

    h_avg_cpu = [t.cpu_avg for t in haiku_tasks.values() if t.cpu_avg > 0]
    l_avg_cpu = [t.cpu_avg for t in local_tasks.values() if t.cpu_avg > 0]
    h_avg_mem = [t.mem_avg for t in haiku_tasks.values() if t.mem_avg > 0]
    l_avg_mem = [t.mem_avg for t in local_tasks.values() if t.mem_avg > 0]
    h_peak_cpu = [t.cpu_max for t in haiku_tasks.values() if t.cpu_max > 0]
    l_peak_cpu = [t.cpu_max for t in local_tasks.values() if t.cpu_max > 0]
    h_peak_mem = [t.mem_max for t in haiku_tasks.values() if t.mem_max > 0]
    l_peak_mem = [t.mem_max for t in local_tasks.values() if t.mem_max > 0]

    fig, axes = plt.subplots(2, 2, figsize=(12, 9))
    colors_list = ["#2196F3", "#4CAF50"]

    panels = [
        (axes[0, 0], "Average CPU Usage (%)", [h_avg_cpu, l_avg_cpu]),
        (axes[0, 1], "Average Memory Usage (MB)", [h_avg_mem, l_avg_mem]),
        (axes[1, 0], "Peak CPU Usage (%)", [h_peak_cpu, l_peak_cpu]),
        (axes[1, 1], "Peak Memory Usage (MB)", [h_peak_mem, l_peak_mem]),
    ]

    for ax, title, data_pair in panels:
        bp = ax.boxplot(data_pair, labels=["Haiku", "GLM"], patch_artist=True,
                        widths=0.5)
        for patch, c in zip(bp["boxes"], colors_list):
            patch.set_facecolor(c)
            patch.set_alpha(0.6)
        ax.set_title(title, fontsize=12)
        ax.grid(axis="y", alpha=0.3)

    fig.suptitle("Resource Usage Distribution: Haiku vs GLM", fontsize=14)
    plt.tight_layout()
    os.makedirs(COMPARISON_FIGURES, exist_ok=True)
    out_path = os.path.join(COMPARISON_FIGURES, "resource_boxplots_comparison.png")
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out_path}")


# ============================================================================
# Step 6c: Tool time pattern chart (ratio histogram + timeline)
# ============================================================================

def step_tool_time_chart(haiku_results, local_results, haiku_tasks, local_tasks):
    """Generate tool time pattern figure (2 subplots).

    (a) Per-task tool time ratio distribution (histogram)
    (b) Tool usage over normalized execution timeline (stacked area)

    Saved to comparison_figures/tool_time_pattern.png
    """
    _section("Tool Time Pattern Chart")

    haiku_ratios = haiku_results.get("tools", {}).get("tool_vs_thinking_ratio", [])
    local_ratios = local_results.get("tools", {}).get("tool_vs_thinking_ratio", [])

    all_tasks = {}
    if haiku_tasks:
        all_tasks.update(haiku_tasks)
    if local_tasks:
        all_tasks.update(local_tasks)

    if not haiku_ratios and not local_ratios:
        print("  WARNING: No tool ratio data — skipping")
        return

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.5))

    # ---- (a) Tool time ratio histogram ----
    bins = np.linspace(0, 80, 17)
    if haiku_ratios:
        ax1.hist(haiku_ratios, bins=bins, alpha=0.55,
                 color="#2196F3", label=f"Haiku (n={len(haiku_ratios)})",
                 edgecolor="white")
    if local_ratios:
        ax1.hist(local_ratios, bins=bins, alpha=0.55,
                 color="#4CAF50", label=f"GLM (n={len(local_ratios)})",
                 edgecolor="white")
    all_ratios = haiku_ratios + local_ratios
    if all_ratios:
        avg = statistics.mean(all_ratios)
        med = statistics.median(all_ratios)
        ax1.axvline(x=avg, color="red", linestyle="--", linewidth=1.5,
                    label=f"Mean ({avg:.1f}%)")
        ax1.axvline(x=med, color="black", linestyle=":", linewidth=1.5,
                    label=f"Median ({med:.1f}%)")
    ax1.set_xlabel("Tool Time Ratio (%)", fontsize=15)
    ax1.set_ylabel("Number of Tasks", fontsize=15)
    ax1.set_title("(a) Per-Task Tool Time Ratio", fontsize=16)
    ax1.legend(fontsize=13)
    ax1.tick_params(axis="both", labelsize=13)
    ax1.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax1.grid(alpha=0.3)

    # ---- (b) Tool timeline (stacked area) ----
    n_bins = 10
    tool_types = ["Bash", "Read", "Edit", "Grep", "Glob", "Write", "TodoWrite"]
    timeline_data = {t: np.zeros(n_bins) for t in tool_types}
    timeline_data["Other"] = np.zeros(n_bins)

    for task in all_tasks.values():
        if not task.tool_calls or not task.resource_samples:
            continue
        t_start = task.resource_samples[0].epoch
        t_end = task.resource_samples[-1].epoch
        span = t_end - t_start
        if span <= 0:
            continue
        for tc in task.tool_calls:
            if tc.start_time is None:
                continue
            tc_epoch = tc.start_time.timestamp()
            norm_pos = (tc_epoch - t_start) / span
            norm_pos = max(0.0, min(norm_pos, 0.999))
            b = int(norm_pos * n_bins)
            tname = tc.tool if tc.tool in timeline_data else "Other"
            timeline_data[tname][b] += 1

    x_bins = np.arange(n_bins)
    x_labels = [f"{i * 10}-{(i + 1) * 10}%" for i in range(n_bins)]
    cmap = plt.cm.tab10
    active_tools = [t for t in tool_types + ["Other"] if timeline_data[t].sum() > 0]
    stacks = [timeline_data[t] for t in active_tools]
    colors = [cmap(i) for i in range(len(active_tools))]

    ax2.stackplot(x_bins, *stacks, labels=active_tools, colors=colors, alpha=0.8)
    ax2.set_xticks(x_bins)
    ax2.set_xticklabels(x_labels, fontsize=10)
    ax2.set_xlabel("Normalized Execution Time", fontsize=12)
    ax2.set_ylabel("Tool Call Count", fontsize=12)
    ax2.set_title("(b) Tool Usage Over Execution Timeline", fontsize=13)
    ax2.legend(loc="upper right", fontsize=9, ncol=2)
    ax2.grid(alpha=0.3)

    plt.tight_layout()
    os.makedirs(COMPARISON_FIGURES, exist_ok=True)
    out_path = os.path.join(COMPARISON_FIGURES, "tool_time_pattern.png")
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out_path}")
    print(f"  Tool timeline: {sum(int(s.sum()) for s in stacks)} total calls from "
          f"{len(all_tasks)} tasks")


# ============================================================================
# Step 7: Tool & Bash breakdown pie charts (all tasks combined)
# ============================================================================

def _load_bash_categories(base_dir):
    """Load bash command category time from tool_calls.json for each valid task."""
    import re as _re
    from filter_valid_tasks import get_valid_task_names
    from collections import defaultdict

    def _categorize(cmd):
        cl = cmd.strip().lower()
        if _re.search(r"\bpytest\b|\bpython\s+-m\s+pytest\b|\btox\b|\bnose\b|\bunittest\b", cl):
            return "Test Execution"
        if _re.search(r"\bgit\s+(diff|log|status|show|add|commit|checkout|stash|branch|reset)\b", cl):
            return "Git Operations"
        if _re.search(r"\bpip\s+install\b|\bconda\s+install\b|\bapt\b|\byum\b", cl):
            return "Package Install"
        if _re.search(r"\bls\b|\bfind\b|\btree\b|\bwc\b|\bdu\b|\bdf\b", cl):
            return "File Exploration"
        if _re.search(r"\bpython\s+-c\b|\bpython3\s+-c\b", cl):
            return "Python Snippet"
        if _re.search(r"\bpython\b|\bpython3\b", cl):
            return "Python Run"
        if _re.search(r"\bcat\b|\bhead\b|\btail\b|\bgrep\b|\bsed\b|\bawk\b", cl):
            return "Text Processing"
        if _re.search(r"\bcd\b|\bsource\b|\bexport\b|\bchmod\b|\bmkdir\b", cl):
            return "Shell/Environment"
        return "Other"

    from analyze_tool_time_ratio import parse_iso
    valid = get_valid_task_names(base_dir)
    cat_time = defaultdict(float)

    for name in valid:
        attempt = os.path.join(base_dir, name, "attempt_1")
        tc_path = os.path.join(attempt, "tool_calls.json")
        if not os.path.exists(tc_path):
            continue
        with open(tc_path) as f:
            calls = _json.load(f)
        for call in calls:
            if call.get("tool") != "Bash":
                continue
            cmd = call.get("input", {}).get("command", "")
            cat = _categorize(cmd)
            ts_s = parse_iso(call.get("timestamp"))
            ts_e = parse_iso(call.get("end_timestamp"))
            if ts_s and ts_e:
                dur = (ts_e - ts_s).total_seconds()
                if dur >= 0:
                    cat_time[cat] += dur
    return dict(cat_time)


def step_tool_and_bash_pie_chart(haiku_results, local_results):
    """Generate a single figure with 2 pie subplots (Haiku only).

    (a) Tool usage breakdown by total time
    (b) Bash command category breakdown by total time

    Saved to comparison_figures/tool_bash_breakdown.png
    """
    _section("Tool & Bash Breakdown Pie Charts (Haiku)")

    # ---- Use Haiku tool_stats only ----
    from collections import defaultdict
    merged_tool = defaultdict(lambda: {"count": 0, "total_time": 0.0})
    for tname, stats in haiku_results.get("tools", {}).get("tool_stats", {}).items():
        merged_tool[tname]["count"] += stats["count"]
        merged_tool[tname]["total_time"] += stats["total_time"]

    # ---- Haiku bash categories only ----
    merged_bash = _load_bash_categories(HAIKU_DIR)

    if not merged_tool and not merged_bash:
        print("  WARNING: No data — skipping")
        return

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(18, 8))
    cmap = plt.cm.Set3

    # Helper: group slices < threshold into "Other"
    def _group_small(names, sizes, threshold=2.0):
        total = sum(sizes)
        main_n, main_s, other_s = [], [], 0.0
        for n, s in zip(names, sizes):
            if s / total * 100 >= threshold:
                main_n.append(n)
                main_s.append(s)
            else:
                other_s += s
        if other_s > 0:
            main_n.append("Other")
            main_s.append(other_s)
        return main_n, main_s

    # ---- (a) Tool usage pie ----
    sorted_tools = sorted(merged_tool.items(),
                          key=lambda x: x[1]["total_time"], reverse=True)
    sorted_tools = [(n, s) for n, s in sorted_tools if s["total_time"] > 0]
    total_tool_time = sum(s["total_time"] for _, s in sorted_tools)

    t_names = [n for n, _ in sorted_tools]
    t_sizes = [s["total_time"] for _, s in sorted_tools]
    t_names, t_sizes = _group_small(t_names, t_sizes, threshold=2.0)
    colors_a = [cmap(i) for i in range(len(t_names))]

    wedges1, texts1, autotexts1 = ax1.pie(
        t_sizes, labels=t_names, colors=colors_a, startangle=90,
        autopct=lambda p: f"{p:.1f}%" if p >= 3 else "",
        pctdistance=0.75,
        textprops={"fontsize": 14})
    for at in autotexts1:
        at.set_fontsize(13)
        at.set_fontweight("bold")
    ax1.set_title("(a) Tool Usage by Time", fontsize=18, pad=15)

    # ---- (b) Bash category pie ----
    cats = sorted(merged_bash.keys(), key=lambda c: merged_bash[c], reverse=True)
    total_bash = sum(merged_bash[c] for c in cats)
    b_names = list(cats)
    b_sizes = [merged_bash[c] for c in cats]
    b_names, b_sizes = _group_small(b_names, b_sizes, threshold=2.0)
    colors_b = [cmap(i) for i in range(len(b_names))]

    wedges2, texts2, autotexts2 = ax2.pie(
        b_sizes, labels=b_names, colors=colors_b, startangle=90,
        autopct=lambda p: f"{p:.1f}%" if p >= 3 else "",
        pctdistance=0.75,
        textprops={"fontsize": 14})
    for at in autotexts2:
        at.set_fontsize(13)
        at.set_fontweight("bold")
    ax2.set_title("(b) Bash Command Time by Category", fontsize=18, pad=15)

    plt.tight_layout()
    os.makedirs(COMPARISON_FIGURES, exist_ok=True)
    out_path = os.path.join(COMPARISON_FIGURES, "tool_bash_breakdown.png")
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out_path}")

    # ---- Print numerical summary ----
    print(f"\n  Tool usage (combined, total {total_tool_time:.0f}s):")
    for n, s in sorted_tools:
        pct = s["total_time"] / total_tool_time * 100
        avg = s["total_time"] / s["count"] if s["count"] > 0 else 0
        print(f"    {n:<15} {s['total_time']:>9.1f}s  ({pct:5.1f}%)  "
              f"n={s['count']:<5}  avg={avg:.2f}s")

    print(f"\n  Bash categories (combined, total {total_bash:.0f}s):")
    for c in cats:
        pct = merged_bash[c] / total_bash * 100
        print(f"    {c:<20} {merged_bash[c]:>8.1f}s  ({pct:5.1f}%)")


# ============================================================================
# Step 8: Resource profile chart (image size + memory trajectory) — for RQ2
# ============================================================================

def _scan_setup_data(base_dir):
    """Scan a dataset directory, return list of dicts with image/time/perm data."""
    from filter_valid_tasks import get_valid_task_names
    valid = get_valid_task_names(base_dir)
    rows = []
    for name in valid:
        attempt = os.path.join(base_dir, name, "attempt_1")
        rp = os.path.join(attempt, "results.json")
        if not os.path.exists(rp):
            continue
        with open(rp) as f:
            res = _json.load(f)
        img_mb = res.get("image_info", {}).get("size_mb", 0)
        ct = res.get("claude_time", 0)
        pf = res.get("permission_fix_time")
        pf = float(pf) if pf else 0
        if ct > 0:
            rows.append({"task": name, "image_mb": img_mb, "claude_time": ct,
                         "perm_fix": pf})
    return rows


def step_resource_profile_chart(haiku_tasks, local_tasks):
    """Generate resource profile figure for RQ2 (2 subplots).

    (a) Docker image size distribution (deduplicated across datasets)
    (b) Aggregated memory trajectory over execution progress

    Saved to comparison_figures/resource_profile.png
    """
    _section("Resource Profile Chart (RQ2)")

    haiku = _scan_setup_data(HAIKU_DIR)
    local = _scan_setup_data(LOCAL_DIR)

    all_tasks = {}
    if haiku_tasks:
        all_tasks.update(haiku_tasks)
    if local_tasks:
        all_tasks.update(local_tasks)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.5))

    # ---- (a) Image size distribution (deduplicated by task name) ----
    seen_tasks = {}
    for r in haiku + local:
        if r["task"] not in seen_tasks and r["image_mb"] > 0:
            seen_tasks[r["task"]] = r["image_mb"] / 1024
    unique_img = list(seen_tasks.values())
    bins_img = np.linspace(0, 20, 21)
    if unique_img:
        ax1.hist(unique_img, bins=bins_img, alpha=0.75, color="#2196F3",
                 edgecolor="white")
        avg_img = statistics.mean(unique_img)
        med_img = statistics.median(unique_img)
        ax1.axvline(avg_img, color="red", ls="--", lw=1.5,
                    label=f"Mean ({avg_img:.1f} GB)")
        ax1.axvline(med_img, color="black", ls=":", lw=1.5,
                    label=f"Median ({med_img:.1f} GB)")
    ax1.set_xlabel("Image Size (GB)", fontsize=15)
    ax1.set_ylabel("Number of Tasks", fontsize=15)
    ax1.set_title(f"(a) Docker Image Size (n={len(unique_img)})", fontsize=16)
    ax1.legend(fontsize=13)
    ax1.tick_params(axis="both", labelsize=13)
    ax1.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax1.grid(alpha=0.3)

    # ---- (b) Aggregated memory trajectory ----
    n_points = 100
    all_mem_interp = []
    for task in all_tasks.values():
        traj = [s.mem_usage_mb for s in task.resource_samples]
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

        x_mem = np.linspace(0, 100, n_points)
        ax2.fill_between(x_mem, p10, p90, alpha=0.15, color="#2196F3", label="P10–P90")
        ax2.fill_between(x_mem, p25, p75, alpha=0.3, color="#2196F3", label="P25–P75")
        ax2.plot(x_mem, mean_mem, color="#2196F3", linewidth=2, label="Mean")

    ax2.set_xlabel("Execution Progress (%)", fontsize=12)
    ax2.set_ylabel("Memory Usage (MB)", fontsize=12)
    ax2.set_title(f"(b) Aggregated Memory Trajectory (n={len(all_mem_interp)} tasks)",
                  fontsize=13)
    ax2.legend(fontsize=10)
    ax2.grid(alpha=0.3)

    plt.tight_layout()
    os.makedirs(COMPARISON_FIGURES, exist_ok=True)
    out_path = os.path.join(COMPARISON_FIGURES, "resource_profile.png")
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out_path}")

    # Print summary
    if unique_img:
        print(f"\n  Unique images (n={len(unique_img)}, deduplicated by task):")
        print(f"    Range: {min(unique_img):.1f} – {max(unique_img):.1f} GB")
        print(f"    Mean:  {statistics.mean(unique_img):.1f} GB,  Median: {statistics.median(unique_img):.1f} GB")
        print(f"    Total: {sum(unique_img):.0f} GB")
    print(f"  Memory trajectory: {len(all_mem_interp)} tasks with ≥10 samples")


# ============================================================================
# Helpers
# ============================================================================

def _section(title):
    print(f"\n{'='*70}")
    print(f"  [Step] {title}")
    print(f"{'='*70}")


def _heading(title):
    print(f"\n  ## {title}")
    print(f"  {'-'*50}")


def _list_generated_figures():
    """List all generated PNG files in output directories."""
    print(f"\n{'='*70}")
    print("  Generated figures:")
    print(f"{'='*70}")
    for d in [HAIKU_FIGURES, QWEN3_FIGURES, COMPARISON_FIGURES]:
        if not os.path.exists(d):
            continue
        pngs = sorted(f for f in os.listdir(d) if f.endswith(".png"))
        print(f"\n  {d}/  ({len(pngs)} PNG)")
        for p in pngs:
            print(f"    {p}")


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Generate all characterization figures and data for Section 3"
    )
    parser.add_argument("--haiku-only", action="store_true",
                        help="Only analyze Haiku dataset")
    parser.add_argument("--local-only", action="store_true",
                        help="Only analyze Local dataset")
    parser.add_argument("--skip-extended", action="store_true",
                        help="Skip extended insights analysis")
    parser.add_argument("--skip-rq", action="store_true",
                        help="Skip RQ validation analysis")
    args = parser.parse_args()

    run_haiku = not args.local_only
    run_local = not args.haiku_only

    print("=" * 70)
    print("  AgentCgroup Characterization Analysis")
    print("  Generating all figures & data for Section 3")
    print("=" * 70)
    print(f"  Haiku data: {HAIKU_DIR}  {'(skip)' if not run_haiku else ''}")
    print(f"  Local data: {LOCAL_DIR}  {'(skip)' if not run_local else ''}")

    # ------------------------------------------------------------------
    # 1. analyze_swebench_data  →  rq1/rq2/rq3/rq4 figures + report
    # ------------------------------------------------------------------
    haiku_tasks, haiku_results = None, {}
    local_tasks, local_results = None, {}

    if run_haiku:
        haiku_tasks, haiku_results = step_swebench_analysis(
            "haiku", HAIKU_DIR, HAIKU_FIGURES)

    if run_local:
        local_tasks, local_results = step_swebench_analysis(
            "qwen3", LOCAL_DIR, QWEN3_FIGURES)

    # ------------------------------------------------------------------
    # 1b. RQ1 Fig-exec: Execution overview (exec time + phase breakdown)
    # ------------------------------------------------------------------
    if haiku_results and local_results:
        step_exec_overview_chart(haiku_results, local_results)

    # ------------------------------------------------------------------
    # 1c. RQ1 Fig-tool-time: Tool time pattern (ratio + timeline)
    # ------------------------------------------------------------------
    if haiku_results and local_results and (haiku_tasks or local_tasks):
        step_tool_time_chart(haiku_results, local_results, haiku_tasks, local_tasks)

    # ------------------------------------------------------------------
    # 1d. RQ1 Fig-tool-type: Tool & bash breakdown pie charts
    # ------------------------------------------------------------------
    if haiku_results and local_results:
        step_tool_and_bash_pie_chart(haiku_results, local_results)

    # ------------------------------------------------------------------
    # 1e. RQ2 Fig-resource: Resource profile (image size + memory traj)
    # ------------------------------------------------------------------
    if haiku_tasks or local_tasks:
        step_resource_profile_chart(haiku_tasks, local_tasks)

    # ------------------------------------------------------------------
    # 1f. Resource boxplots (optional, kept for reference)
    # ------------------------------------------------------------------
    if haiku_tasks and local_tasks:
        step_resource_boxplots(haiku_tasks, local_tasks)

    # ------------------------------------------------------------------
    # 2. analyze_tool_time_ratio  →  chart_01 … chart_14
    # ------------------------------------------------------------------
    if run_haiku:
        step_tool_time_analysis(HAIKU_DIR, HAIKU_FIGURES)

    if run_local:
        step_tool_time_analysis(LOCAL_DIR, QWEN3_FIGURES)

    # ------------------------------------------------------------------
    # 3. analyze_haiku_vs_qwen  →  comparison_figures/
    # ------------------------------------------------------------------
    comp_results, comp_stats = [], {}
    if run_haiku and run_local:
        comp_results, comp_stats = step_comparison()

    # ------------------------------------------------------------------
    # 4. analyze_extended_insights  →  textual insights
    # ------------------------------------------------------------------
    extended = {}
    if not args.skip_extended:
        extended = step_extended_insights(run_haiku, run_local)

    # ------------------------------------------------------------------
    # 5. analyze_rq_validation  →  validation charts
    # ------------------------------------------------------------------
    rq_results = {}
    if not args.skip_rq:
        rq_results = step_rq_validation(run_haiku, run_local)

    # ------------------------------------------------------------------
    # Summary: all key numerical values for characterization.md
    # ------------------------------------------------------------------
    print_summary(
        haiku_tasks, haiku_results,
        local_tasks, local_results,
        comp_results, comp_stats,
        extended, rq_results,
    )

    # ------------------------------------------------------------------
    # List generated figures
    # ------------------------------------------------------------------
    _list_generated_figures()


if __name__ == "__main__":
    main()
