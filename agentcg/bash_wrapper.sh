#!/usr/bin/real-bash
# AgentCgroup Bash Wrapper
#
# Transparent wrapper that creates per-tool-call ephemeral cgroups for each
# "bash -c ..." invocation. Supports bidirectional resource negotiation:
#   - Upward (Agent→System): AGENT_RESOURCE_HINT env var declares resource needs
#   - Downward (System→Agent): OOM feedback via stderr with actionable suggestions
#
# Installation: rename /usr/bin/bash to /usr/bin/real-bash, install this as /usr/bin/bash
# Or: bind-mount this over /usr/bin/bash in the container
#
# Environment variables:
#   AGENTCG_ROOT       - parent cgroup path (default: /sys/fs/cgroup/agentcg/session_high)
#   AGENTCG_LOG        - log file path (default: /tmp/agentcg_tools.jsonl)
#   AGENT_RESOURCE_HINT - resource hint from agent (e.g., "memory:low", "memory:2g")
#   AGENTCG_DISABLE    - set to "1" to disable wrapper (passthrough mode)

REAL_BASH="/usr/bin/real-bash"
CGROUP_ROOT="${AGENTCG_ROOT:-/sys/fs/cgroup/agentcg/session_high}"
LOG_FILE="${AGENTCG_LOG:-/tmp/agentcg_tools.jsonl}"

# If disabled, pass through directly
if [ "${AGENTCG_DISABLE:-0}" = "1" ]; then
    exec "$REAL_BASH" "$@"
fi

# Non "-c" invocations pass through (interactive bash, source scripts, etc.)
if [ "$1" != "-c" ]; then
    exec "$REAL_BASH" "$@"
fi

# --- Per-tool-call cgroup creation ---

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
        # Use shell arithmetic; fall back to max on error
        MEM_HIGH=$(( NUM * 1024 * 1024 * 1024 )) 2>/dev/null || MEM_HIGH="max"
        ;;
    memory:*[mM])
        NUM="${HINT#memory:}"
        NUM="${NUM%[mM]}"
        MEM_HIGH=$(( NUM * 1024 * 1024 )) 2>/dev/null || MEM_HIGH="max"
        ;;
    "") ;;  # no hint, use default (max)
    *)  ;;  # unknown hint format, ignore
esac

# Create ephemeral child cgroup
if mkdir -p "$TOOL_CG" 2>/dev/null; then
    # Apply memory.high if hint was provided
    if [ "$MEM_HIGH" != "max" ] && [ -f "$TOOL_CG/memory.high" ]; then
        echo "$MEM_HIGH" > "$TOOL_CG/memory.high" 2>/dev/null
    fi
    # Move self into child cgroup
    if echo $$ > "$TOOL_CG/cgroup.procs" 2>/dev/null; then
        IN_CG=1
    fi
fi

# Record start time (nanoseconds if available, seconds otherwise)
START_NS=$(date +%s%N 2>/dev/null || date +%s)

# --- Execute the actual command ---
"$REAL_BASH" "$@"
EXIT_CODE=$?

# Record end time
END_NS=$(date +%s%N 2>/dev/null || date +%s)

# --- Collect resource metrics ---
PEAK_MEM=""
CURRENT_MEM=""
if [ "$IN_CG" = "1" ]; then
    PEAK_MEM=$(cat "$TOOL_CG/memory.peak" 2>/dev/null || echo "")
    CURRENT_MEM=$(cat "$TOOL_CG/memory.current" 2>/dev/null || echo "")
fi

# --- Downward feedback: System → Agent ---
if [ $EXIT_CODE -eq 137 ] && [ "$IN_CG" = "1" ]; then
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

# --- Log tool call metrics (JSON Lines) ---
if [ "$IN_CG" = "1" ]; then
    DURATION_NS=$((END_NS - START_NS))
    DURATION_MS=$((DURATION_NS / 1000000))
    # Truncate command preview to 200 chars
    CMD_PREVIEW=$(printf '%s' "$2" | head -c 200 | tr '"' "'")
    printf '{"ts":%s,"pid":%d,"cgroup":"%s","cmd":"%s","exit":%d,"duration_ms":%d,"peak_mem":"%s","current_mem":"%s","hint":"%s"}\n' \
        "$START_NS" "$$" "$TOOL_CG" "$CMD_PREVIEW" "$EXIT_CODE" "$DURATION_MS" \
        "${PEAK_MEM:-unknown}" "${CURRENT_MEM:-unknown}" "$HINT" \
        >> "$LOG_FILE" 2>/dev/null
fi

# --- Cleanup: move back to parent cgroup, remove child ---
if [ "$IN_CG" = "1" ]; then
    echo $$ > "$CGROUP_ROOT/cgroup.procs" 2>/dev/null
    rmdir "$TOOL_CG" 2>/dev/null
fi

exit $EXIT_CODE
