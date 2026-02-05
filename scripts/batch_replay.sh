#!/bin/bash
# Batch replay all traces from batch_swebench_18tasks
# Usage: ./scripts/batch_replay.sh

set -e

cd ~/agentcgroup
source .venv/bin/activate

BATCH_DIR="experiments/batch_swebench_18tasks"
OUTPUT_BASE="experiments/replays"

# Get all attempt directories
ATTEMPTS=$(ls -d ${BATCH_DIR}/*/attempt_1 2>/dev/null | sort)

total=$(echo "$ATTEMPTS" | wc -l)
current=0

echo "============================================================"
echo "Batch Replay - $total tasks"
echo "============================================================"
echo "Output directory: $OUTPUT_BASE"
echo "============================================================"
echo ""

for attempt_dir in $ATTEMPTS; do
    current=$((current + 1))
    task_name=$(basename $(dirname $attempt_dir))

    echo ""
    echo "============================================================"
    echo "[$current/$total] Replaying: $task_name"
    echo "============================================================"

    # Check if trace exists
    if [ ! -f "$attempt_dir/trace.jsonl" ]; then
        echo "  SKIP: No trace.jsonl found"
        continue
    fi

    # Check if already completed (checkpoint/resume)
    output_dir="${OUTPUT_BASE}/${task_name}"
    if [ -f "$output_dir/resources.json" ]; then
        echo "  SKIP: Already completed (found $output_dir/resources.json)"
        continue
    fi

    # Run replay (unbuffered output)
    python -u scripts/replay_trace.py "$attempt_dir" \
        --output-dir "$output_dir"

    echo ""
    echo "[$current/$total] $task_name completed"
done

echo ""
echo "============================================================"
echo "All replays completed!"
echo "Results saved to: $OUTPUT_BASE"
echo "============================================================"
