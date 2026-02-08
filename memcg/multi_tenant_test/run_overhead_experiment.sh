#!/bin/bash
#
# run_overhead_experiment.sh - Measure BPF memcg struct_ops overhead
#
# This experiment measures the overhead introduced by BPF when there is
# NO memory pressure (i.e., BPF is attached but not actively throttling).
#
# Three scenarios:
#   1. no_bpf:      No BPF attached (baseline)
#   2. bpf_attached: BPF attached, but memory limit is high (no throttling)
#   3. bpf_active:   BPF attached with memory pressure (for comparison)
#
# Metrics measured:
#   - Allocation latency (P50, P95, P99, Max)
#   - Total completion time
#   - CPU usage (via /proc/stat)
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# === Configuration ===
CGROUP_ROOT="/sys/fs/cgroup/overhead_test"
RESULT_DIR="$SCRIPT_DIR/overhead_results"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Memory configuration
AMPLE_MEMORY_MB=2000     # 2GB - plenty of room, no pressure
PRESSURE_MEMORY_MB=1100  # 1.1GB - creates pressure

# Traces to use (single process for cleaner overhead measurement)
HIGH_TRACE="$SCRIPT_DIR/../../../experiments/all_images_haiku/dask__dask-11628/attempt_1/resources.json"

# Replay settings
SPEED_FACTOR=50
BASE_MEMORY_MB=100
RUNS_PER_SCENARIO=3

# BPF settings
BPF_LOADER="$SCRIPT_DIR/bpf_loader/memcg_priority"
BPF_DELAY_MS=50
BPF_THRESHOLD=10000

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_header() {
    echo ""
    echo -e "${BLUE}================================================================${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}================================================================${NC}"
    echo ""
}

# === Utility Functions ===

get_cpu_time() {
    # Get total CPU time from /proc/stat (in jiffies)
    awk '/^cpu / {print $2+$3+$4+$5+$6+$7+$8}' /proc/stat
}

setup_cgroups() {
    local memory_limit_mb=$1

    log_info "Setting up cgroups with ${memory_limit_mb}MB limit"

    # Clean up old cgroups
    cleanup_cgroups 2>/dev/null || true

    # Create root cgroup
    sudo mkdir -p "$CGROUP_ROOT"
    echo "+memory" | sudo tee "$CGROUP_ROOT/cgroup.subtree_control" > /dev/null

    # Create session cgroup
    sudo mkdir -p "$CGROUP_ROOT/test_session"

    # Set memory limits
    local limit_bytes=$((memory_limit_mb * 1024 * 1024))
    echo "$limit_bytes" | sudo tee "$CGROUP_ROOT/memory.max" > /dev/null
    echo "0" | sudo tee "$CGROUP_ROOT/memory.swap.max" > /dev/null

    # Set memory.high for BPF to work with
    echo "max" | sudo tee "$CGROUP_ROOT/test_session/memory.high" > /dev/null

    # Make cgroup writable
    sudo chmod 666 "$CGROUP_ROOT/test_session/cgroup.procs"

    log_info "Cgroup setup complete"
}

cleanup_cgroups() {
    # Kill any processes in cgroups
    for cg in "$CGROUP_ROOT"/*/cgroup.procs; do
        if [[ -f "$cg" ]]; then
            while read -r pid; do
                sudo kill -9 "$pid" 2>/dev/null || true
            done < "$cg"
        fi
    done
    sleep 1

    # Remove cgroups
    sudo rmdir "$CGROUP_ROOT"/test_session 2>/dev/null || true
    sudo rmdir "$CGROUP_ROOT" 2>/dev/null || true
}

start_bpf_loader() {
    log_info "Starting BPF loader..."

    sudo "$BPF_LOADER" \
        --high "$CGROUP_ROOT/test_session" \
        --delay-ms "$BPF_DELAY_MS" \
        --threshold "$BPF_THRESHOLD" \
        --below-low &

    BPF_PID=$!
    sleep 2

    if ! ps -p $BPF_PID > /dev/null 2>&1; then
        log_error "BPF loader failed to start"
        return 1
    fi

    log_info "BPF loader started (PID: $BPF_PID)"
}

stop_bpf_loader() {
    if [[ -n "$BPF_PID" ]]; then
        log_info "Stopping BPF loader (PID: $BPF_PID)"
        sudo kill "$BPF_PID" 2>/dev/null || true
        wait "$BPF_PID" 2>/dev/null || true
        BPF_PID=""
    fi
}

run_single_workload() {
    local output_file=$1
    local name=$2

    local cpu_before=$(get_cpu_time)
    local start_time=$(date +%s.%N)

    python3 "$SCRIPT_DIR/trace_replay.py" \
        "$HIGH_TRACE" \
        --speed "$SPEED_FACTOR" \
        --base-memory-mb "$BASE_MEMORY_MB" \
        --cgroup "$CGROUP_ROOT/test_session" \
        --name "$name" \
        --output "$output_file"

    local end_time=$(date +%s.%N)
    local cpu_after=$(get_cpu_time)

    # Add CPU time delta to result
    local cpu_delta=$((cpu_after - cpu_before))
    local wall_time=$(echo "$end_time - $start_time" | bc)

    # Append CPU stats to result JSON
    python3 -c "
import json
with open('$output_file', 'r') as f:
    data = json.load(f)
data['cpu_jiffies'] = $cpu_delta
data['wall_time_sec'] = $wall_time
with open('$output_file', 'w') as f:
    json.dump(data, f, indent=2)
"
}

run_scenario() {
    local scenario=$1
    local memory_limit_mb=$2
    local use_bpf=$3
    local run_num=$4

    local result_dir="$RESULT_DIR/${scenario}_run${run_num}_${TIMESTAMP}"
    mkdir -p "$result_dir"

    log_info "Running scenario: $scenario (run $run_num)"
    log_info "  Memory limit: ${memory_limit_mb}MB"
    log_info "  BPF enabled: $use_bpf"

    # Setup cgroups
    setup_cgroups "$memory_limit_mb"

    # Start BPF if needed
    if [[ "$use_bpf" == "yes" ]]; then
        start_bpf_loader
    fi

    # Wait for system to settle
    sleep 2

    # Run workload
    run_single_workload "$result_dir/result.json" "$scenario"

    # Stop BPF
    if [[ "$use_bpf" == "yes" ]]; then
        stop_bpf_loader
    fi

    # Save config
    cat > "$result_dir/config.json" <<EOF
{
    "scenario": "$scenario",
    "run_num": $run_num,
    "memory_limit_mb": $memory_limit_mb,
    "bpf_enabled": "$use_bpf",
    "speed_factor": $SPEED_FACTOR,
    "base_memory_mb": $BASE_MEMORY_MB,
    "bpf_delay_ms": $BPF_DELAY_MS,
    "timestamp": "$TIMESTAMP"
}
EOF

    # Cleanup
    cleanup_cgroups

    echo "$result_dir"
}

analyze_results() {
    log_header "Analyzing Overhead Results"

    python3 - "$RESULT_DIR" <<'PYTHON_SCRIPT'
import json
import os
import sys
from pathlib import Path
from collections import defaultdict

result_dir = Path(sys.argv[1])
results = defaultdict(list)

# Collect all results
for run_dir in result_dir.iterdir():
    if not run_dir.is_dir():
        continue

    result_file = run_dir / "result.json"
    config_file = run_dir / "config.json"

    if not result_file.exists() or not config_file.exists():
        continue

    with open(result_file) as f:
        result = json.load(f)
    with open(config_file) as f:
        config = json.load(f)

    scenario = config["scenario"]
    results[scenario].append({
        "config": config,
        "result": result
    })

# Calculate statistics
def calc_stats(values):
    if not values:
        return {}
    n = len(values)
    sorted_v = sorted(values)
    return {
        "n": n,
        "min": sorted_v[0],
        "max": sorted_v[-1],
        "avg": sum(values) / n,
        "p50": sorted_v[n // 2],
    }

print("\n" + "=" * 70)
print("  OVERHEAD MEASUREMENT RESULTS")
print("=" * 70)

# Compare scenarios
scenarios = ["no_bpf", "bpf_attached", "bpf_active"]
metrics = {}

for scenario in scenarios:
    if scenario not in results:
        continue

    runs = results[scenario]

    # Aggregate latency stats across runs
    all_latencies = []
    completion_times = []
    cpu_times = []

    for run in runs:
        r = run["result"]
        if "allocation_latencies_ms" in r:
            all_latencies.extend(r["allocation_latencies_ms"])
        if "completion_time_sec" in r:
            completion_times.append(r["completion_time_sec"])
        if "cpu_jiffies" in r:
            cpu_times.append(r["cpu_jiffies"])

    # Calculate latency percentiles
    if all_latencies:
        sorted_lat = sorted(all_latencies)
        n = len(sorted_lat)
        latency_stats = {
            "count": n,
            "p50_ms": sorted_lat[n // 2],
            "p95_ms": sorted_lat[int(n * 0.95)] if n >= 20 else sorted_lat[-1],
            "p99_ms": sorted_lat[int(n * 0.99)] if n >= 100 else sorted_lat[-1],
            "max_ms": sorted_lat[-1],
            "avg_ms": sum(sorted_lat) / n,
        }
    else:
        latency_stats = {}

    metrics[scenario] = {
        "runs": len(runs),
        "latency": latency_stats,
        "completion_time": calc_stats(completion_times),
        "cpu_jiffies": calc_stats(cpu_times),
    }

    print(f"\n{scenario.upper()}")
    print("-" * 40)
    print(f"  Runs: {len(runs)}")
    if latency_stats:
        print(f"  Allocation Latency:")
        print(f"    P50:  {latency_stats['p50_ms']:.3f} ms")
        print(f"    P95:  {latency_stats['p95_ms']:.3f} ms")
        print(f"    P99:  {latency_stats['p99_ms']:.3f} ms")
        print(f"    Max:  {latency_stats['max_ms']:.3f} ms")
        print(f"    Avg:  {latency_stats['avg_ms']:.3f} ms")
    if completion_times:
        print(f"  Completion Time: {calc_stats(completion_times)['avg']:.3f}s (avg)")
    if cpu_times:
        print(f"  CPU Jiffies: {calc_stats(cpu_times)['avg']:.0f} (avg)")

# Calculate overhead
print("\n" + "=" * 70)
print("  OVERHEAD ANALYSIS")
print("=" * 70)

if "no_bpf" in metrics and "bpf_attached" in metrics:
    no_bpf = metrics["no_bpf"]
    bpf = metrics["bpf_attached"]

    print("\nBPF Overhead (when NOT throttling):")
    print("-" * 40)

    if no_bpf["latency"] and bpf["latency"]:
        for percentile in ["p50_ms", "p95_ms", "p99_ms", "max_ms", "avg_ms"]:
            base = no_bpf["latency"][percentile]
            with_bpf = bpf["latency"][percentile]
            overhead_pct = ((with_bpf - base) / base * 100) if base > 0 else 0
            overhead_ms = with_bpf - base

            label = percentile.replace("_ms", "").upper()
            print(f"  {label:6} Latency: {base:.3f}ms -> {with_bpf:.3f}ms " +
                  f"({overhead_ms:+.3f}ms, {overhead_pct:+.1f}%)")

    if no_bpf["completion_time"] and bpf["completion_time"]:
        base = no_bpf["completion_time"]["avg"]
        with_bpf = bpf["completion_time"]["avg"]
        overhead_pct = ((with_bpf - base) / base * 100) if base > 0 else 0
        print(f"\n  Completion Time: {base:.3f}s -> {with_bpf:.3f}s ({overhead_pct:+.1f}%)")

    if no_bpf["cpu_jiffies"] and bpf["cpu_jiffies"]:
        base = no_bpf["cpu_jiffies"]["avg"]
        with_bpf = bpf["cpu_jiffies"]["avg"]
        overhead_pct = ((with_bpf - base) / base * 100) if base > 0 else 0
        print(f"  CPU Usage:       {base:.0f} -> {with_bpf:.0f} jiffies ({overhead_pct:+.1f}%)")

print("\n")

# Save summary
summary = {
    "metrics": metrics,
    "scenarios": list(metrics.keys()),
}
summary_file = result_dir / "overhead_summary.json"
with open(summary_file, "w") as f:
    json.dump(summary, f, indent=2)
print(f"Summary saved to: {summary_file}")

PYTHON_SCRIPT
}

# === Main ===

main() {
    log_header "BPF memcg struct_ops Overhead Experiment"

    # Check prerequisites
    if [[ ! -f "$BPF_LOADER" ]]; then
        log_error "BPF loader not found: $BPF_LOADER"
        log_info "Please build it first: cd bpf_loader && make"
        exit 1
    fi

    if [[ ! -f "$HIGH_TRACE" ]]; then
        log_error "Trace file not found: $HIGH_TRACE"
        exit 1
    fi

    # Create result directory
    mkdir -p "$RESULT_DIR"

    log_info "Configuration:"
    log_info "  Ample memory limit: ${AMPLE_MEMORY_MB}MB (no pressure)"
    log_info "  Pressure memory limit: ${PRESSURE_MEMORY_MB}MB"
    log_info "  Speed factor: ${SPEED_FACTOR}x"
    log_info "  Runs per scenario: $RUNS_PER_SCENARIO"
    log_info "  Trace: $HIGH_TRACE"

    # Cleanup before starting
    cleanup_cgroups 2>/dev/null || true

    # Run experiments
    for run in $(seq 1 $RUNS_PER_SCENARIO); do
        log_header "Run $run of $RUNS_PER_SCENARIO"

        # Scenario 1: No BPF (baseline)
        run_scenario "no_bpf" "$AMPLE_MEMORY_MB" "no" "$run"
        sleep 3

        # Scenario 2: BPF attached, no pressure
        run_scenario "bpf_attached" "$AMPLE_MEMORY_MB" "yes" "$run"
        sleep 3

        # Scenario 3: BPF with pressure (for comparison)
        run_scenario "bpf_active" "$PRESSURE_MEMORY_MB" "yes" "$run"
        sleep 3
    done

    # Analyze results
    analyze_results

    log_header "Experiment Complete"
    log_info "Results saved to: $RESULT_DIR"
}

# Trap for cleanup
trap 'stop_bpf_loader; cleanup_cgroups' EXIT

# Run main
main "$@"
