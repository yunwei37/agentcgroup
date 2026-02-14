#!/usr/bin/env python3
"""
show_results.py - 显示实验结果
"""

import json
import sys
from pathlib import Path


def load_result(path):
    """加载单个结果文件"""
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return None


def show_results(exp_dir):
    """显示实验结果"""
    exp_dir = Path(exp_dir)

    # 加载配置
    config_path = exp_dir / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
        print(f"Experiment: {config.get('experiment', 'unknown')}")
        print(f"BPF enabled: {config.get('bpf_enabled', False)}")
        print(f"Total memory: {config.get('total_memory_mb', '?')} MB")
        print(f"Per process: {config.get('per_process_mb', '?')} MB")
        print()

    # 加载各进程结果
    results = {}
    for name in ["high", "low1", "low2"]:
        result_path = exp_dir / f"{name}_result.json"
        if result_path.exists():
            results[name] = load_result(result_path)

    if not results:
        print("No results found!")
        return

    # 显示结果表格
    print("=" * 60)
    print(f"{'Process':<10} {'Allocated':<12} {'Alloc Time':<12} {'Total Time':<12}")
    print("=" * 60)

    for name in ["high", "low1", "low2"]:
        if name in results:
            r = results[name]
            alloc_mb = r.get('allocated_mb', 0)
            alloc_time = r.get('allocation_time', 0)
            total_time = r.get('total_time', 0)
            print(f"{name.upper():<10} {alloc_mb:>8.1f} MB  {alloc_time:>8.2f} s   {total_time:>8.2f} s")

    print("=" * 60)

    # 显示 memory.events
    print("\nMemory Events (delta):")
    print("-" * 40)
    for name in ["high", "low1", "low2"]:
        if name in results:
            r = results[name]
            events = r.get('events_delta', {})
            high_count = events.get('high', 0)
            max_count = events.get('max', 0)
            print(f"{name.upper():<10} high={high_count:<6} max={max_count}")

    # 计算统计
    print("\n" + "=" * 60)
    print("Summary:")
    print("=" * 60)

    if "high" in results and ("low1" in results or "low2" in results):
        high_time = results["high"].get("total_time", 0)

        low_times = []
        if "low1" in results:
            low_times.append(results["low1"].get("total_time", 0))
        if "low2" in results:
            low_times.append(results["low2"].get("total_time", 0))

        if low_times:
            avg_low_time = sum(low_times) / len(low_times)
            ratio = avg_low_time / high_time if high_time > 0 else 0

            print(f"HIGH completion time:     {high_time:.2f} s")
            print(f"LOW avg completion time:  {avg_low_time:.2f} s")
            print(f"LOW/HIGH ratio:           {ratio:.2f}x")

            if ratio > 1.5:
                print("\n✓ LOW processes are significantly slower than HIGH")
                print("  This indicates priority isolation is working!")
            else:
                print("\n• LOW and HIGH have similar completion times")
                print("  No significant priority isolation observed")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <experiment_dir>")
        print(f"Example: {sys.argv[0]} results/baseline_20240101_120000")
        return 1

    show_results(sys.argv[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
