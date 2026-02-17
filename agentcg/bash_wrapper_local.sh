#!/bin/bash
# AgentCgroup Bash Wrapper - Local Testing Version
#
# This version works without root / real cgroups by using a tmpdir to simulate
# cgroup filesystem. It still wraps every "bash -c ..." call with:
#   - Per-tool-call resource tracking (via /proc/self for memory)
#   - Resource hint parsing (AGENT_RESOURCE_HINT)
#   - OOM feedback on exit code 137
#   - JSONL logging of every tool call
#
# Usage: Set AGENTCG_BASH_WRAPPER=<path-to-this-file> and ensure it's called as bash.
#
# Environment variables:
#   AGENTCG_ROOT       - simulated cgroup root (default: /tmp/agentcg_sim)
#   AGENTCG_LOG        - log file (default: /tmp/agentcg_tools.jsonl)
#   AGENT_RESOURCE_HINT - resource hint from agent
#   AGENTCG_DISABLE    - set to "1" to disable

REAL_BASH="/bin/bash"
CGROUP_ROOT="${AGENTCG_ROOT:-/tmp/agentcg_sim/session_high}"
LOG_FILE="${AGENTCG_LOG:-/tmp/agentcg_tools.jsonl}"

# If disabled, pass through
if [ "${AGENTCG_DISABLE:-0}" = "1" ]; then
    exec "$REAL_BASH" "$@"
fi

# Non "-c" invocations pass through
if [ "$1" != "-c" ]; then
    exec "$REAL_BASH" "$@"
fi

# --- Per-tool-call tracking ---

TOOL_CG="${CGROUP_ROOT}/tool_$$_$(date +%s%N 2>/dev/null || echo $$)"
IN_CG=0

# Parse resource hint (upward: Agent → System)
HINT="${AGENT_RESOURCE_HINT:-}"
MEM_HIGH="max"
case "$HINT" in
    memory:low)     MEM_HIGH=$((256 * 1024 * 1024)) ;;
    memory:medium)  MEM_HIGH=$((1024 * 1024 * 1024)) ;;
    memory:high)    MEM_HIGH="max" ;;
    memory:*[gG])
        NUM="${HINT#memory:}"
        NUM="${NUM%[gG]}"
        MEM_HIGH=$(( NUM * 1024 * 1024 * 1024 )) 2>/dev/null || MEM_HIGH="max"
        ;;
    memory:*[mM])
        NUM="${HINT#memory:}"
        NUM="${NUM%[mM]}"
        MEM_HIGH=$(( NUM * 1024 * 1024 )) 2>/dev/null || MEM_HIGH="max"
        ;;
    "") ;;
    *)  ;;
esac

# Create simulated tool cgroup dir (for tracking, not real isolation)
if mkdir -p "$TOOL_CG" 2>/dev/null; then
    # Write hint info
    echo "$MEM_HIGH" > "$TOOL_CG/memory.high" 2>/dev/null
    echo "$$" > "$TOOL_CG/cgroup.procs" 2>/dev/null
    IN_CG=1
fi

# Get memory before execution (from /proc)
MEM_BEFORE=$(awk '/VmRSS/{print $2}' /proc/$$/status 2>/dev/null || echo 0)

START_NS=$(date +%s%N 2>/dev/null || date +%s)

# --- Execute the actual command ---
"$REAL_BASH" "$@"
EXIT_CODE=$?

END_NS=$(date +%s%N 2>/dev/null || date +%s)

# Get memory after execution
MEM_AFTER=$(awk '/VmRSS/{print $2}' /proc/$$/status 2>/dev/null || echo 0)

# Write simulated memory.peak (kB from /proc → bytes)
if [ "$IN_CG" = "1" ]; then
    PEAK_KB=$((MEM_AFTER > MEM_BEFORE ? MEM_AFTER : MEM_BEFORE))
    PEAK_BYTES=$((PEAK_KB * 1024))
    echo "$PEAK_BYTES" > "$TOOL_CG/memory.peak" 2>/dev/null
    echo "$((MEM_AFTER * 1024))" > "$TOOL_CG/memory.current" 2>/dev/null
fi

# --- Downward feedback: System → Agent ---
if [ $EXIT_CODE -eq 137 ] && [ "$IN_CG" = "1" ]; then
    PEAK_MEM=$(cat "$TOOL_CG/memory.peak" 2>/dev/null || echo "")
    PEAK_MB=""
    if [ -n "$PEAK_MEM" ]; then
        PEAK_MB=$((PEAK_MEM / 1024 / 1024))
    fi
    echo "[Resource] Command killed (OOM, exit 137). Peak memory: ${PEAK_MB:-unknown}MB." >&2
    echo "[Resource] The command exceeded the available memory budget." >&2
    echo "[Resource] Suggestions: run more targeted operations (e.g., specific test" >&2
    echo "  files instead of full test suite), reduce data size, or split into" >&2
    echo "  smaller steps. You can also request more memory by setting" >&2
    echo "  AGENT_RESOURCE_HINT=\"memory:<size>g\" before the command." >&2
fi

# --- Log tool call metrics (JSONL) ---
if [ "$IN_CG" = "1" ]; then
    DURATION_NS=$((END_NS - START_NS))
    DURATION_MS=$((DURATION_NS / 1000000))
    PEAK_MEM=$(cat "$TOOL_CG/memory.peak" 2>/dev/null || echo "unknown")
    CURRENT_MEM=$(cat "$TOOL_CG/memory.current" 2>/dev/null || echo "unknown")
    CMD_PREVIEW=$(printf '%s' "$2" | head -c 200 | tr '"\\' "' ")
    printf '{"ts":%s,"pid":%d,"cgroup":"%s","cmd":"%s","exit":%d,"duration_ms":%d,"peak_mem":"%s","current_mem":"%s","hint":"%s","mem_high":"%s"}\n' \
        "$START_NS" "$$" "$TOOL_CG" "$CMD_PREVIEW" "$EXIT_CODE" "$DURATION_MS" \
        "${PEAK_MEM}" "${CURRENT_MEM}" "$HINT" "$MEM_HIGH" \
        >> "$LOG_FILE" 2>/dev/null
fi

# --- Cleanup ---
if [ "$IN_CG" = "1" ]; then
    rm -f "$TOOL_CG/memory.high" "$TOOL_CG/memory.peak" "$TOOL_CG/memory.current" "$TOOL_CG/cgroup.procs" 2>/dev/null
    rmdir "$TOOL_CG" 2>/dev/null
fi

exit $EXIT_CODE
