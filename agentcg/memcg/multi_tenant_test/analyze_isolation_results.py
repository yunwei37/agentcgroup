#!/usr/bin/env python3
"""
analyze_isolation_results.py - 分析隔离策略对比实验结果

比较三种策略:
  1. no_isolation - 无优先级隔离
  2. static       - 静态内存限制
  3. bpf          - 动态 BPF 优先级隔离
"""

import json
import os
import sys
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from pathlib import Path


@dataclass
class ExperimentResult:
    """单次实验结果"""
    strategy: str
    run: int
    exp_dir: str

    # 配置
    total_memory_mb: float = 0
    speed_factor: float = 1.0

    # HIGH session
    high_time: float = 0
    high_peak_mb: float = 0
    high_oom: int = 0
    high_events_high: int = 0

    # LOW sessions
    low1_time: float = 0
    low1_peak_mb: float = 0
    low1_oom: int = 0
    low1_events_high: int = 0

    low2_time: float = 0
    low2_peak_mb: float = 0
    low2_oom: int = 0
    low2_events_high: int = 0

    # BPF 统计 (仅 BPF 策略)
    bpf_delay_calls: int = 0
    bpf_delay_active: int = 0
    bpf_below_low_calls: int = 0

    # 计算指标
    @property
    def low_avg_time(self) -> float:
        times = [t for t in [self.low1_time, self.low2_time] if t > 0]
        return sum(times) / len(times) if times else 0

    @property
    def priority_ratio(self) -> float:
        """LOW/HIGH 时间比值，越大表示隔离效果越好"""
        if self.high_time > 0:
            return self.low_avg_time / self.high_time
        return 1.0

    @property
    def total_oom(self) -> int:
        return self.high_oom + self.low1_oom + self.low2_oom

    @property
    def total_events_high(self) -> int:
        return self.high_events_high + self.low1_events_high + self.low2_events_high


def load_experiment(exp_dir: str) -> Optional[ExperimentResult]:
    """加载单个实验结果"""
    config_path = os.path.join(exp_dir, "config.json")
    if not os.path.exists(config_path):
        return None

    try:
        with open(config_path) as f:
            config = json.load(f)
    except Exception as e:
        print(f"Failed to load config from {exp_dir}: {e}", file=sys.stderr)
        return None

    result = ExperimentResult(
        strategy=config.get("strategy", "unknown"),
        run=config.get("run", 0),
        exp_dir=exp_dir,
        total_memory_mb=config.get("total_memory_mb", 0),
        speed_factor=config.get("speed_factor", 1.0)
    )

    # 加载 HIGH 结果
    high_result_path = os.path.join(exp_dir, "high_result.json")
    if os.path.exists(high_result_path):
        try:
            with open(high_result_path) as f:
                high = json.load(f)
            result.high_time = high.get("total_time", high.get("completion_time_sec", 0))
            result.high_peak_mb = high.get("peak_memory_mb", 0)
            result.high_oom = high.get("oom_count", 0)
            result.high_events_high = high.get("events_delta", {}).get("high", 0)
        except Exception as e:
            print(f"Failed to load high result: {e}", file=sys.stderr)

    # 加载 LOW1 结果
    low1_result_path = os.path.join(exp_dir, "low1_result.json")
    if os.path.exists(low1_result_path):
        try:
            with open(low1_result_path) as f:
                low1 = json.load(f)
            result.low1_time = low1.get("total_time", low1.get("completion_time_sec", 0))
            result.low1_peak_mb = low1.get("peak_memory_mb", 0)
            result.low1_oom = low1.get("oom_count", 0)
            result.low1_events_high = low1.get("events_delta", {}).get("high", 0)
        except Exception as e:
            print(f"Failed to load low1 result: {e}", file=sys.stderr)

    # 加载 LOW2 结果
    low2_result_path = os.path.join(exp_dir, "low2_result.json")
    if os.path.exists(low2_result_path):
        try:
            with open(low2_result_path) as f:
                low2 = json.load(f)
            result.low2_time = low2.get("total_time", low2.get("completion_time_sec", 0))
            result.low2_peak_mb = low2.get("peak_memory_mb", 0)
            result.low2_oom = low2.get("oom_count", 0)
            result.low2_events_high = low2.get("events_delta", {}).get("high", 0)
        except Exception as e:
            print(f"Failed to load low2 result: {e}", file=sys.stderr)

    # 加载 BPF 统计
    bpf_log_path = os.path.join(exp_dir, "bpf_loader.log")
    if os.path.exists(bpf_log_path):
        try:
            with open(bpf_log_path) as f:
                log_content = f.read()
            # 解析 "get_high_delay_ms calls: X (active: Y)"
            import re
            delay_match = re.search(r"get_high_delay_ms calls: (\d+) \(active: (\d+)\)", log_content)
            if delay_match:
                result.bpf_delay_calls = int(delay_match.group(1))
                result.bpf_delay_active = int(delay_match.group(2))
            below_match = re.search(r"below_low calls: (\d+)", log_content)
            if below_match:
                result.bpf_below_low_calls = int(below_match.group(1))
        except Exception as e:
            print(f"Failed to parse BPF log: {e}", file=sys.stderr)

    return result


def load_all_experiments(results_dir: str) -> List[ExperimentResult]:
    """加载所有实验结果"""
    results = []

    if not os.path.exists(results_dir):
        print(f"Results directory not found: {results_dir}", file=sys.stderr)
        return results

    for entry in sorted(os.listdir(results_dir)):
        exp_dir = os.path.join(results_dir, entry)
        if os.path.isdir(exp_dir):
            result = load_experiment(exp_dir)
            if result:
                results.append(result)

    return results


def group_by_strategy(results: List[ExperimentResult]) -> Dict[str, List[ExperimentResult]]:
    """按策略分组"""
    groups = {}
    for r in results:
        if r.strategy not in groups:
            groups[r.strategy] = []
        groups[r.strategy].append(r)
    return groups


def calculate_stats(values: List[float]) -> Dict[str, float]:
    """计算统计值"""
    if not values:
        return {"mean": 0, "min": 0, "max": 0, "std": 0}

    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values) if len(values) > 1 else 0
    std = variance ** 0.5

    return {
        "mean": mean,
        "min": min(values),
        "max": max(values),
        "std": std
    }


def print_comparison_table(groups: Dict[str, List[ExperimentResult]]):
    """打印对比表格"""
    print("\n" + "=" * 100)
    print("ISOLATION STRATEGY COMPARISON")
    print("=" * 100)

    # 表头
    headers = ["Strategy", "HIGH Time", "LOW Avg", "Ratio", "OOM", "Events.high", "BPF Active"]
    print(f"\n{'Strategy':<15} {'HIGH Time':>12} {'LOW Avg':>12} {'Ratio':>10} "
          f"{'OOM':>6} {'Events.high':>12} {'BPF Active':>12}")
    print("-" * 100)

    strategy_order = ["no_isolation", "static", "bpf"]
    strategy_stats = {}

    for strategy in strategy_order:
        if strategy not in groups:
            continue

        results = groups[strategy]

        # 计算平均值
        high_times = [r.high_time for r in results if r.high_time > 0]
        low_avg_times = [r.low_avg_time for r in results if r.low_avg_time > 0]
        ratios = [r.priority_ratio for r in results if r.priority_ratio > 0]
        ooms = [r.total_oom for r in results]
        events = [r.total_events_high for r in results]
        bpf_active = [r.bpf_delay_active for r in results]

        high_stats = calculate_stats(high_times)
        low_stats = calculate_stats(low_avg_times)
        ratio_stats = calculate_stats(ratios)
        oom_total = sum(ooms)
        events_total = sum(events)
        bpf_total = sum(bpf_active)

        strategy_stats[strategy] = {
            "high_time": high_stats["mean"],
            "low_avg_time": low_stats["mean"],
            "ratio": ratio_stats["mean"],
            "oom": oom_total,
            "events_high": events_total,
            "bpf_active": bpf_total
        }

        strategy_name = {
            "no_isolation": "NoIsolation",
            "static": "Static",
            "bpf": "BPF-Priority"
        }.get(strategy, strategy)

        print(f"{strategy_name:<15} {high_stats['mean']:>10.1f}s {low_stats['mean']:>10.1f}s "
              f"{ratio_stats['mean']:>9.2f}x {oom_total:>6} {events_total:>12} {bpf_total:>12}")

    # 打印分析
    print("\n" + "-" * 100)
    print("ANALYSIS")
    print("-" * 100)

    if "no_isolation" in strategy_stats and "bpf" in strategy_stats:
        no_iso = strategy_stats["no_isolation"]
        bpf = strategy_stats["bpf"]

        ratio_improvement = (bpf["ratio"] / no_iso["ratio"] - 1) * 100 if no_iso["ratio"] > 0 else 0
        high_time_change = (bpf["high_time"] / no_iso["high_time"] - 1) * 100 if no_iso["high_time"] > 0 else 0

        print(f"\nBPF vs NoIsolation:")
        print(f"  - Priority ratio: {no_iso['ratio']:.2f}x -> {bpf['ratio']:.2f}x ({ratio_improvement:+.1f}% improvement)")
        print(f"  - HIGH time: {no_iso['high_time']:.1f}s -> {bpf['high_time']:.1f}s ({high_time_change:+.1f}%)")
        print(f"  - BPF delay activated: {bpf['bpf_active']} times")

    if "static" in strategy_stats and "bpf" in strategy_stats:
        static = strategy_stats["static"]
        bpf = strategy_stats["bpf"]

        print(f"\nBPF vs Static:")
        print(f"  - OOM events: {static['oom']} -> {bpf['oom']}")
        print(f"  - Priority ratio: {static['ratio']:.2f}x -> {bpf['ratio']:.2f}x")

    return strategy_stats


def print_detailed_results(results: List[ExperimentResult]):
    """打印详细结果"""
    print("\n" + "=" * 100)
    print("DETAILED RESULTS")
    print("=" * 100)

    for r in results:
        print(f"\n--- {r.strategy} Run {r.run} ---")
        print(f"  Directory: {r.exp_dir}")
        print(f"  Total memory limit: {r.total_memory_mb}MB")
        print(f"  HIGH: time={r.high_time:.1f}s, peak={r.high_peak_mb:.0f}MB, OOM={r.high_oom}")
        print(f"  LOW1: time={r.low1_time:.1f}s, peak={r.low1_peak_mb:.0f}MB, OOM={r.low1_oom}")
        print(f"  LOW2: time={r.low2_time:.1f}s, peak={r.low2_peak_mb:.0f}MB, OOM={r.low2_oom}")
        print(f"  Priority ratio: {r.priority_ratio:.2f}x")
        if r.strategy == "bpf":
            print(f"  BPF: delay_calls={r.bpf_delay_calls}, active={r.bpf_delay_active}")


def generate_markdown_report(groups: Dict[str, List[ExperimentResult]],
                             strategy_stats: Dict, output_path: str):
    """生成 Markdown 报告"""
    lines = []
    lines.append("# 隔离策略对比实验结果")
    lines.append("")
    lines.append("## 实验配置")
    lines.append("")

    # 从第一个结果获取配置
    first_result = next(iter(next(iter(groups.values()))))
    lines.append(f"- 总内存限制: {first_result.total_memory_mb}MB")
    lines.append(f"- 回放速度: {first_result.speed_factor}x")
    lines.append("")

    lines.append("## 策略对比")
    lines.append("")
    lines.append("| 策略 | HIGH 时间 | LOW 平均 | 优先级比 | OOM | memory.high 事件 | BPF 触发 |")
    lines.append("|------|----------|----------|---------|-----|-----------------|---------|")

    strategy_order = ["no_isolation", "static", "bpf"]
    strategy_names = {
        "no_isolation": "无隔离",
        "static": "静态限制",
        "bpf": "BPF 动态"
    }

    for strategy in strategy_order:
        if strategy not in strategy_stats:
            continue
        s = strategy_stats[strategy]
        name = strategy_names.get(strategy, strategy)
        lines.append(f"| {name} | {s['high_time']:.1f}s | {s['low_avg_time']:.1f}s | "
                     f"{s['ratio']:.2f}x | {s['oom']} | {s['events_high']} | {s['bpf_active']} |")

    lines.append("")
    lines.append("## 分析")
    lines.append("")

    if "no_isolation" in strategy_stats and "bpf" in strategy_stats:
        no_iso = strategy_stats["no_isolation"]
        bpf = strategy_stats["bpf"]
        ratio_improvement = (bpf["ratio"] / no_iso["ratio"] - 1) * 100 if no_iso["ratio"] > 0 else 0

        lines.append(f"### BPF vs 无隔离")
        lines.append("")
        lines.append(f"- **优先级比改善**: {no_iso['ratio']:.2f}x -> {bpf['ratio']:.2f}x ({ratio_improvement:+.1f}%)")
        lines.append(f"- **BPF 延迟触发次数**: {bpf['bpf_active']}")
        lines.append("")

    if "static" in strategy_stats and "bpf" in strategy_stats:
        static = strategy_stats["static"]
        bpf = strategy_stats["bpf"]

        lines.append(f"### BPF vs 静态限制")
        lines.append("")
        lines.append(f"- **OOM 事件**: {static['oom']} -> {bpf['oom']}")
        lines.append(f"- **优先级比**: {static['ratio']:.2f}x -> {bpf['ratio']:.2f}x")
        lines.append("")

    lines.append("## 结论")
    lines.append("")
    if "bpf" in strategy_stats and "no_isolation" in strategy_stats:
        improvement = (strategy_stats["bpf"]["ratio"] / strategy_stats["no_isolation"]["ratio"] - 1) * 100
        lines.append(f"BPF 动态优先级隔离相比无隔离策略，优先级比改善了 **{improvement:.1f}%**，")
        lines.append(f"有效保护了 HIGH 优先级任务的完成时间。")

    with open(output_path, 'w') as f:
        f.write('\n'.join(lines))

    print(f"\nMarkdown report saved to: {output_path}")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results_dir>")
        sys.exit(1)

    results_dir = sys.argv[1]

    print(f"Loading experiments from: {results_dir}")

    results = load_all_experiments(results_dir)

    if not results:
        print("No experiments found!")
        sys.exit(1)

    print(f"Loaded {len(results)} experiment(s)")

    groups = group_by_strategy(results)
    print(f"Strategies: {list(groups.keys())}")

    strategy_stats = print_comparison_table(groups)
    print_detailed_results(results)

    # 生成 Markdown 报告
    report_path = os.path.join(results_dir, "COMPARISON_REPORT.md")
    generate_markdown_report(groups, strategy_stats, report_path)


if __name__ == "__main__":
    main()
