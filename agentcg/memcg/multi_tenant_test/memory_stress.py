#!/usr/bin/env python3
"""
memory_stress.py - 内存压力工具

在指定 cgroup 中分配内存，模拟 agent 工作负载。
"""

import argparse
import json
import os
import sys
import time


def join_cgroup(cgroup_path):
    """将当前进程加入指定 cgroup"""
    procs_file = os.path.join(cgroup_path, "cgroup.procs")
    try:
        with open(procs_file, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception as e:
        print(f"Warning: Failed to join cgroup {cgroup_path}: {e}", file=sys.stderr)
        return False


def allocate_memory(target_mb, chunk_size_mb=64):
    """
    分配指定大小的内存并触发物理分配。

    Args:
        target_mb: 目标内存大小 (MB)
        chunk_size_mb: 每次分配的块大小 (MB)

    Returns:
        list of bytearrays
    """
    buffers = []
    allocated = 0
    chunk_size = chunk_size_mb * 1024 * 1024

    while allocated < target_mb * 1024 * 1024:
        remaining = target_mb * 1024 * 1024 - allocated
        alloc_size = min(chunk_size, remaining)

        try:
            buf = bytearray(alloc_size)
            # Touch every page to trigger physical allocation
            for i in range(0, len(buf), 4096):
                buf[i] = 1
            buffers.append(buf)
            allocated += alloc_size
            print(f"  Allocated {allocated / (1024*1024):.1f} MB", file=sys.stderr)
        except MemoryError as e:
            print(f"  MemoryError at {allocated / (1024*1024):.1f} MB: {e}", file=sys.stderr)
            break

    return buffers


def read_memory_events(cgroup_path):
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


def main():
    parser = argparse.ArgumentParser(description="Memory stress tool for cgroup testing")
    parser.add_argument("--cgroup", required=True, help="Cgroup path to join")
    parser.add_argument("--memory-mb", type=int, default=256, help="Target memory in MB")
    parser.add_argument("--hold-seconds", type=int, default=10, help="How long to hold memory")
    parser.add_argument("--name", default="worker", help="Worker name for logging")
    parser.add_argument("--output", help="Output JSON file for results")
    args = parser.parse_args()

    result = {
        "name": args.name,
        "cgroup": args.cgroup,
        "target_mb": args.memory_mb,
        "hold_seconds": args.hold_seconds,
        "pid": os.getpid(),
    }

    print(f"[{args.name}] Starting memory stress test", file=sys.stderr)
    print(f"[{args.name}] Target: {args.memory_mb} MB, Hold: {args.hold_seconds}s", file=sys.stderr)

    # Join cgroup
    if not join_cgroup(args.cgroup):
        result["error"] = "Failed to join cgroup"
        if args.output:
            with open(args.output, "w") as f:
                json.dump(result, f, indent=2)
        return 1

    print(f"[{args.name}] Joined cgroup: {args.cgroup}", file=sys.stderr)

    # Record start events
    events_before = read_memory_events(args.cgroup)
    result["events_before"] = events_before

    # Start timing
    start_time = time.time()
    result["start_time"] = start_time

    # Allocate memory
    print(f"[{args.name}] Allocating {args.memory_mb} MB...", file=sys.stderr)
    alloc_start = time.time()
    buffers = allocate_memory(args.memory_mb)
    alloc_end = time.time()

    actual_mb = sum(len(b) for b in buffers) / (1024 * 1024)
    result["allocated_mb"] = actual_mb
    result["allocation_time"] = alloc_end - alloc_start

    print(f"[{args.name}] Allocated {actual_mb:.1f} MB in {alloc_end - alloc_start:.2f}s", file=sys.stderr)

    # Hold memory
    print(f"[{args.name}] Holding memory for {args.hold_seconds}s...", file=sys.stderr)
    time.sleep(args.hold_seconds)

    # Release memory
    print(f"[{args.name}] Releasing memory...", file=sys.stderr)
    buffers.clear()

    # Record end time and events
    end_time = time.time()
    result["end_time"] = end_time
    result["total_time"] = end_time - start_time

    events_after = read_memory_events(args.cgroup)
    result["events_after"] = events_after

    # Calculate event deltas
    result["events_delta"] = {
        k: events_after.get(k, 0) - events_before.get(k, 0)
        for k in set(events_before.keys()) | set(events_after.keys())
    }

    print(f"[{args.name}] Completed in {result['total_time']:.2f}s", file=sys.stderr)
    print(f"[{args.name}] Events delta: {result['events_delta']}", file=sys.stderr)

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
