#!/bin/bash
#
# run_full_overhead_experiment.sh - Complete BPF overhead measurement
#
# Runs two complementary experiments:
#   1. Microbenchmark: Precise latency measurement with synthetic allocations
#   2. Trace replay: Realistic overhead with agent workload patterns
#
# Output: Comprehensive overhead report showing BPF cost when NOT throttling
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# === Configuration ===
CGROUP_ROOT="/sys/fs/cgroup/overhead_test"
RESULT_DIR="$SCRIPT_DIR/overhead_results"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Memory configuration (ample to avoid pressure)
MEMORY_LIMIT_MB=2000

# Microbenchmark settings
MICRO_SIZE_MB=10
MICRO_ITERATIONS=300
MICRO_WARMUP=50
MICRO_THROUGHPUT_DURATION=5

# Trace replay settings
TRACE="$SCRIPT_DIR/../../experiments/all_images_haiku/dask__dask-11628/attempt_1/resources.json"
SPEED_FACTOR=50
BASE_MEMORY_MB=100

# BPF settings
BPF_LOADER="$SCRIPT_DIR/bpf_loader/memcg_priority"
BPF_DELAY_MS=50
BPF_THRESHOLD=10000

RUNS=3

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_header() {
    echo ""
    echo -e "${BLUE}================================================================${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}================================================================${NC}"
    echo ""
}

setup_cgroups() {
    log_info "Setting up cgroups with ${MEMORY_LIMIT_MB}MB limit"
    cleanup_cgroups 2>/dev/null || true

    sudo mkdir -p "$CGROUP_ROOT"
    echo "+memory" | sudo tee "$CGROUP_ROOT/cgroup.subtree_control" > /dev/null
    sudo mkdir -p "$CGROUP_ROOT/test_session"

    local limit_bytes=$((MEMORY_LIMIT_MB * 1024 * 1024))
    echo "$limit_bytes" | sudo tee "$CGROUP_ROOT/memory.max" > /dev/null
    echo "0" | sudo tee "$CGROUP_ROOT/memory.swap.max" > /dev/null
    echo "max" | sudo tee "$CGROUP_ROOT/test_session/memory.high" > /dev/null
    sudo chmod 666 "$CGROUP_ROOT/test_session/cgroup.procs"
}

cleanup_cgroups() {
    for cg in "$CGROUP_ROOT"/*/cgroup.procs; do
        if [[ -f "$cg" ]]; then
            while read -r pid; do
                sudo kill -9 "$pid" 2>/dev/null || true
            done < "$cg"
        fi
    done
    sleep 1
    sudo rmdir "$CGROUP_ROOT"/test_session 2>/dev/null || true
    sudo rmdir "$CGROUP_ROOT" 2>/dev/null || true
}

start_bpf() {
    log_info "Starting BPF loader..."
    sudo "$BPF_LOADER" \
        --high "$CGROUP_ROOT/test_session" \
        --delay-ms "$BPF_DELAY_MS" \
        --threshold "$BPF_THRESHOLD" \
        --below-low &
    BPF_PID=$!
    sleep 2
    log_info "BPF started (PID: $BPF_PID)"
}

stop_bpf() {
    if [[ -n "$BPF_PID" ]]; then
        sudo kill "$BPF_PID" 2>/dev/null || true
        wait "$BPF_PID" 2>/dev/null || true
        BPF_PID=""
    fi
}

run_microbench() {
    local scenario=$1
    local run=$2
    local output="$RESULT_DIR/micro_${scenario}_run${run}.json"

    log_info "Running microbenchmark: $scenario (run $run)"

    python3 "$SCRIPT_DIR/overhead_microbench.py" \
        --cgroup "$CGROUP_ROOT/test_session" \
        --name "$scenario" \
        --size-mb "$MICRO_SIZE_MB" \
        --iterations "$MICRO_ITERATIONS" \
        --warmup "$MICRO_WARMUP" \
        --throughput-duration "$MICRO_THROUGHPUT_DURATION" \
        --output "$output" \
        --skip-mmap
}

run_trace_replay() {
    local scenario=$1
    local run=$2
    local output="$RESULT_DIR/trace_${scenario}_run${run}.json"

    log_info "Running trace replay: $scenario (run $run)"

    python3 "$SCRIPT_DIR/trace_replay.py" \
        "$TRACE" \
        --speed "$SPEED_FACTOR" \
        --base-memory-mb "$BASE_MEMORY_MB" \
        --cgroup "$CGROUP_ROOT/test_session" \
        --name "$scenario" \
        --output "$output"
}

analyze_all() {
    log_header "Overhead Analysis Report"

    python3 - "$RESULT_DIR" <<'PYTHON'
import json
import sys
from pathlib import Path
from collections import defaultdict

result_dir = Path(sys.argv[1])

# Collect results
micro_results = defaultdict(list)
trace_results = defaultdict(list)

for f in result_dir.glob("micro_*.json"):
    parts = f.stem.split("_")
    scenario = parts[1]  # no_bpf or bpf
    with open(f) as fp:
        micro_results[scenario].append(json.load(fp))

for f in result_dir.glob("trace_*.json"):
    parts = f.stem.split("_")
    scenario = parts[1]
    with open(f) as fp:
        trace_results[scenario].append(json.load(fp))

def avg(lst):
    return sum(lst) / len(lst) if lst else 0

def print_comparison(title, no_bpf_vals, bpf_vals, unit="", lower_is_better=True):
    if not no_bpf_vals or not bpf_vals:
        return
    no_bpf_avg = avg(no_bpf_vals)
    bpf_avg = avg(bpf_vals)
    diff = bpf_avg - no_bpf_avg
    pct = (diff / no_bpf_avg * 100) if no_bpf_avg != 0 else 0

    # Determine if this is good or bad
    is_overhead = (diff > 0) if lower_is_better else (diff < 0)
    color = "\033[91m" if is_overhead else "\033[92m"  # red or green
    reset = "\033[0m"

    print(f"  {title:25} {no_bpf_avg:10.3f} {unit:4} -> {bpf_avg:10.3f} {unit:4}  "
          f"({color}{diff:+.3f} {unit}, {pct:+.1f}%{reset})")

print("=" * 76)
print("  MICROBENCHMARK RESULTS (Synthetic Allocations)")
print("=" * 76)
print()

if micro_results:
    print(f"  Configuration: {micro_results['no'][0].get('iterations', '?')} iterations, "
          f"{micro_results['no'][0].get('size_mb', '?')} MB allocations")
    print()

    # Extract latency stats
    no_bpf_p50 = [r['alloc_latency']['p50_ms'] for r in micro_results.get('no', [])]
    bpf_p50 = [r['alloc_latency']['p50_ms'] for r in micro_results.get('bpf', [])]

    no_bpf_p95 = [r['alloc_latency']['p95_ms'] for r in micro_results.get('no', [])]
    bpf_p95 = [r['alloc_latency']['p95_ms'] for r in micro_results.get('bpf', [])]

    no_bpf_p99 = [r['alloc_latency']['p99_ms'] for r in micro_results.get('no', [])]
    bpf_p99 = [r['alloc_latency']['p99_ms'] for r in micro_results.get('bpf', [])]

    no_bpf_max = [r['alloc_latency']['max_ms'] for r in micro_results.get('no', [])]
    bpf_max = [r['alloc_latency']['max_ms'] for r in micro_results.get('bpf', [])]

    no_bpf_avg = [r['alloc_latency']['avg_ms'] for r in micro_results.get('no', [])]
    bpf_avg_lat = [r['alloc_latency']['avg_ms'] for r in micro_results.get('bpf', [])]

    no_bpf_tp = [r['throughput']['throughput_per_sec'] for r in micro_results.get('no', [])]
    bpf_tp = [r['throughput']['throughput_per_sec'] for r in micro_results.get('bpf', [])]

    print("  Allocation Latency (lower is better):")
    print("  " + "-" * 72)
    print(f"  {'Metric':25} {'No BPF':>14}     {'With BPF':>14}     {'Overhead':>20}")
    print("  " + "-" * 72)
    print_comparison("P50 Latency", no_bpf_p50, bpf_p50, "ms")
    print_comparison("P95 Latency", no_bpf_p95, bpf_p95, "ms")
    print_comparison("P99 Latency", no_bpf_p99, bpf_p99, "ms")
    print_comparison("Max Latency", no_bpf_max, bpf_max, "ms")
    print_comparison("Avg Latency", no_bpf_avg, bpf_avg_lat, "ms")
    print()
    print("  Throughput (higher is better):")
    print("  " + "-" * 72)
    print_comparison("Allocs/sec", no_bpf_tp, bpf_tp, "/s", lower_is_better=False)

print()
print("=" * 76)
print("  TRACE REPLAY RESULTS (Realistic Agent Workload)")
print("=" * 76)
print()

if trace_results:
    # Extract stats
    no_bpf_time = [r['completion_time_sec'] for r in trace_results.get('no', [])]
    bpf_time = [r['completion_time_sec'] for r in trace_results.get('bpf', [])]

    no_bpf_lat = [r.get('latency_stats', {}) for r in trace_results.get('no', [])]
    bpf_lat = [r.get('latency_stats', {}) for r in trace_results.get('bpf', [])]

    no_bpf_p50 = [l.get('p50_ms', 0) for l in no_bpf_lat]
    bpf_p50 = [l.get('p50_ms', 0) for l in bpf_lat]

    no_bpf_p95 = [l.get('p95_ms', 0) for l in no_bpf_lat]
    bpf_p95 = [l.get('p95_ms', 0) for l in bpf_lat]

    no_bpf_max = [l.get('max_ms', 0) for l in no_bpf_lat]
    bpf_max = [l.get('max_ms', 0) for l in bpf_lat]

    print("  Completion Time:")
    print("  " + "-" * 72)
    print_comparison("Total Time", no_bpf_time, bpf_time, "s")
    print()
    print("  Allocation Latency:")
    print("  " + "-" * 72)
    print_comparison("P50 Latency", no_bpf_p50, bpf_p50, "ms")
    print_comparison("P95 Latency", no_bpf_p95, bpf_p95, "ms")
    print_comparison("Max Latency", no_bpf_max, bpf_max, "ms")

print()
print("=" * 76)
print("  SUMMARY")
print("=" * 76)
print()
print("  BPF Overhead (when NOT throttling):")
print()

# Calculate overall overhead percentages
if micro_results:
    no_avg = avg([r['alloc_latency']['avg_ms'] for r in micro_results.get('no', [])])
    bpf_avg = avg([r['alloc_latency']['avg_ms'] for r in micro_results.get('bpf', [])])
    if no_avg > 0:
        overhead = (bpf_avg - no_avg) / no_avg * 100
        print(f"  - Microbenchmark avg latency overhead: {overhead:+.2f}%")

    no_tp = avg([r['throughput']['throughput_per_sec'] for r in micro_results.get('no', [])])
    bpf_tp = avg([r['throughput']['throughput_per_sec'] for r in micro_results.get('bpf', [])])
    if no_tp > 0:
        tp_overhead = (bpf_tp - no_tp) / no_tp * 100
        print(f"  - Throughput impact: {tp_overhead:+.2f}%")

if trace_results:
    no_time = avg([r['completion_time_sec'] for r in trace_results.get('no', [])])
    bpf_time = avg([r['completion_time_sec'] for r in trace_results.get('bpf', [])])
    if no_time > 0:
        time_overhead = (bpf_time - no_time) / no_time * 100
        print(f"  - Trace replay time overhead: {time_overhead:+.2f}%")

print()

# Save summary
summary = {
    "microbenchmark": {k: [r for r in v] for k, v in micro_results.items()},
    "trace_replay": {k: [r for r in v] for k, v in trace_results.items()},
}
with open(result_dir / "overhead_summary.json", "w") as f:
    # Remove raw latency arrays to keep file size manageable
    clean_summary = json.loads(json.dumps(summary))
    for bench_type in clean_summary.values():
        for scenario in bench_type.values():
            for run in scenario:
                run.pop("allocation_latencies_ms", None)
    json.dump(clean_summary, f, indent=2)

print(f"  Full results saved to: {result_dir}/overhead_summary.json")
print()

PYTHON
}

main() {
    log_header "BPF memcg struct_ops Overhead Measurement"

    # Check prerequisites
    if [[ ! -f "$BPF_LOADER" ]]; then
        echo "Error: BPF loader not found at $BPF_LOADER"
        echo "Build it with: cd bpf_loader && make"
        exit 1
    fi

    if [[ ! -f "$TRACE" ]]; then
        echo "Error: Trace file not found: $TRACE"
        exit 1
    fi

    # Create result directory
    RESULT_DIR="$RESULT_DIR/$TIMESTAMP"
    mkdir -p "$RESULT_DIR"

    log_info "Configuration:"
    log_info "  Memory limit: ${MEMORY_LIMIT_MB}MB (no pressure)"
    log_info "  Microbench: ${MICRO_ITERATIONS} x ${MICRO_SIZE_MB}MB"
    log_info "  Trace: $TRACE"
    log_info "  Runs: $RUNS"
    log_info "  Results: $RESULT_DIR"

    # Cleanup
    cleanup_cgroups 2>/dev/null || true

    for run in $(seq 1 $RUNS); do
        log_header "Run $run of $RUNS"

        # === Microbenchmark without BPF ===
        setup_cgroups
        run_microbench "no" "$run"
        cleanup_cgroups
        sleep 2

        # === Microbenchmark with BPF ===
        setup_cgroups
        start_bpf
        run_microbench "bpf" "$run"
        stop_bpf
        cleanup_cgroups
        sleep 2

        # === Trace replay without BPF ===
        setup_cgroups
        run_trace_replay "no" "$run"
        cleanup_cgroups
        sleep 2

        # === Trace replay with BPF ===
        setup_cgroups
        start_bpf
        run_trace_replay "bpf" "$run"
        stop_bpf
        cleanup_cgroups
        sleep 2
    done

    # Analyze
    analyze_all

    log_header "Experiment Complete"
    log_info "Results: $RESULT_DIR"
}

trap 'stop_bpf; cleanup_cgroups' EXIT
main "$@"
