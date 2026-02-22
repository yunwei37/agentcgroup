#!/bin/bash
#
# run_isolation_comparison.sh - 对比三种隔离策略的实验
#
# 策略:
#   1. no_isolation   - 仅设置总内存限制，无优先级
#   2. static         - 静态 memory.max 分配给每个 session
#   3. bpf            - 动态 BPF 优先级隔离
#
# 用法:
#   sudo ./run_isolation_comparison.sh [--total-mb 1024] [--speed 10] [--runs 3]
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CGROUP_ROOT="/sys/fs/cgroup/isolation_test"
RESULTS_DIR="$SCRIPT_DIR/isolation_results"
TRACES_DIR="$SCRIPT_DIR/../../experiments/all_images_haiku"

# 默认参数
TOTAL_MEMORY_MB=1024       # 总内存限制 1GB
SPEED_FACTOR=10            # 10倍速回放
NUM_RUNS=1                 # 每组运行次数
BPF_DELAY_MS=50            # BPF 延迟 (50ms - 足够防止 OOM 但不会完全冻结)
BASE_MEMORY_MB=100         # 模拟 Claude Code 进程的基线内存

# Traces 配置 (可修改)
# HIGH 优先级使用中等波动 trace (峰值 321MB)
HIGH_TRACE="dask__dask-11628"
# LOW 优先级使用较高峰值 traces，制造内存压力
LOW1_TRACE="sigmavirus24__github3.py-673"  # 峰值 306MB
LOW2_TRACE="sigmavirus24__github3.py-673"  # 峰值 306MB (同一个 trace，增加压力)

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_section() {
    echo ""
    echo -e "${BLUE}================================================================${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}================================================================${NC}"
    echo ""
}

cleanup_cgroups() {
    log_info "Cleaning up cgroups..."

    # 杀死 cgroup 中的进程
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

setup_cgroups_base() {
    log_info "Setting up base cgroups at $CGROUP_ROOT"

    cleanup_cgroups 2>/dev/null || true

    mkdir -p $CGROUP_ROOT
    echo "+memory" > $CGROUP_ROOT/cgroup.subtree_control 2>/dev/null || true
    echo "0" > $CGROUP_ROOT/memory.swap.max 2>/dev/null || true

    # 创建子 cgroups
    for name in high_session low_session_1 low_session_2; do
        mkdir -p $CGROUP_ROOT/$name
    done
}

# 策略 1: 无隔离 - 仅总内存限制
setup_no_isolation() {
    local total_mb=$1
    log_info "Setting up NO ISOLATION (total limit: ${total_mb}MB)"

    setup_cgroups_base

    # 设置父 cgroup 的总内存限制
    echo "${total_mb}M" > $CGROUP_ROOT/memory.max

    # 子 cgroup 无限制 (共享父限制)
    for name in high_session low_session_1 low_session_2; do
        echo "max" > $CGROUP_ROOT/$name/memory.max 2>/dev/null || true
        echo "max" > $CGROUP_ROOT/$name/memory.high 2>/dev/null || true
    done

    log_info "  Parent memory.max: ${total_mb}MB"
    log_info "  All sessions: unlimited (fair sharing)"
}

# 策略 2: 静态隔离 - 固定 memory.max
setup_static_isolation() {
    local total_mb=$1
    local per_session_mb=$((total_mb / 3))
    log_info "Setting up STATIC ISOLATION (${per_session_mb}MB per session)"

    setup_cgroups_base

    # 设置父 cgroup 的总内存限制
    echo "${total_mb}M" > $CGROUP_ROOT/memory.max

    # 每个子 cgroup 固定限制
    for name in high_session low_session_1 low_session_2; do
        echo "${per_session_mb}M" > $CGROUP_ROOT/$name/memory.max
        log_info "  $name: memory.max=${per_session_mb}MB"
    done
}

# 策略 3: BPF 动态隔离
setup_bpf_isolation() {
    local total_mb=$1
    # HIGH 可以 burst，设置 memory.high = max
    local high_session_threshold="max"
    # LOW 设置为峰值使用量 (~400MB)，只在峰值时触发 BPF 延迟
    # 这样 LOW 平时正常运行，只在达到峰值时被 BPF 延迟
    local low_session_threshold=400  # LOW peak ~406MB, 稍低于峰值触发延迟

    log_info "Setting up BPF DYNAMIC ISOLATION"

    setup_cgroups_base

    # 设置父 cgroup 的总内存限制
    echo "${total_mb}M" > $CGROUP_ROOT/memory.max

    # HIGH session: 无限制，允许 burst
    echo "max" > $CGROUP_ROOT/high_session/memory.max 2>/dev/null || true
    echo "max" > $CGROUP_ROOT/high_session/memory.high 2>/dev/null || true
    log_info "  high_session: memory.high=max (no BPF delay)"

    # LOW sessions: 设置 memory.high 略低于峰值，峰值时触发 BPF 延迟
    for name in low_session_1 low_session_2; do
        echo "max" > $CGROUP_ROOT/$name/memory.max 2>/dev/null || true
        echo "${low_session_threshold}M" > $CGROUP_ROOT/$name/memory.high
        log_info "  $name: memory.high=${low_session_threshold}MB (BPF delay at peak)"
    done
}

run_workloads() {
    local exp_dir=$1
    local strategy=$2

    log_info "Starting workloads..."

    local high_trace_path="$TRACES_DIR/$HIGH_TRACE/attempt_1/resources.json"
    local low1_trace_path="$TRACES_DIR/$LOW1_TRACE/attempt_1/resources.json"
    local low2_trace_path="$TRACES_DIR/$LOW2_TRACE/attempt_1/resources.json"

    # 检查 trace 文件
    for trace in "$high_trace_path" "$low1_trace_path" "$low2_trace_path"; do
        if [ ! -f "$trace" ]; then
            log_error "Trace file not found: $trace"
            return 1
        fi
    done

    local start_time=$(date +%s.%N)

    # 启动 HIGH 优先级工作负载
    python3 "$SCRIPT_DIR/trace_replay.py" "$high_trace_path" \
        --cgroup "$CGROUP_ROOT/high_session" \
        --speed $SPEED_FACTOR \
        --base-memory-mb $BASE_MEMORY_MB \
        --name "HIGH" \
        --output "$exp_dir/high_result.json" &
    local pid_high=$!

    # 启动 LOW 优先级工作负载
    python3 "$SCRIPT_DIR/trace_replay.py" "$low1_trace_path" \
        --cgroup "$CGROUP_ROOT/low_session_1" \
        --speed $SPEED_FACTOR \
        --base-memory-mb $BASE_MEMORY_MB \
        --name "LOW1" \
        --output "$exp_dir/low1_result.json" &
    local pid_low1=$!

    python3 "$SCRIPT_DIR/trace_replay.py" "$low2_trace_path" \
        --cgroup "$CGROUP_ROOT/low_session_2" \
        --speed $SPEED_FACTOR \
        --base-memory-mb $BASE_MEMORY_MB \
        --name "LOW2" \
        --output "$exp_dir/low2_result.json" &
    local pid_low2=$!

    log_info "  HIGH PID: $pid_high (trace: $HIGH_TRACE)"
    log_info "  LOW1 PID: $pid_low1 (trace: $LOW1_TRACE)"
    log_info "  LOW2 PID: $pid_low2 (trace: $LOW2_TRACE)"

    # 等待完成
    wait $pid_high 2>/dev/null
    local high_exit=$?
    wait $pid_low1 2>/dev/null
    local low1_exit=$?
    wait $pid_low2 2>/dev/null
    local low2_exit=$?

    local end_time=$(date +%s.%N)
    local total_time=$(echo "$end_time - $start_time" | bc)

    log_info "All processes completed in ${total_time}s"
    log_info "  HIGH exit: $high_exit, LOW1 exit: $low1_exit, LOW2 exit: $low2_exit"

    # 收集 memory.events
    for name in high_session low_session_1 low_session_2; do
        cat $CGROUP_ROOT/$name/memory.events > "$exp_dir/${name}_memory_events.txt" 2>/dev/null || true
    done

    # 收集 dmesg OOM 信息
    dmesg | grep -i "oom\|kill" | tail -20 > "$exp_dir/dmesg_oom.txt" 2>/dev/null || true

    # 记录总时间
    echo "$total_time" > "$exp_dir/total_time.txt"

    return 0
}

run_experiment() {
    local strategy=$1
    local run_num=$2
    local exp_name="${strategy}_run${run_num}_$(date +%Y%m%d_%H%M%S)"
    local exp_dir="$RESULTS_DIR/$exp_name"
    mkdir -p "$exp_dir"

    log_section "Running: $strategy (Run $run_num)"
    log_info "Results will be saved to: $exp_dir"

    # 记录配置
    cat > "$exp_dir/config.json" << EOF
{
    "strategy": "$strategy",
    "run": $run_num,
    "total_memory_mb": $TOTAL_MEMORY_MB,
    "speed_factor": $SPEED_FACTOR,
    "base_memory_mb": $BASE_MEMORY_MB,
    "bpf_delay_ms": $BPF_DELAY_MS,
    "high_trace": "$HIGH_TRACE",
    "low1_trace": "$LOW1_TRACE",
    "low2_trace": "$LOW2_TRACE"
}
EOF

    local bpf_pid=""

    case $strategy in
        no_isolation)
            setup_no_isolation $TOTAL_MEMORY_MB
            ;;
        static)
            setup_static_isolation $TOTAL_MEMORY_MB
            ;;
        bpf)
            setup_bpf_isolation $TOTAL_MEMORY_MB

            # 启动 BPF loader
            local BPF_LOADER="$SCRIPT_DIR/bpf_loader/memcg_priority"
            if [ ! -x "$BPF_LOADER" ]; then
                log_error "BPF loader not found at $BPF_LOADER"
                log_error "Please build it first: cd bpf_loader && make"
                cleanup_cgroups
                return 1
            fi

            log_info "Starting BPF loader..."
            "$BPF_LOADER" \
                --high "$CGROUP_ROOT/high_session" \
                --low "$CGROUP_ROOT/low_session_1" \
                --low "$CGROUP_ROOT/low_session_2" \
                --delay-ms $BPF_DELAY_MS \
                --threshold 10000 \
                --below-low \
                --verbose \
                > "$exp_dir/bpf_loader.log" 2>&1 &
            bpf_pid=$!

            sleep 2

            if ! kill -0 $bpf_pid 2>/dev/null; then
                log_error "BPF loader failed to start"
                cat "$exp_dir/bpf_loader.log"
                cleanup_cgroups
                return 1
            fi

            log_info "BPF loader started (PID: $bpf_pid)"
            ;;
        *)
            log_error "Unknown strategy: $strategy"
            return 1
            ;;
    esac

    # 运行工作负载
    run_workloads "$exp_dir" "$strategy"
    local workload_exit=$?

    # 停止 BPF loader
    if [ -n "$bpf_pid" ]; then
        log_info "Stopping BPF loader..."
        kill $bpf_pid 2>/dev/null || true
        wait $bpf_pid 2>/dev/null || true
    fi

    cleanup_cgroups

    if [ $workload_exit -ne 0 ]; then
        log_error "Workloads failed"
        return 1
    fi

    log_info "Experiment completed: $exp_dir"
    return 0
}

analyze_results() {
    log_section "Analyzing Results"

    python3 "$SCRIPT_DIR/analyze_isolation_results.py" "$RESULTS_DIR"
}

print_usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Compare three memory isolation strategies:"
    echo "  1. no_isolation - Total memory limit only, fair sharing"
    echo "  2. static       - Fixed memory.max per session"
    echo "  3. bpf          - Dynamic BPF priority isolation"
    echo ""
    echo "Options:"
    echo "  --total-mb MB     Total memory limit (default: $TOTAL_MEMORY_MB)"
    echo "  --speed FACTOR    Replay speed factor (default: $SPEED_FACTOR)"
    echo "  --runs N          Number of runs per strategy (default: $NUM_RUNS)"
    echo "  --base-mb MB      Base memory per process (default: $BASE_MEMORY_MB)"
    echo "  --strategy STR    Run only specified strategy (no_isolation|static|bpf)"
    echo "  --analyze-only    Only analyze existing results"
    echo "  --help            Show this help"
    echo ""
    echo "Example:"
    echo "  sudo keyctl session - $0 --total-mb 1024 --speed 10 --runs 3"
}

# 解析参数
ONLY_STRATEGY=""
ANALYZE_ONLY=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --total-mb)
            TOTAL_MEMORY_MB="$2"
            shift 2
            ;;
        --speed)
            SPEED_FACTOR="$2"
            shift 2
            ;;
        --runs)
            NUM_RUNS="$2"
            shift 2
            ;;
        --base-mb)
            BASE_MEMORY_MB="$2"
            shift 2
            ;;
        --strategy)
            ONLY_STRATEGY="$2"
            shift 2
            ;;
        --analyze-only)
            ANALYZE_ONLY=true
            shift
            ;;
        --help)
            print_usage
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            print_usage
            exit 1
            ;;
    esac
done

# 主程序
main() {
    log_section "Memory Isolation Comparison Experiment"

    log_info "Configuration:"
    log_info "  Total memory limit: ${TOTAL_MEMORY_MB}MB"
    log_info "  Speed factor: ${SPEED_FACTOR}x"
    log_info "  Number of runs: $NUM_RUNS"
    log_info "  Base memory per process: ${BASE_MEMORY_MB}MB"
    log_info "  HIGH trace: $HIGH_TRACE"
    log_info "  LOW traces: $LOW1_TRACE, $LOW2_TRACE"

    if [ "$ANALYZE_ONLY" = true ]; then
        analyze_results
        exit 0
    fi

    mkdir -p "$RESULTS_DIR"

    # 确定要运行的策略
    if [ -n "$ONLY_STRATEGY" ]; then
        strategies=("$ONLY_STRATEGY")
    else
        strategies=("no_isolation" "static" "bpf")
    fi

    # 运行实验
    for strategy in "${strategies[@]}"; do
        for run in $(seq 1 $NUM_RUNS); do
            run_experiment "$strategy" "$run"
            sleep 2  # 让系统恢复
        done
    done

    # 分析结果
    analyze_results

    log_section "Experiment Complete"
    log_info "All results saved to: $RESULTS_DIR"
}

# 检查是否 root
if [ "$EUID" -ne 0 ]; then
    log_error "This script must be run as root"
    log_info "Try: sudo keyctl session - $0 $*"
    exit 1
fi

main
