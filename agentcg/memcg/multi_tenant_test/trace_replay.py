#!/usr/bin/env python3
"""
trace_replay.py - 基于真实 agent trace 回放内存使用模式

按照 trace 中的时序分配/释放内存，模拟真实 agent 工作负载。
"""

import argparse
import json
import os
import sys
import time
from typing import List, Dict, Tuple


def parse_mem_usage(mem_str: str) -> float:
    """解析 '147.9MB / 16.19GB' 格式，返回 bytes"""
    used = mem_str.split('/')[0].strip()
    if 'GB' in used:
        return float(used.replace('GB', '')) * 1024 * 1024 * 1024
    elif 'MB' in used:
        return float(used.replace('MB', '')) * 1024 * 1024
    elif 'kB' in used:
        return float(used.replace('kB', '')) * 1024
    elif 'B' in used:
        return float(used.replace('B', ''))
    return 0


def load_trace(trace_path: str) -> List[Dict]:
    """加载 resources.json trace 文件"""
    with open(trace_path) as f:
        data = json.load(f)

    samples = []
    for s in data.get('samples', []):
        mem_bytes = parse_mem_usage(s.get('mem_usage', '0MB'))
        cpu_str = str(s.get('cpu_percent', '0')).replace('%', '')
        try:
            cpu_pct = float(cpu_str)
        except ValueError:
            cpu_pct = 0.0

        samples.append({
            'epoch': s.get('epoch', 0),
            'mem_bytes': int(mem_bytes),
            'cpu_percent': cpu_pct
        })

    return samples


def join_cgroup(cgroup_path: str) -> bool:
    """将当前进程加入指定 cgroup"""
    procs_file = os.path.join(cgroup_path, "cgroup.procs")
    try:
        with open(procs_file, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception as e:
        print(f"Warning: Failed to join cgroup {cgroup_path}: {e}", file=sys.stderr)
        return False


def read_memory_events(cgroup_path: str) -> Dict[str, int]:
    """读取 cgroup 的 memory.events"""
    events = {}
    events_file = os.path.join(cgroup_path, "memory.events")
    try:
        with open(events_file, "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) == 2:
                    events[parts[0]] = int(parts[1])
    except:
        pass
    return events


def replay_memory_trace(samples: List[Dict], speed_factor: float = 1.0,
                        base_memory_mb: float = 0, name: str = "worker") -> Dict:
    """
    按 trace 时序分配/释放内存

    Args:
        samples: 采样数据列表
        speed_factor: 加速因子 (1.0 = 实时, 10.0 = 10倍速)
        base_memory_mb: 额外基线内存 (模拟 Claude Code 进程)
        name: 工作者名称

    Returns:
        结果字典
    """
    if not samples:
        return {"error": "No samples"}

    # 分配基线内存
    base_buffers = []
    if base_memory_mb > 0:
        print(f"[{name}] Allocating {base_memory_mb:.0f}MB base memory...", file=sys.stderr)
        try:
            base_size = int(base_memory_mb * 1024 * 1024)
            buf = bytearray(base_size)
            # Touch pages
            for i in range(0, len(buf), 4096):
                buf[i] = 1
            base_buffers.append(buf)
            print(f"[{name}] Base memory allocated", file=sys.stderr)
        except MemoryError as e:
            print(f"[{name}] Failed to allocate base memory: {e}", file=sys.stderr)

    allocated_buffers = []
    start_time = time.time()
    trace_start = samples[0]['epoch']
    peak_mem = 0
    oom_count = 0
    allocation_events = []

    print(f"[{name}] Starting trace replay ({len(samples)} samples, {speed_factor}x speed)", file=sys.stderr)

    for i, sample in enumerate(samples):
        # 计算应该等待的时间
        trace_elapsed = sample['epoch'] - trace_start
        real_elapsed = time.time() - start_time
        wait_time = (trace_elapsed / speed_factor) - real_elapsed

        if wait_time > 0:
            time.sleep(wait_time)

        target_mem = sample['mem_bytes']
        current_mem = sum(len(buf) for buf in allocated_buffers)

        try:
            if target_mem > current_mem:
                # 需要分配更多内存
                alloc_size = target_mem - current_mem
                # 分块分配避免一次性分配太大
                chunk_size = 64 * 1024 * 1024  # 64MB chunks
                alloc_start = time.time()
                while alloc_size > 0:
                    size = min(chunk_size, alloc_size)
                    buf = bytearray(size)
                    # Touch pages
                    for j in range(0, len(buf), 4096):
                        buf[j] = 1
                    allocated_buffers.append(buf)
                    alloc_size -= size
                alloc_latency_ms = (time.time() - alloc_start) * 1000

                allocation_events.append({
                    'time': time.time() - start_time,
                    'action': 'alloc',
                    'target_mb': target_mem / (1024 * 1024),
                    'actual_mb': sum(len(b) for b in allocated_buffers) / (1024 * 1024),
                    'latency_ms': alloc_latency_ms  # 记录分配延迟
                })

            elif target_mem < current_mem:
                # 释放内存
                while allocated_buffers and sum(len(b) for b in allocated_buffers) > target_mem:
                    allocated_buffers.pop()

                allocation_events.append({
                    'time': time.time() - start_time,
                    'action': 'free',
                    'target_mb': target_mem / (1024 * 1024),
                    'actual_mb': sum(len(b) for b in allocated_buffers) / (1024 * 1024)
                })

        except MemoryError:
            oom_count += 1
            print(f"[{name}] OOM at sample {i}, target={target_mem/(1024*1024):.1f}MB", file=sys.stderr)

        current_mem = sum(len(buf) for buf in allocated_buffers) + sum(len(b) for b in base_buffers)
        peak_mem = max(peak_mem, current_mem)

        # 进度报告 (每 10% 报告一次)
        if i > 0 and i % max(1, len(samples) // 10) == 0:
            progress = i * 100 // len(samples)
            print(f"[{name}] Progress: {progress}%, mem={current_mem/(1024*1024):.1f}MB", file=sys.stderr)

    completion_time = time.time() - start_time

    # 清理
    allocated_buffers.clear()
    base_buffers.clear()

    # 计算分配延迟统计
    alloc_latencies = [e['latency_ms'] for e in allocation_events if e.get('latency_ms')]
    latency_stats = {}
    if alloc_latencies:
        sorted_latencies = sorted(alloc_latencies)
        n = len(sorted_latencies)
        latency_stats = {
            'count': n,
            'min_ms': sorted_latencies[0],
            'max_ms': sorted_latencies[-1],
            'avg_ms': sum(sorted_latencies) / n,
            'p50_ms': sorted_latencies[n // 2],
            'p95_ms': sorted_latencies[int(n * 0.95)] if n >= 20 else sorted_latencies[-1],
            'p99_ms': sorted_latencies[int(n * 0.99)] if n >= 100 else sorted_latencies[-1],
        }

    return {
        'completion_time_sec': completion_time,
        'peak_memory_bytes': peak_mem,
        'peak_memory_mb': peak_mem / (1024 * 1024),
        'oom_count': oom_count,
        'samples_processed': len(samples),
        'speed_factor': speed_factor,
        'base_memory_mb': base_memory_mb,
        'allocation_events_count': len(allocation_events),
        'latency_stats': latency_stats,
        'allocation_latencies_ms': alloc_latencies  # 完整延迟列表
    }


def main():
    parser = argparse.ArgumentParser(description="Replay memory trace from agent experiments")
    parser.add_argument("trace_path", help="Path to resources.json")
    parser.add_argument("--speed", type=float, default=10.0, help="Speed factor (default: 10x)")
    parser.add_argument("--base-memory-mb", type=float, default=0,
                        help="Extra base memory to allocate (simulates Claude Code process)")
    parser.add_argument("--cgroup", help="Cgroup path to join")
    parser.add_argument("--name", default="worker", help="Worker name for logging")
    parser.add_argument("--output", help="Output JSON file for results")
    args = parser.parse_args()

    result = {
        "name": args.name,
        "trace_path": args.trace_path,
        "speed_factor": args.speed,
        "base_memory_mb": args.base_memory_mb,
        "pid": os.getpid(),
    }

    # Join cgroup if specified
    if args.cgroup:
        if not join_cgroup(args.cgroup):
            result["error"] = "Failed to join cgroup"
            if args.output:
                with open(args.output, "w") as f:
                    json.dump(result, f, indent=2)
            return 1
        result["cgroup"] = args.cgroup
        print(f"[{args.name}] Joined cgroup: {args.cgroup}", file=sys.stderr)

        # Record events before
        result["events_before"] = read_memory_events(args.cgroup)

    # Load trace
    print(f"[{args.name}] Loading trace from {args.trace_path}", file=sys.stderr)
    try:
        samples = load_trace(args.trace_path)
        result["trace_samples"] = len(samples)
        if samples:
            trace_duration = samples[-1]['epoch'] - samples[0]['epoch']
            result["trace_duration_sec"] = trace_duration
            mem_values = [s['mem_bytes'] for s in samples]
            result["trace_mem_avg_mb"] = sum(mem_values) / len(mem_values) / (1024 * 1024)
            result["trace_mem_max_mb"] = max(mem_values) / (1024 * 1024)
            print(f"[{args.name}] Trace: {len(samples)} samples, {trace_duration:.0f}s, "
                  f"avg={result['trace_mem_avg_mb']:.0f}MB, max={result['trace_mem_max_mb']:.0f}MB",
                  file=sys.stderr)
    except Exception as e:
        result["error"] = f"Failed to load trace: {e}"
        if args.output:
            with open(args.output, "w") as f:
                json.dump(result, f, indent=2)
        return 1

    # Record start time
    result["start_time"] = time.time()

    # Run replay
    replay_result = replay_memory_trace(
        samples,
        speed_factor=args.speed,
        base_memory_mb=args.base_memory_mb,
        name=args.name
    )
    result.update(replay_result)

    # Record end time
    result["end_time"] = time.time()
    result["total_time"] = result["end_time"] - result["start_time"]

    # Record events after
    if args.cgroup:
        result["events_after"] = read_memory_events(args.cgroup)
        result["events_delta"] = {
            k: result["events_after"].get(k, 0) - result["events_before"].get(k, 0)
            for k in set(result["events_before"].keys()) | set(result["events_after"].keys())
        }

    print(f"[{args.name}] Completed in {result['total_time']:.2f}s, "
          f"peak={result.get('peak_memory_mb', 0):.0f}MB, OOM={result.get('oom_count', 0)}",
          file=sys.stderr)

    # Output results
    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)
        print(f"[{args.name}] Results saved to {args.output}", file=sys.stderr)
    else:
        print(json.dumps(result, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
