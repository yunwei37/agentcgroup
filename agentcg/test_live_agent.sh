#!/bin/bash
#
# test_live_agent.sh - Run Claude Code haiku through bash wrapper
#
# Strategy: We can't intercept Claude's internal bash (it uses /bin/bash directly).
# Instead we:
#   1. Run agent-like commands through the wrapper directly to verify tracking
#   2. Run Claude Code haiku and capture its tool trace
#   3. Cross-reference tool calls with wrapper logs
#
# Usage: ./test_live_agent.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$SCRIPT_DIR/bash_wrapper_local.sh"
WORKDIR=$(mktemp -d /tmp/agentcg_live_test_XXXXXX)
LOG_FILE="$WORKDIR/tool_calls.jsonl"
SIM_CG="$WORKDIR/cgroup_sim/session_high"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log()  { echo -e "${GREEN}[TEST]${NC} $1"; }
info() { echo -e "${CYAN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
sep()  { echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

cleanup() {
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

mkdir -p "$SIM_CG"
export AGENTCG_ROOT="$SIM_CG"
export AGENTCG_LOG="$LOG_FILE"

sep
echo -e "${BOLD} AgentCgroup Per-Tool-Call Semantic Validation${NC}"
sep
echo ""

# ================================================================
# PART 1: Per-tool-call tracking with agent-like commands
# ================================================================
log "PART 1: Per-tool-call tracking with agent-like commands"
echo ""

# Set up a test project
mkdir -p "$WORKDIR/testproject"
cat > "$WORKDIR/testproject/calculator.py" << 'PYEOF'
def add(a, b):
    return a + b

def subtract(a, b):
    return a - b

def multiply(a, b):
    return a * b

def divide(a, b):
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
        with self.assertRaises(ZeroDivisionError):
            divide(10, 0)

if __name__ == "__main__":
    unittest.main()
PYEOF

cd "$WORKDIR/testproject"

info "Simulating agent tool calls through wrapper..."
echo ""

# Simulate a typical agent tool-call sequence:
# 1. Read/explore (lightweight)
info "Tool call 1: cat calculator.py (lightweight read)"
"$WRAPPER" -c "cat calculator.py" > /dev/null
echo "  done"

# 2. Run tests (heavier)
info "Tool call 2: python -m unittest test_calculator -v (test execution)"
"$WRAPPER" -c "cd $WORKDIR/testproject && python3 -m unittest test_calculator -v 2>&1" || true
echo "  done"

# 3. Git status (lightweight)
info "Tool call 3: git status (lightweight)"
"$WRAPPER" -c "git status 2>/dev/null || echo 'not a git repo'" > /dev/null
echo "  done"

# 4. Apply fix via sed
info "Tool call 4: sed edit (fix the bug)"
"$WRAPPER" -c "cd $WORKDIR/testproject && sed -i 's/return a \/ b/if b == 0:\n        raise ZeroDivisionError(\"division by zero\")\n    return a \/ b/' calculator.py"
echo "  done"

# 5. Run tests again (heavier, should pass now)
info "Tool call 5: python -m unittest test_calculator -v (re-run tests)"
"$WRAPPER" -c "cd $WORKDIR/testproject && python3 -m unittest test_calculator -v 2>&1" || true
echo "  done"

# 6. With resource hint (Agent → System)
info "Tool call 6: pytest with memory:high hint (upward declaration)"
AGENT_RESOURCE_HINT="memory:high" "$WRAPPER" -c "cd $WORKDIR/testproject && python3 -m unittest test_calculator 2>&1" || true
echo "  done"

# 7. With resource hint low (lightweight op)
info "Tool call 7: ls with memory:low hint (upward declaration)"
AGENT_RESOURCE_HINT="memory:low" "$WRAPPER" -c "ls $WORKDIR/testproject" > /dev/null
echo "  done"

echo ""

# --- Analyze Part 1 ---
log "Per-tool-call log analysis:"
echo ""

TOTAL_CALLS=$(wc -l < "$LOG_FILE")
info "Total tool calls intercepted: $TOTAL_CALLS"
echo ""

echo "  ┌─────┬───────────┬──────────┬────────────────┬────────────────────────────────────────┐"
printf "  │ %-3s │ %9s │ %8s │ %-14s │ %-38s │\n" "#" "Duration" "Peak Mem" "Hint" "Command"
echo "  ├─────┼───────────┼──────────┼────────────────┼────────────────────────────────────────┤"

IDX=0
while IFS= read -r line; do
    IDX=$((IDX + 1))
    DURATION=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d['duration_ms']}ms\")" 2>/dev/null || echo "?")
    PEAK=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); p=d.get('peak_mem','0'); print(f\"{int(p)//1024}KB\" if p.isdigit() else p)" 2>/dev/null || echo "?")
    CMD=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); c=d.get('cmd',''); print(c[:38])" 2>/dev/null || echo "?")
    HINT=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); h=d.get('hint',''); print(h if h else '(none)')" 2>/dev/null || echo "?")

    printf "  │ %3d │ %9s │ %8s │ %-14s │ %-38s │\n" "$IDX" "$DURATION" "$PEAK" "$HINT" "$CMD"
done < "$LOG_FILE"

echo "  └─────┴───────────┴──────────┴────────────────┴────────────────────────────────────────┘"
echo ""

# Verify semantic properties
log "Semantic property checks:"
echo ""

python3 << PYEOF
import json, os

calls = []
with open("$LOG_FILE") as f:
    for line in f:
        try:
            calls.append(json.loads(line))
        except:
            pass

checks = []

# 1. Each tool call gets unique cgroup
cgroups = [c.get('cgroup', '') for c in calls]
unique = len(set(cgroups)) == len(cgroups)
checks.append(("Each tool call has unique cgroup path", unique))

# 2. All cgroup paths use tool_ prefix
all_prefixed = all('tool_' in cg for cg in cgroups)
checks.append(("All cgroups use tool_ naming convention", all_prefixed))

# 3. Duration tracked
all_dur = all(c.get('duration_ms', -1) >= 0 for c in calls)
checks.append(("Duration recorded for all calls", all_dur))

# 4. Peak memory tracked
all_mem = all(c.get('peak_mem', 'unknown') != 'unknown' for c in calls)
checks.append(("Peak memory tracked for all calls", all_mem))

# 5. Exit codes recorded
all_exits = all('exit' in c for c in calls)
checks.append(("Exit codes recorded for all calls", all_exits))

# 6. Cgroups cleaned up
stale = [cg for cg in cgroups if os.path.isdir(cg)]
checks.append(("All ephemeral cgroups cleaned up after use", len(stale) == 0))

# 7. Resource hints captured
hints = [c for c in calls if c.get('hint')]
checks.append(("Resource hints captured when declared", len(hints) >= 2))

# 8. mem_high set for hinted calls
hinted_with_limit = [c for c in calls if c.get('hint') and c.get('mem_high', 'max') != 'max']
hinted_low = [c for c in calls if c.get('hint') == 'memory:low']
checks.append(("memory:low hint sets mem_high to 256MB",
    any(c.get('mem_high') == str(256*1024*1024) for c in hinted_low) if hinted_low else False))

for name, passed in checks:
    icon = "\u2713" if passed else "\u2717"
    color = "\033[0;32m" if passed else "\033[0;31m"
    print(f"  {color}{icon}\033[0m {name}")

passed_count = sum(1 for _, p in checks if p)
total = len(checks)
print(f"\n  Result: {passed_count}/{total} checks passed")
PYEOF

echo ""

# ================================================================
# PART 2: Run Claude Code haiku and capture its tool trace
# ================================================================
sep
log "PART 2: Running Claude Code haiku for a real agent task"
sep
echo ""

CLAUDE_LOG="$WORKDIR/claude_wrapper.jsonl"
export AGENTCG_LOG="$CLAUDE_LOG"

# Create a fresh test project for Claude
mkdir -p "$WORKDIR/claude_project"
cat > "$WORKDIR/claude_project/calculator.py" << 'PYEOF'
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

cat > "$WORKDIR/claude_project/test_calculator.py" << 'PYEOF'
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
        with self.assertRaises(ZeroDivisionError):
            divide(10, 0)

if __name__ == "__main__":
    unittest.main()
PYEOF

info "Running Claude Code haiku on the test project..."
info "Task: Fix divide() and run tests using the wrapper"
echo ""

# Run Claude Code and ask it to use our wrapper for bash commands
cd "$WORKDIR/claude_project"

PROMPT='Fix the divide function in calculator.py to handle division by zero.

IMPORTANT: For EVERY bash command you run, please use the wrapper script at '"$WRAPPER"' instead of plain bash.
For example, instead of running a command directly, do:
  '"$WRAPPER"' -c "your command here"

You can also set AGENT_RESOURCE_HINT before commands:
  AGENT_RESOURCE_HINT="memory:low" '"$WRAPPER"' -c "cat calculator.py"
  AGENT_RESOURCE_HINT="memory:high" '"$WRAPPER"' -c "python3 -m unittest test_calculator -v"

Steps:
1. Read calculator.py using the wrapper with memory:low hint
2. Fix the divide function
3. Run the tests using the wrapper with memory:high hint
4. Show git diff of your changes'

claude --model haiku \
    --print \
    --dangerously-skip-permissions \
    "$PROMPT" \
    2>&1 | tee "$WORKDIR/claude_output.txt" || true

echo ""

# --- Analyze Claude's wrapper usage ---
log "Claude Code tool call analysis:"
echo ""

if [ -f "$CLAUDE_LOG" ] && [ -s "$CLAUDE_LOG" ]; then
    CLAUDE_CALLS=$(wc -l < "$CLAUDE_LOG")
    info "Claude Code tool calls through wrapper: $CLAUDE_CALLS"
    echo ""

    echo "  ┌─────┬───────────┬──────────┬────────────────┬────────────────────────────────────────┐"
    printf "  │ %-3s │ %9s │ %8s │ %-14s │ %-38s │\n" "#" "Duration" "Peak Mem" "Hint" "Command"
    echo "  ├─────┼───────────┼──────────┼────────────────┼────────────────────────────────────────┤"

    IDX=0
    while IFS= read -r line; do
        IDX=$((IDX + 1))
        DURATION=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d['duration_ms']}ms\")" 2>/dev/null || echo "?")
        PEAK=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); p=d.get('peak_mem','0'); print(f\"{int(p)//1024}KB\" if p.isdigit() else p)" 2>/dev/null || echo "?")
        CMD=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); c=d.get('cmd',''); print(c[:38])" 2>/dev/null || echo "?")
        HINT=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); h=d.get('hint',''); print(h if h else '(none)')" 2>/dev/null || echo "?")

        printf "  │ %3d │ %9s │ %8s │ %-14s │ %-38s │\n" "$IDX" "$DURATION" "$PEAK" "$HINT" "$CMD"
    done < "$CLAUDE_LOG"

    echo "  └─────┴───────────┴──────────┴────────────────┴────────────────────────────────────────┘"
    echo ""

    # Check if Claude used hints
    python3 << PYEOF2
import json
calls = []
with open("$CLAUDE_LOG") as f:
    for line in f:
        try:
            calls.append(json.loads(line))
        except:
            pass

hints_used = [c for c in calls if c.get('hint')]
print(f"  Calls with resource hints: {len(hints_used)}/{len(calls)}")
for c in hints_used:
    print(f"    hint={c['hint']}, cmd={c['cmd'][:60]}")
PYEOF2
else
    warn "No wrapper calls from Claude detected."
    info "Claude may have used its built-in Bash tool directly."
    info "See Claude's output above for what it did."
fi

echo ""
sep
log "Validation complete!"
sep
echo ""
info "Part 1 log: $LOG_FILE"
if [ -f "$CLAUDE_LOG" ]; then
    info "Part 2 log: $CLAUDE_LOG"
fi
info "Claude output: $WORKDIR/claude_output.txt"
