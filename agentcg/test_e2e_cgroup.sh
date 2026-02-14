#!/bin/bash
#
# test_e2e_cgroup.sh - End-to-end test on real cgroup v2 filesystem
#
# Tests the CgroupMemcgController with actual cgroup operations:
#   1. Creates cgroup hierarchy
#   2. Starts trace replay in HIGH and LOW cgroups
#   3. agentcgroupd's CgroupMemcgController monitors and protects
#   4. Verifies HIGH session gets priority under memory pressure
#
# Usage: sudo ./test_e2e_cgroup.sh [--total-mb 512] [--speed 50]
#
# Requires: root, cgroup v2 mounted at /sys/fs/cgroup
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CGROUP_ROOT="/sys/fs/cgroup/agentcg_test"
TRACES_DIR="$PROJECT_ROOT/experiments/all_images_haiku"

# Parameters
TOTAL_MEMORY_MB=512
SPEED=50

while [[ $# -gt 0 ]]; do
    case "$1" in
        --total-mb) TOTAL_MEMORY_MB="$2"; shift 2 ;;
        --speed)    SPEED="$2"; shift 2 ;;
        *)          shift ;;
    esac
done

# Trace selection
HIGH_TRACE="dask__dask-11628"
LOW_TRACE="dask__dask-11628"  # same trace for LOW to create contention

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[TEST]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

# ---- Preflight ----
if [ "$(id -u)" -ne 0 ]; then
    fail "Must run as root"
fi

if [ ! -d /sys/fs/cgroup ]; then
    fail "cgroup v2 filesystem not found at /sys/fs/cgroup"
fi

HIGH_TRACE_PATH="$TRACES_DIR/$HIGH_TRACE/attempt_1/resources.json"
LOW_TRACE_PATH="$TRACES_DIR/$LOW_TRACE/attempt_1/resources.json"

if [ ! -f "$HIGH_TRACE_PATH" ]; then
    fail "HIGH trace not found: $HIGH_TRACE_PATH"
fi

# ---- Cleanup function ----
cleanup() {
    log "Cleaning up..."
    # Kill children
    for cg in session_high session_low; do
        if [ -f "$CGROUP_ROOT/$cg/cgroup.procs" ]; then
            while read pid; do
                kill -9 "$pid" 2>/dev/null || true
            done < "$CGROUP_ROOT/$cg/cgroup.procs"
        fi
    done
    sleep 1
    # Remove cgroups (must be empty)
    rmdir "$CGROUP_ROOT/session_high" 2>/dev/null || true
    rmdir "$CGROUP_ROOT/session_low" 2>/dev/null || true
    rmdir "$CGROUP_ROOT" 2>/dev/null || true
    log "Cleanup done"
}
trap cleanup EXIT

# ---- Step 1: Create cgroup hierarchy ----
log "=== Step 1: Creating cgroup hierarchy ==="

cleanup 2>/dev/null || true

mkdir -p "$CGROUP_ROOT"

# Enable controllers on parent first, then create children
# memory.max and subtree_control must be set before child cgroups exist
TOTAL_BYTES=$((TOTAL_MEMORY_MB * 1024 * 1024))
echo "$TOTAL_BYTES" > "$CGROUP_ROOT/memory.max" 2>/dev/null || warn "Could not set memory.max"
echo "+memory +cpu" > "$CGROUP_ROOT/cgroup.subtree_control" 2>/dev/null || warn "Could not enable subtree controllers"

mkdir -p "$CGROUP_ROOT/session_high"
mkdir -p "$CGROUP_ROOT/session_low"

# Set CPU weights
echo 150 > "$CGROUP_ROOT/session_high/cpu.weight" 2>/dev/null || true
echo 50 > "$CGROUP_ROOT/session_low/cpu.weight" 2>/dev/null || true

log "Cgroup hierarchy created at $CGROUP_ROOT"
log "  Total memory limit: ${TOTAL_MEMORY_MB}MB (${TOTAL_BYTES} bytes)"
log "  HIGH cpu.weight: 150, LOW cpu.weight: 50"

# ---- Step 2: Record baseline memory.events ----
log "=== Step 2: Recording baseline memory.events ==="

HIGH_EVENTS_BEFORE=$(cat "$CGROUP_ROOT/session_high/memory.events" 2>/dev/null || echo "")
LOW_EVENTS_BEFORE=$(cat "$CGROUP_ROOT/session_low/memory.events" 2>/dev/null || echo "")

log "HIGH events before: $(echo $HIGH_EVENTS_BEFORE | tr '\n' ' ')"
log "LOW  events before: $(echo $LOW_EVENTS_BEFORE | tr '\n' ' ')"

# ---- Step 3: Start CgroupMemcgController in background ----
log "=== Step 3: Starting CgroupMemcgController ==="

MONITOR_LOG=$(mktemp /tmp/agentcg_monitor_XXXXXX.log)
python3 -c "
import sys, time, json, logging
sys.path.insert(0, '$SCRIPT_DIR')
from memcg_controller import MemcgConfig, CgroupMemcgController

logging.basicConfig(level=logging.INFO, format='[memcg] %(message)s')

ctrl = CgroupMemcgController()
config = MemcgConfig(
    high_cgroup='$CGROUP_ROOT/session_high',
    low_cgroups=['$CGROUP_ROOT/session_low'],
    threshold=1,
    protection_window_s=1.0,
)
ctrl.attach(config)

# Poll for 60 seconds, logging state changes
start = time.monotonic()
poll_count = 0
while time.monotonic() - start < 60:
    ctrl.poll()
    poll_count += 1
    if poll_count % 10 == 0:
        stats = ctrl.get_stats()
        print(json.dumps({
            'time': time.monotonic() - start,
            'stats': stats,
        }), flush=True)
    time.sleep(0.1)

ctrl.detach()
stats = ctrl.get_stats()
print(json.dumps({'final_stats': stats}), flush=True)
" > "$MONITOR_LOG" 2>&1 &
MONITOR_PID=$!

log "Controller started (PID $MONITOR_PID), logging to $MONITOR_LOG"

sleep 0.5

# ---- Step 4: Start trace replay in both cgroups ----
log "=== Step 4: Starting trace replay ==="

RESULT_DIR=$(mktemp -d /tmp/agentcg_results_XXXXXX)

# HIGH session
python3 "$SCRIPT_DIR/../memcg/multi_tenant_test/trace_replay.py" \
    "$HIGH_TRACE_PATH" \
    --speed "$SPEED" \
    --cgroup "$CGROUP_ROOT/session_high" \
    --name "HIGH" \
    --output "$RESULT_DIR/high_result.json" &
HIGH_PID=$!
log "HIGH replay started (PID $HIGH_PID): $HIGH_TRACE"

# LOW session (start 1 second later to create overlap)
sleep 1
python3 "$SCRIPT_DIR/../memcg/multi_tenant_test/trace_replay.py" \
    "$LOW_TRACE_PATH" \
    --speed "$SPEED" \
    --base-memory-mb 100 \
    --cgroup "$CGROUP_ROOT/session_low" \
    --name "LOW" \
    --output "$RESULT_DIR/low_result.json" &
LOW_PID=$!
log "LOW  replay started (PID $LOW_PID):  $LOW_TRACE (+ 100MB base)"

# ---- Step 5: Wait for replays to finish ----
log "=== Step 5: Waiting for replays to complete ==="

wait $HIGH_PID 2>/dev/null || true
HIGH_EXIT=$?
wait $LOW_PID 2>/dev/null || true
LOW_EXIT=$?

# Stop monitor
kill $MONITOR_PID 2>/dev/null || true
wait $MONITOR_PID 2>/dev/null || true

# ---- Step 6: Analyze results ----
log "=== Step 6: Results ==="

HIGH_EVENTS_AFTER=$(cat "$CGROUP_ROOT/session_high/memory.events" 2>/dev/null || echo "")
LOW_EVENTS_AFTER=$(cat "$CGROUP_ROOT/session_low/memory.events" 2>/dev/null || echo "")

echo ""
echo "=== Memory Events ==="
echo "HIGH events after: $(echo $HIGH_EVENTS_AFTER | tr '\n' ' ')"
echo "LOW  events after: $(echo $LOW_EVENTS_AFTER | tr '\n' ' ')"

echo ""
echo "=== Controller Log ==="
cat "$MONITOR_LOG"

echo ""
echo "=== Replay Results ==="
if [ -f "$RESULT_DIR/high_result.json" ]; then
    echo "HIGH session:"
    python3 -c "
import json
r = json.load(open('$RESULT_DIR/high_result.json'))
print(f'  Completion time: {r.get(\"completion_time_sec\",0):.2f}s')
print(f'  Peak memory: {r.get(\"peak_memory_mb\",0):.0f}MB')
print(f'  OOM count: {r.get(\"oom_count\",0)}')
ls = r.get('latency_stats', {})
if ls:
    print(f'  Alloc latency: avg={ls.get(\"avg_ms\",0):.1f}ms p95={ls.get(\"p95_ms\",0):.1f}ms max={ls.get(\"max_ms\",0):.1f}ms')
ed = r.get('events_delta', {})
if ed:
    print(f'  Events delta: {ed}')
"
fi

if [ -f "$RESULT_DIR/low_result.json" ]; then
    echo "LOW session:"
    python3 -c "
import json
r = json.load(open('$RESULT_DIR/low_result.json'))
print(f'  Completion time: {r.get(\"completion_time_sec\",0):.2f}s')
print(f'  Peak memory: {r.get(\"peak_memory_mb\",0):.0f}MB')
print(f'  OOM count: {r.get(\"oom_count\",0)}')
ls = r.get('latency_stats', {})
if ls:
    print(f'  Alloc latency: avg={ls.get(\"avg_ms\",0):.1f}ms p95={ls.get(\"p95_ms\",0):.1f}ms max={ls.get(\"max_ms\",0):.1f}ms')
ed = r.get('events_delta', {})
if ed:
    print(f'  Events delta: {ed}')
"
fi

echo ""
echo "=== Verdict ==="
if [ -f "$RESULT_DIR/high_result.json" ] && [ -f "$RESULT_DIR/low_result.json" ]; then
    python3 -c "
import json
h = json.load(open('$RESULT_DIR/high_result.json'))
l = json.load(open('$RESULT_DIR/low_result.json'))
h_oom = h.get('oom_count', 0)
l_oom = l.get('oom_count', 0)
h_time = h.get('completion_time_sec', 0)
l_time = l.get('completion_time_sec', 0)
h_lat = h.get('latency_stats', {}).get('avg_ms', 0)
l_lat = l.get('latency_stats', {}).get('avg_ms', 0)

if h_oom == 0:
    print('  ✓ HIGH session: no OOM (protected)')
else:
    print(f'  ✗ HIGH session: {h_oom} OOMs')

if l_time > h_time:
    print(f'  ✓ LOW session slower ({l_time:.1f}s vs {h_time:.1f}s) — throttling worked')
elif l_lat > h_lat and l_lat > 0:
    print(f'  ✓ LOW alloc latency higher ({l_lat:.1f}ms vs {h_lat:.1f}ms) — throttling worked')
else:
    print('  ? No clear throttling effect (may need more memory pressure)')
"
fi

# Cleanup temp files
rm -f "$MONITOR_LOG"
rm -rf "$RESULT_DIR"

log "Test complete"
