#!/usr/bin/env python3
"""
overhead_microbench.py - Microbenchmark for BPF memcg struct_ops overhead

This measures the overhead of BPF by performing rapid memory allocations
and measuring latency with high precision.

Unlike trace_replay which simulates realistic workloads, this microbenchmark
is designed to isolate and measure BPF overhead specifically.

Metrics:
  - Allocation latency per operation (nanoseconds)
  - Throughput (allocations per second)
  - mmap syscall overhead
"""

import argparse
import ctypes
import json
import mmap
import os
import sys
import time
from typing import Dict, List


def join_cgroup(cgroup_path: str) -> bool:
    """Join the specified cgroup."""
    procs_file = os.path.join(cgroup_path, "cgroup.procs")
    try:
        with open(procs_file, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception as e:
        print(f"Failed to join cgroup {cgroup_path}: {e}", file=sys.stderr)
        return False


def warmup(iterations: int = 100, size_mb: float = 10):
    """Warmup phase to stabilize the system."""
    print(f"Warming up ({iterations} iterations)...", file=sys.stderr)
    buffers = []
    size = int(size_mb * 1024 * 1024)
    for _ in range(iterations):
        buf = bytearray(size)
        # Touch pages
        for j in range(0, len(buf), 4096):
            buf[j] = 1
        buffers.append(buf)
        if len(buffers) > 10:
            buffers.pop(0)
    buffers.clear()


def measure_alloc_latency(size_mb: float, iterations: int) -> List[float]:
    """
    Measure allocation latency for a given size.
    Returns list of latencies in nanoseconds.
    """
    size = int(size_mb * 1024 * 1024)
    latencies = []
    buffers = []

    for i in range(iterations):
        # Measure allocation
        start = time.perf_counter_ns()
        buf = bytearray(size)
        # Touch all pages to ensure actual allocation
        for j in range(0, len(buf), 4096):
            buf[j] = 1
        end = time.perf_counter_ns()

        latencies.append(end - start)
        buffers.append(buf)

        # Keep memory pool limited
        if len(buffers) > 20:
            buffers.pop(0)

        # Progress
        if (i + 1) % 100 == 0:
            print(f"  Progress: {i+1}/{iterations}", file=sys.stderr)

    buffers.clear()
    return latencies


def measure_mmap_latency(size_mb: float, iterations: int) -> List[float]:
    """
    Measure mmap latency directly (lower level than bytearray).
    Returns list of latencies in nanoseconds.
    """
    size = int(size_mb * 1024 * 1024)
    latencies = []
    mappings = []

    for i in range(iterations):
        # Measure mmap + touch
        start = time.perf_counter_ns()
        m = mmap.mmap(-1, size, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        # Touch pages
        for j in range(0, size, 4096):
            m[j] = 1
        end = time.perf_counter_ns()

        latencies.append(end - start)
        mappings.append(m)

        # Keep memory pool limited
        if len(mappings) > 20:
            old = mappings.pop(0)
            old.close()

    for m in mappings:
        m.close()

    return latencies


def measure_throughput(size_mb: float, duration_sec: float) -> Dict:
    """
    Measure allocation throughput over a fixed duration.
    """
    size = int(size_mb * 1024 * 1024)
    count = 0
    buffers = []

    start = time.perf_counter()
    end_time = start + duration_sec

    while time.perf_counter() < end_time:
        buf = bytearray(size)
        for j in range(0, len(buf), 4096):
            buf[j] = 1
        buffers.append(buf)
        count += 1

        if len(buffers) > 20:
            buffers.pop(0)

    elapsed = time.perf_counter() - start
    buffers.clear()

    return {
        "allocations": count,
        "duration_sec": elapsed,
        "throughput_per_sec": count / elapsed,
        "mb_per_sec": (count * size_mb) / elapsed,
    }


def calc_percentile(sorted_list: List[float], p: float) -> float:
    """Calculate percentile from sorted list."""
    if not sorted_list:
        return 0
    idx = int(len(sorted_list) * p / 100)
    idx = min(idx, len(sorted_list) - 1)
    return sorted_list[idx]


def calc_stats(latencies: List[float]) -> Dict:
    """Calculate latency statistics."""
    if not latencies:
        return {}

    sorted_lat = sorted(latencies)
    n = len(sorted_lat)

    return {
        "count": n,
        "min_ns": sorted_lat[0],
        "max_ns": sorted_lat[-1],
        "avg_ns": sum(sorted_lat) / n,
        "p50_ns": sorted_lat[n // 2],
        "p90_ns": calc_percentile(sorted_lat, 90),
        "p95_ns": calc_percentile(sorted_lat, 95),
        "p99_ns": calc_percentile(sorted_lat, 99),
        "p999_ns": calc_percentile(sorted_lat, 99.9),
        # Also in ms for readability
        "avg_ms": sum(sorted_lat) / n / 1e6,
        "p50_ms": sorted_lat[n // 2] / 1e6,
        "p95_ms": calc_percentile(sorted_lat, 95) / 1e6,
        "p99_ms": calc_percentile(sorted_lat, 99) / 1e6,
        "max_ms": sorted_lat[-1] / 1e6,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Microbenchmark for BPF memcg overhead measurement"
    )
    parser.add_argument("--cgroup", help="Cgroup path to join")
    parser.add_argument("--name", default="bench", help="Benchmark name")
    parser.add_argument("--size-mb", type=float, default=10,
                        help="Allocation size in MB (default: 10)")
    parser.add_argument("--iterations", type=int, default=500,
                        help="Number of iterations (default: 500)")
    parser.add_argument("--warmup", type=int, default=50,
                        help="Warmup iterations (default: 50)")
    parser.add_argument("--throughput-duration", type=float, default=5.0,
                        help="Throughput test duration in seconds (default: 5)")
    parser.add_argument("--output", help="Output JSON file")
    parser.add_argument("--skip-mmap", action="store_true",
                        help="Skip mmap benchmark")
    args = parser.parse_args()

    result = {
        "name": args.name,
        "pid": os.getpid(),
        "size_mb": args.size_mb,
        "iterations": args.iterations,
        "timestamp": time.time(),
    }

    # Join cgroup if specified
    if args.cgroup:
        if not join_cgroup(args.cgroup):
            result["error"] = "Failed to join cgroup"
            print(json.dumps(result, indent=2))
            return 1
        result["cgroup"] = args.cgroup
        print(f"[{args.name}] Joined cgroup: {args.cgroup}", file=sys.stderr)

    # Warmup
    if args.warmup > 0:
        warmup(args.warmup, args.size_mb)

    # Measure allocation latency (bytearray)
    print(f"[{args.name}] Measuring allocation latency ({args.iterations} x {args.size_mb}MB)...",
          file=sys.stderr)
    alloc_latencies = measure_alloc_latency(args.size_mb, args.iterations)
    result["alloc_latency"] = calc_stats(alloc_latencies)
    result["alloc_latencies_ns"] = alloc_latencies

    # Measure mmap latency
    if not args.skip_mmap:
        print(f"[{args.name}] Measuring mmap latency...", file=sys.stderr)
        mmap_latencies = measure_mmap_latency(args.size_mb, args.iterations)
        result["mmap_latency"] = calc_stats(mmap_latencies)
        result["mmap_latencies_ns"] = mmap_latencies

    # Measure throughput
    print(f"[{args.name}] Measuring throughput ({args.throughput_duration}s)...",
          file=sys.stderr)
    result["throughput"] = measure_throughput(args.size_mb, args.throughput_duration)

    result["end_timestamp"] = time.time()
    result["total_duration_sec"] = result["end_timestamp"] - result["timestamp"]

    # Print summary
    print(f"\n[{args.name}] Results:", file=sys.stderr)
    print(f"  Allocation Latency:", file=sys.stderr)
    stats = result["alloc_latency"]
    print(f"    Avg:  {stats['avg_ms']:.3f} ms", file=sys.stderr)
    print(f"    P50:  {stats['p50_ms']:.3f} ms", file=sys.stderr)
    print(f"    P95:  {stats['p95_ms']:.3f} ms", file=sys.stderr)
    print(f"    P99:  {stats['p99_ms']:.3f} ms", file=sys.stderr)
    print(f"    Max:  {stats['max_ms']:.3f} ms", file=sys.stderr)
    print(f"  Throughput: {result['throughput']['throughput_per_sec']:.1f} allocs/sec",
          file=sys.stderr)

    # Output
    if args.output:
        # Don't include raw latencies in file (too large)
        output_result = {k: v for k, v in result.items()
                        if not k.endswith("_ns")}
        with open(args.output, "w") as f:
            json.dump(output_result, f, indent=2)
        print(f"[{args.name}] Results saved to {args.output}", file=sys.stderr)
    else:
        # Print full result to stdout
        print(json.dumps({k: v for k, v in result.items()
                         if not k.endswith("_ns")}, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
