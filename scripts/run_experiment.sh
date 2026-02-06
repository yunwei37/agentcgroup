#!/bin/bash

# ============================================
# SWE-bench Experiment Runner with Auto-restart
# Runs llama-server + swebench experiments
# ============================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

# Config
LLAMA_SERVER_DIR="/home/yunwei37/workspace/gpu/llama.cpp"
LLAMA_MODEL="unsloth/GLM-4.7-Flash-GGUF:Q4_K_M"
LLAMA_PORT=8080
LLAMA_CTX=128000
MAX_RETRIES=1
HEALTH_CHECK_INTERVAL=60

# Create log directory
mkdir -p "$LOG_DIR"

# Log files
LLAMA_LOG="$LOG_DIR/llama_server_${TIMESTAMP}.log"
SWEBENCH_LOG="$LOG_DIR/swebench_${TIMESTAMP}.log"
MONITOR_LOG="$LOG_DIR/monitor_${TIMESTAMP}.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$MONITOR_LOG"
}

# Rotate old logs (keep last 5)
rotate_logs() {
    local prefix=$1
    local count=$(ls -1 "$LOG_DIR"/${prefix}_*.log 2>/dev/null | wc -l)
    if [ "$count" -gt 5 ]; then
        ls -1t "$LOG_DIR"/${prefix}_*.log | tail -n +6 | xargs -r rm -f
        log "Rotated old ${prefix} logs"
    fi
}

rotate_logs "llama_server"
rotate_logs "swebench"
rotate_logs "monitor"

# Check if llama-server is running
check_llama_running() {
    pgrep -f "llama-server.*$LLAMA_PORT" > /dev/null 2>&1
}

# Check if llama-server is healthy
check_llama_health() {
    curl -s --max-time 10 "http://localhost:$LLAMA_PORT/health" 2>/dev/null | grep -q "ok"
}

# Check if swebench is running
check_swebench_running() {
    pgrep -f "run_all_swebench_images.py" > /dev/null 2>&1
}

# Start llama-server
start_llama() {
    log "Starting llama-server..."
    cd "$LLAMA_SERVER_DIR"

    nohup build/bin/llama-server \
        -hf "$LLAMA_MODEL" \
        -c "$LLAMA_CTX" \
        --port "$LLAMA_PORT" \
        --jinja \
        > "$LLAMA_LOG" 2>&1 &

    local pid=$!
    disown $pid
    log "llama-server started with PID: $pid"

    # Wait for server to be ready
    log "Waiting for llama-server to initialize..."
    for i in {1..60}; do
        if check_llama_health; then
            log "llama-server is healthy!"
            return 0
        fi
        sleep 2
    done

    log "ERROR: llama-server failed to start"
    return 1
}

# Stop llama-server
stop_llama() {
    log "Stopping llama-server..."
    pkill -f "llama-server.*$LLAMA_PORT" 2>/dev/null
    sleep 2
}

# Start swebench experiment
start_swebench() {
    log "Starting swebench experiment..."
    cd "$PROJECT_DIR"

    source .venv/bin/activate

    nohup .venv/bin/python scripts/run_all_swebench_images.py \
        --model qwen3 \
        --task-list task_list.json \
        --resume \
        > "$SWEBENCH_LOG" 2>&1 &

    local pid=$!
    disown $pid
    log "swebench started with PID: $pid"
    return 0
}

# Stop swebench
stop_swebench() {
    log "Stopping swebench..."
    pkill -f "run_all_swebench_images.py" 2>/dev/null
    podman stop -a 2>/dev/null
    sleep 2
}

# Main startup
main_start() {
    log "============================================"
    log "Starting SWE-bench Experiment"
    log "Model: $LLAMA_MODEL"
    log "Log directory: $LOG_DIR"
    log "============================================"

    # Start llama-server if not running
    if check_llama_running && check_llama_health; then
        log "llama-server already running and healthy"
    else
        if check_llama_running; then
            stop_llama
        fi
        start_llama || return 1
    fi

    # Start swebench if not running
    if check_swebench_running; then
        log "swebench already running"
    else
        start_swebench
    fi

    log "All services started!"
}

# Monitor loop
monitor_loop() {
    local llama_retries=0
    local swebench_retries=0

    log "Starting monitor loop (check every ${HEALTH_CHECK_INTERVAL}s)..."

    while true; do
        sleep "$HEALTH_CHECK_INTERVAL"

        # Check llama-server
        if ! check_llama_health; then
            log "WARNING: llama-server is not healthy!"

            if [ $llama_retries -lt $MAX_RETRIES ]; then
                log "Attempting restart (retry $((llama_retries + 1))/$MAX_RETRIES)..."
                stop_llama
                if start_llama; then
                    llama_retries=0
                    # Restart swebench after llama restart
                    stop_swebench
                    sleep 5
                    start_swebench
                else
                    llama_retries=$((llama_retries + 1))
                fi
            else
                log "ERROR: llama-server failed after $MAX_RETRIES retries. Giving up."
            fi
        else
            llama_retries=0
        fi

        # Check swebench
        if ! check_swebench_running; then
            # Check if it finished naturally (check progress)
            local progress_file="$PROJECT_DIR/experiments/all_images_local/progress.json"
            if [ -f "$progress_file" ]; then
                local completed=$(python3 -c "import json; print(len(json.load(open('$progress_file')).get('completed', [])))" 2>/dev/null || echo "0")
                log "swebench stopped. Completed tasks: $completed"
            fi

            if [ $swebench_retries -lt $MAX_RETRIES ]; then
                log "Attempting restart (retry $((swebench_retries + 1))/$MAX_RETRIES)..."
                if check_llama_health; then
                    start_swebench
                    swebench_retries=$((swebench_retries + 1))
                else
                    log "Cannot restart swebench: llama-server not healthy"
                fi
            else
                log "swebench finished or failed after retries."
                # Reset retry counter to allow future restarts
                swebench_retries=0
            fi
        else
            swebench_retries=0
        fi

        # Log status
        local llama_status="DOWN"
        local swebench_status="DOWN"
        check_llama_health && llama_status="OK"
        check_swebench_running && swebench_status="RUNNING"
        log "Status: llama=$llama_status, swebench=$swebench_status"
    done
}

# Parse command
case "${1:-start}" in
    start)
        main_start
        echo ""
        echo "To monitor: tail -f $MONITOR_LOG"
        echo "To run monitor loop: $0 monitor"
        ;;
    monitor)
        main_start && monitor_loop
        ;;
    stop)
        log "Stopping all services..."
        stop_swebench
        stop_llama
        log "All services stopped"
        ;;
    status)
        echo "llama-server: $(check_llama_health && echo 'healthy' || echo 'down')"
        echo "swebench: $(check_swebench_running && echo 'running' || echo 'stopped')"
        if [ -f "$PROJECT_DIR/experiments/all_images_local/progress.json" ]; then
            python3 -c "
import json
p = json.load(open('$PROJECT_DIR/experiments/all_images_local/progress.json'))
completed = len(p.get('completed', []))
success = sum(1 for r in p.get('results', {}).values() if r.get('success'))
print(f'Progress: {completed} completed, {success} successful')
"
        fi
        ;;
    *)
        echo "Usage: $0 {start|monitor|stop|status}"
        exit 1
        ;;
esac
