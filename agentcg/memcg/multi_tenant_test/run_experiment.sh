#!/bin/bash
#
# run_experiment.sh - 运行多租户内存竞争实验
#
# 用法:
#   sudo ./run_experiment.sh baseline   # 运行无 BPF 基准测试
#   sudo ./run_experiment.sh bpf        # 运行有 BPF 测试
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CGROUP_ROOT="/sys/fs/cgroup/memcg_bpf_test"
RESULTS_DIR="$SCRIPT_DIR/results"

# 实验参数
# 设计: 3 个进程各 200MB = 600MB 总需求
#       总限制 500MB → 产生压力但不立即 OOM
#       memory.high = 150MB → 每个进程都会超过阈值
TOTAL_MEMORY_MB=500      # 总内存限制
PER_PROCESS_MB=200       # 每进程目标 200MB，总需求 600MB
MEMORY_HIGH_MB=150       # memory.high 阈值，触发限流
HOLD_SECONDS=5           # 保持时间
BPF_DELAY_MS=2000

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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

setup_cgroups() {
    log_info "Setting up cgroups at $CGROUP_ROOT"

    # 清理旧的 cgroup
    cleanup_cgroups 2>/dev/null || true

    # 创建父 cgroup
    mkdir -p $CGROUP_ROOT

    # 启用 memory controller
    echo "+memory" > $CGROUP_ROOT/cgroup.subtree_control 2>/dev/null || true

    # 不设置 memory.max (避免 OOM)，只用 memory.high 触发 BPF
    # echo "${TOTAL_MEMORY_MB}M" > $CGROUP_ROOT/memory.max
    echo "0" > $CGROUP_ROOT/memory.swap.max

    log_info "No memory.max limit (using memory.high only)"

    # 创建子 cgroups
    for name in high_session low_session_1 low_session_2; do
        mkdir -p $CGROUP_ROOT/$name
        # 设置 memory.high 阈值 (触发 BPF 回调)
        echo "${MEMORY_HIGH_MB}M" > $CGROUP_ROOT/$name/memory.high
        log_info "Created $CGROUP_ROOT/$name (memory.high=${MEMORY_HIGH_MB}MB)"
    done
}

cleanup_cgroups() {
    log_info "Cleaning up cgroups"

    # 先杀死 cgroup 中的进程
    for name in high_session low_session_1 low_session_2; do
        if [ -f "$CGROUP_ROOT/$name/cgroup.procs" ]; then
            for pid in $(cat $CGROUP_ROOT/$name/cgroup.procs 2>/dev/null); do
                kill -9 $pid 2>/dev/null || true
            done
        fi
        rmdir $CGROUP_ROOT/$name 2>/dev/null || true
    done
    rmdir $CGROUP_ROOT 2>/dev/null || true
}

run_baseline() {
    local exp_name="baseline_$(date +%Y%m%d_%H%M%S)"
    local exp_dir="$RESULTS_DIR/$exp_name"
    mkdir -p "$exp_dir"

    log_info "Running BASELINE experiment (no BPF)"
    log_info "Results will be saved to: $exp_dir"

    setup_cgroups

    # 记录实验参数
    cat > "$exp_dir/config.json" << EOF
{
    "experiment": "baseline",
    "total_memory_mb": $TOTAL_MEMORY_MB,
    "per_process_mb": $PER_PROCESS_MB,
    "memory_high_mb": $MEMORY_HIGH_MB,
    "hold_seconds": $HOLD_SECONDS,
    "bpf_enabled": false
}
EOF

    # 并发启动 3 个内存压力进程
    log_info "Starting 3 concurrent memory stress processes..."
    log_info "  HIGH: $PER_PROCESS_MB MB"
    log_info "  LOW1: $PER_PROCESS_MB MB"
    log_info "  LOW2: $PER_PROCESS_MB MB"
    log_info "  Total demand: $((PER_PROCESS_MB * 3)) MB > $TOTAL_MEMORY_MB MB limit"

    local start_time=$(date +%s.%N)

    python3 "$SCRIPT_DIR/memory_stress.py" \
        --cgroup "$CGROUP_ROOT/high_session" \
        --memory-mb $PER_PROCESS_MB \
        --hold-seconds $HOLD_SECONDS \
        --name "HIGH" \
        --output "$exp_dir/high_result.json" &
    local pid_high=$!

    python3 "$SCRIPT_DIR/memory_stress.py" \
        --cgroup "$CGROUP_ROOT/low_session_1" \
        --memory-mb $PER_PROCESS_MB \
        --hold-seconds $HOLD_SECONDS \
        --name "LOW1" \
        --output "$exp_dir/low1_result.json" &
    local pid_low1=$!

    python3 "$SCRIPT_DIR/memory_stress.py" \
        --cgroup "$CGROUP_ROOT/low_session_2" \
        --memory-mb $PER_PROCESS_MB \
        --hold-seconds $HOLD_SECONDS \
        --name "LOW2" \
        --output "$exp_dir/low2_result.json" &
    local pid_low2=$!

    log_info "Waiting for processes to complete..."
    log_info "  HIGH PID: $pid_high"
    log_info "  LOW1 PID: $pid_low1"
    log_info "  LOW2 PID: $pid_low2"

    # 等待所有进程完成
    wait $pid_high
    local high_exit=$?
    wait $pid_low1
    local low1_exit=$?
    wait $pid_low2
    local low2_exit=$?

    local end_time=$(date +%s.%N)
    local total_time=$(echo "$end_time - $start_time" | bc)

    log_info "All processes completed in ${total_time}s"
    log_info "  HIGH exit: $high_exit"
    log_info "  LOW1 exit: $low1_exit"
    log_info "  LOW2 exit: $low2_exit"

    # 收集 memory.events
    for name in high_session low_session_1 low_session_2; do
        cat $CGROUP_ROOT/$name/memory.events > "$exp_dir/${name}_memory_events.txt" 2>/dev/null || true
    done

    # 收集 dmesg 中的 OOM 信息
    dmesg | grep -i "oom\|kill" | tail -20 > "$exp_dir/dmesg_oom.txt" 2>/dev/null || true

    cleanup_cgroups

    log_info "Baseline experiment completed!"
    log_info "Results saved to: $exp_dir"

    # 显示简要结果
    echo ""
    echo "========== BASELINE RESULTS =========="
    python3 "$SCRIPT_DIR/show_results.py" "$exp_dir" 2>/dev/null || \
        echo "Run 'python3 show_results.py $exp_dir' to see results"
}

run_bpf() {
    local exp_name="bpf_$(date +%Y%m%d_%H%M%S)"
    local exp_dir="$RESULTS_DIR/$exp_name"
    mkdir -p "$exp_dir"

    log_info "Running BPF experiment (with memcg BPF struct_ops)"
    log_info "Results will be saved to: $exp_dir"

    # Check for BPF loader
    local BPF_LOADER="$SCRIPT_DIR/../memcg_priority"
    if [ ! -x "$BPF_LOADER" ]; then
        log_error "BPF loader not found at $BPF_LOADER"
        log_error "Please build it first: cd .. && make"
        exit 1
    fi

    setup_cgroups

    # 记录实验参数
    cat > "$exp_dir/config.json" << EOF
{
    "experiment": "bpf",
    "total_memory_mb": $TOTAL_MEMORY_MB,
    "per_process_mb": $PER_PROCESS_MB,
    "memory_high_mb": $MEMORY_HIGH_MB,
    "hold_seconds": $HOLD_SECONDS,
    "bpf_enabled": true,
    "bpf_delay_ms": $BPF_DELAY_MS
}
EOF

    # 启动 BPF loader 在后台
    log_info "Starting BPF loader..."
    log_info "  HIGH session: high_mcg_ops (protected, below_low=true)"
    log_info "  LOW sessions: low_mcg_ops (delay=${BPF_DELAY_MS}ms)"

    "$BPF_LOADER" \
        --high "$CGROUP_ROOT/high_session" \
        --low "$CGROUP_ROOT/low_session_1" \
        --low "$CGROUP_ROOT/low_session_2" \
        --delay-ms $BPF_DELAY_MS \
        --threshold 1 \
        --below-low \
        --verbose \
        > "$exp_dir/bpf_loader.log" 2>&1 &
    local bpf_pid=$!

    # Wait a moment for BPF to attach
    sleep 2

    # Check if BPF loader is still running
    if ! kill -0 $bpf_pid 2>/dev/null; then
        log_error "BPF loader failed to start. Check $exp_dir/bpf_loader.log"
        cat "$exp_dir/bpf_loader.log"
        cleanup_cgroups
        exit 1
    fi

    log_info "BPF loader started (PID: $bpf_pid)"

    # 并发启动 3 个内存压力进程
    log_info "Starting 3 concurrent memory stress processes..."
    log_info "  HIGH: $PER_PROCESS_MB MB"
    log_info "  LOW1: $PER_PROCESS_MB MB"
    log_info "  LOW2: $PER_PROCESS_MB MB"
    log_info "  Total demand: $((PER_PROCESS_MB * 3)) MB"

    local start_time=$(date +%s.%N)

    python3 "$SCRIPT_DIR/memory_stress.py" \
        --cgroup "$CGROUP_ROOT/high_session" \
        --memory-mb $PER_PROCESS_MB \
        --hold-seconds $HOLD_SECONDS \
        --name "HIGH" \
        --output "$exp_dir/high_result.json" &
    local pid_high=$!

    python3 "$SCRIPT_DIR/memory_stress.py" \
        --cgroup "$CGROUP_ROOT/low_session_1" \
        --memory-mb $PER_PROCESS_MB \
        --hold-seconds $HOLD_SECONDS \
        --name "LOW1" \
        --output "$exp_dir/low1_result.json" &
    local pid_low1=$!

    python3 "$SCRIPT_DIR/memory_stress.py" \
        --cgroup "$CGROUP_ROOT/low_session_2" \
        --memory-mb $PER_PROCESS_MB \
        --hold-seconds $HOLD_SECONDS \
        --name "LOW2" \
        --output "$exp_dir/low2_result.json" &
    local pid_low2=$!

    log_info "Waiting for processes to complete..."
    log_info "  HIGH PID: $pid_high"
    log_info "  LOW1 PID: $pid_low1"
    log_info "  LOW2 PID: $pid_low2"

    # Wait for memory stress processes
    wait $pid_high
    local high_exit=$?
    wait $pid_low1
    local low1_exit=$?
    wait $pid_low2
    local low2_exit=$?

    local end_time=$(date +%s.%N)
    local total_time=$(echo "$end_time - $start_time" | bc)

    log_info "All processes completed in ${total_time}s"
    log_info "  HIGH exit: $high_exit"
    log_info "  LOW1 exit: $low1_exit"
    log_info "  LOW2 exit: $low2_exit"

    # Stop BPF loader
    log_info "Stopping BPF loader..."
    kill $bpf_pid 2>/dev/null
    wait $bpf_pid 2>/dev/null

    # 收集 memory.events
    for name in high_session low_session_1 low_session_2; do
        cat $CGROUP_ROOT/$name/memory.events > "$exp_dir/${name}_memory_events.txt" 2>/dev/null || true
    done

    # 收集 dmesg 中的 OOM 信息
    dmesg | grep -i "oom\|kill" | tail -20 > "$exp_dir/dmesg_oom.txt" 2>/dev/null || true

    cleanup_cgroups

    log_info "BPF experiment completed!"
    log_info "Results saved to: $exp_dir"

    echo ""
    echo "========== BPF RESULTS =========="
    python3 "$SCRIPT_DIR/show_results.py" "$exp_dir" 2>/dev/null || \
        echo "Run 'python3 show_results.py $exp_dir' to see results"
}

show_usage() {
    echo "Usage: $0 <baseline|bpf|clean>"
    echo ""
    echo "Commands:"
    echo "  baseline  - Run baseline experiment (no BPF)"
    echo "  bpf       - Run BPF experiment (with memcg struct_ops)"
    echo "  clean     - Clean up cgroups"
    echo ""
    echo "Example:"
    echo "  sudo $0 baseline"
    echo "  sudo keyctl session - $0 bpf"
}

# Main
case "${1:-}" in
    baseline)
        run_baseline
        ;;
    bpf)
        run_bpf
        ;;
    clean)
        cleanup_cgroups
        log_info "Cleanup completed"
        ;;
    *)
        show_usage
        exit 1
        ;;
esac
