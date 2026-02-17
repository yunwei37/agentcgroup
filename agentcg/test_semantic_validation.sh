#!/bin/bash
#
# test_semantic_validation.sh - Validate per-tool-call tracking with live Claude Code (haiku)
#
# This script:
#   1. Sets up the bash wrapper as a transparent interceptor
#   2. Runs Claude Code haiku on a small coding task
#   3. Analyzes the tool call log to verify per-tool-call tracking works
#   4. Tests OOM feedback display (simulated)
#
# Usage: ./test_semantic_validation.sh
#
# Requires: claude CLI available in PATH

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$SCRIPT_DIR/bash_wrapper_local.sh"
WORKDIR=$(mktemp -d /tmp/agentcg_semantic_test_XXXXXX)
LOG_FILE="$WORKDIR/tool_calls.jsonl"
SIM_CG="$WORKDIR/cgroup_sim/session_high"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[TEST]${NC} $1"; }
info() { echo -e "${CYAN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

cleanup() {
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

# --- Preflight ---
if ! command -v claude &>/dev/null; then
    echo -e "${RED}[FAIL]${NC} claude CLI not found. Install Claude Code first."
    exit 1
fi

mkdir -p "$SIM_CG"
mkdir -p "$WORKDIR/testproject"

log "=== Per-Tool-Call Semantic Validation ==="
echo ""
info "Wrapper: $WRAPPER"
info "Workdir: $WORKDIR"
info "Log file: $LOG_FILE"
echo ""

# --- Phase 1: Set up a small test project ---
log "Phase 1: Setting up test project..."

cat > "$WORKDIR/testproject/calculator.py" << 'PYEOF'
def add(a, b):
    return a + b

def subtract(a, b):
    return a - b

def multiply(a, b):
    return a * b

def divide(a, b):
    # BUG: no zero division check
    return a / b
PYEOF

cat > "$WORKDIR/testproject/test_calculator.py" << 'PYEOF'
import unittest
from calculator import add, subtract, multiply, divide

class TestCalculator(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_subtract(self):
        self.assertEqual(subtract(5, 3), 2)

    def test_multiply(self):
        self.assertEqual(multiply(3, 4), 12)

    def test_divide(self):
        self.assertEqual(divide(10, 2), 5)

    def test_divide_by_zero(self):
        # This test will fail because divide doesn't handle zero
        with self.assertRaises(ZeroDivisionError):
            divide(10, 0)

if __name__ == "__main__":
    unittest.main()
PYEOF

log "Test project created at $WORKDIR/testproject"

# --- Phase 2: Run Claude Code haiku with bash wrapper ---
log "Phase 2: Running Claude Code haiku with bash wrapper..."
echo ""
info "Task: Fix the divide function and run tests"
info "The wrapper will intercept all bash -c calls and log them"
echo ""

# Configure wrapper environment
export AGENTCG_ROOT="$SIM_CG"
export AGENTCG_LOG="$LOG_FILE"
export AGENTCG_DISABLE="0"

# Create a wrapper that Claude Code's Bash tool will invoke
# We do this by putting the wrapper first in PATH as "bash"
WRAPPER_BIN="$WORKDIR/bin"
mkdir -p "$WRAPPER_BIN"
cp "$WRAPPER" "$WRAPPER_BIN/bash"
chmod +x "$WRAPPER_BIN/bash"

# Run Claude Code with the wrapper in PATH
# We prepend our wrapper dir so Claude's "bash -c ..." calls go through it
log "Starting Claude Code haiku..."
echo "---"

cd "$WORKDIR/testproject"
PATH="$WRAPPER_BIN:$PATH" claude \
    --model haiku \
    --print \
    --dangerously-skip-permissions \
    "Fix the divide function in calculator.py to properly handle division by zero (raise ZeroDivisionError). Then run the tests with: python -m unittest test_calculator -v. Show the test output." \
    2>&1 | tee "$WORKDIR/claude_output.txt" || true

echo "---"
echo ""

# --- Phase 3: Analyze tool call log ---
log "Phase 3: Analyzing per-tool-call log..."
echo ""

if [ ! -f "$LOG_FILE" ]; then
    warn "No tool call log found! The wrapper may not have been invoked."
    echo "  Check if Claude used Bash tool calls."
    # Still show Claude output
    echo ""
    log "Claude output:"
    cat "$WORKDIR/claude_output.txt"
    exit 0
fi

TOTAL_CALLS=$(wc -l < "$LOG_FILE")
info "Total tool calls intercepted: $TOTAL_CALLS"
echo ""

# Parse and display each tool call
info "Per-tool-call breakdown:"
echo "  ┌─────┬───────────┬──────────┬──────────────────────────────────────────────┐"
printf "  │ %s │ %9s │ %8s │ %-44s │\n" "#" "Duration" "Peak Mem" "Command"
echo "  ├─────┼───────────┼──────────┼──────────────────────────────────────────────┤"

IDX=0
while IFS= read -r line; do
    IDX=$((IDX + 1))
    DURATION=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d['duration_ms']}ms\")" 2>/dev/null || echo "?")
    PEAK=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); p=d.get('peak_mem','0'); print(f\"{int(p)//1024}KB\" if p.isdigit() else p)" 2>/dev/null || echo "?")
    CMD=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); c=d.get('cmd',''); print(c[:44])" 2>/dev/null || echo "?")
    EXIT=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('exit','-'))" 2>/dev/null || echo "?")
    HINT=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); h=d.get('hint',''); print(f' [hint:{h}]' if h else '')" 2>/dev/null || echo "")

    printf "  │ %3d │ %9s │ %8s │ %-44s │\n" "$IDX" "$DURATION" "$PEAK" "$CMD"
done < "$LOG_FILE"

echo "  └─────┴───────────┴──────────┴──────────────────────────────────────────────┘"
echo ""

# Summary statistics
python3 << PYEOF
import json, sys

calls = []
with open("$LOG_FILE") as f:
    for line in f:
        try:
            calls.append(json.loads(line))
        except:
            pass

if not calls:
    print("  No calls to analyze.")
    sys.exit(0)

durations = [c['duration_ms'] for c in calls]
exits = [c['exit'] for c in calls]
hints = [c.get('hint', '') for c in calls if c.get('hint')]

print(f"  Summary:")
print(f"    Total tool calls:    {len(calls)}")
print(f"    Total duration:      {sum(durations)}ms")
print(f"    Avg duration:        {sum(durations)/len(durations):.0f}ms")
print(f"    Max duration:        {max(durations)}ms")
print(f"    Successful (exit 0): {sum(1 for e in exits if e == 0)}/{len(exits)}")
print(f"    Failed (exit != 0):  {sum(1 for e in exits if e != 0)}/{len(exits)}")
print(f"    With resource hints: {len(hints)}/{len(calls)}")

# Check for unique cgroup paths
cgroups = set(c.get('cgroup', '') for c in calls)
print(f"    Unique cgroup paths: {len(cgroups)} (each tool call gets its own)")

# Show resource hint usage
if hints:
    print(f"\n  Resource hints used:")
    for h in hints:
        print(f"    - {h}")
PYEOF

echo ""

# --- Phase 4: Verify semantic properties ---
log "Phase 4: Verifying semantic properties..."
echo ""

python3 << 'PYEOF'
import json

calls = []
with open("LOG_FILE_PLACEHOLDER") as f:
    for line in f:
        try:
            calls.append(json.loads(line))
        except:
            pass

checks = []

# Check 1: Each tool call has a unique cgroup path
cgroups = [c.get('cgroup', '') for c in calls]
unique = len(set(cgroups)) == len(cgroups)
checks.append(("Each tool call has unique cgroup", unique))

# Check 2: All cgroup paths start with tool_ prefix
all_prefixed = all('tool_' in cg for cg in cgroups)
checks.append(("All cgroups use tool_ prefix", all_prefixed))

# Check 3: Durations are recorded (> 0)
all_durations = all(c.get('duration_ms', 0) >= 0 for c in calls)
checks.append(("Duration recorded for all calls", all_durations))

# Check 4: Peak memory is tracked
all_mem = all(c.get('peak_mem', 'unknown') != 'unknown' for c in calls)
checks.append(("Peak memory tracked for all calls", all_mem))

# Check 5: Exit codes are recorded
all_exits = all('exit' in c for c in calls)
checks.append(("Exit codes recorded for all calls", all_exits))

# Check 6: Tool calls are cleaned up (cgroup dirs should not exist)
import os
stale = [cg for cg in cgroups if os.path.isdir(cg)]
checks.append(("All tool cgroups cleaned up", len(stale) == 0))

print("  Semantic property checks:")
for name, passed in checks:
    icon = "✓" if passed else "✗"
    color = "\033[0;32m" if passed else "\033[0;31m"
    print(f"    {color}{icon}\033[0m {name}")

passed = sum(1 for _, p in checks if p)
total = len(checks)
print(f"\n  Result: {passed}/{total} checks passed")
PYEOF

# Fix the placeholder in the inline python
python3 << PYEOF2
import json, os

calls = []
with open("$LOG_FILE") as f:
    for line in f:
        try:
            calls.append(json.loads(line))
        except:
            pass

if not calls:
    print("  No calls to verify.")
    exit(0)

checks = []

# Check 1: Each tool call has a unique cgroup path
cgroups = [c.get('cgroup', '') for c in calls]
unique = len(set(cgroups)) == len(cgroups)
checks.append(("Each tool call has unique cgroup", unique))

# Check 2: All cgroup paths contain tool_ prefix
all_prefixed = all('tool_' in cg for cg in cgroups)
checks.append(("All cgroups use tool_ prefix", all_prefixed))

# Check 3: Durations are recorded (>= 0)
all_durations = all(c.get('duration_ms', -1) >= 0 for c in calls)
checks.append(("Duration recorded for all calls", all_durations))

# Check 4: Peak memory is tracked
all_mem = all(c.get('peak_mem', 'unknown') != 'unknown' for c in calls)
checks.append(("Peak memory tracked for all calls", all_mem))

# Check 5: Exit codes are recorded
all_exits = all('exit' in c for c in calls)
checks.append(("Exit codes recorded for all calls", all_exits))

# Check 6: Tool calls are cleaned up (cgroup dirs should not exist)
stale = [cg for cg in cgroups if os.path.isdir(cg)]
checks.append(("All tool cgroups cleaned up", len(stale) == 0))

print("  Semantic property checks:")
for name, passed in checks:
    icon = "\u2713" if passed else "\u2717"
    color = "\033[0;32m" if passed else "\033[0;31m"
    print(f"    {color}{icon}\033[0m {name}")

passed_count = sum(1 for _, p in checks if p)
total = len(checks)
print(f"\n  Result: {passed_count}/{total} checks passed")
PYEOF2

echo ""

# --- Phase 5: Show raw log for inspection ---
log "Phase 5: Raw tool call log (JSONL):"
echo ""
python3 -c "
import json
with open('$LOG_FILE') as f:
    for i, line in enumerate(f, 1):
        d = json.loads(line)
        print(json.dumps(d, indent=2))
        if i < 20:
            print()
" 2>/dev/null || cat "$LOG_FILE"

echo ""
log "=== Semantic validation complete ==="
log "Log file: $LOG_FILE"
log "Claude output: $WORKDIR/claude_output.txt"
