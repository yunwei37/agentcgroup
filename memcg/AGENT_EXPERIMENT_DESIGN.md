# memcg BPF struct_ops 对 Agent 工作负载的效果验证实验设计

## 1. 研究问题

**RQ**: memcg BPF struct_ops 能否有效解决 agent 工作负载的内存资源竞争问题？

具体子问题：
- **RQ1**: 在多租户并发场景下，能否保护高优先级 agent 的任务完成时间？
- **RQ2**: 相比静态内存限制，动态 BPF 控制能否减少 OOM 事件？
- **RQ3**: 能否提高整体资源利用率？

## 2. 实验方法论

### 2.1 核心思路：Trace Replay

使用已收集的真实 agent trace 数据驱动内存分配模式，在受控的多租户环境中验证 memcg BPF struct_ops 的效果。

```
┌─────────────────────────────────────────────────────────────────┐
│                    Agent Trace Replay Framework                  │
├─────────────────────────────────────────────────────────────────┤
│  原始 Trace (resources.json + tool_calls.json)                   │
│       ↓                                                          │
│  Trace Synthesizer: 解析内存使用时序                             │
│       ↓                                                          │
│  Memory Workload Generator: 按 trace 模式分配/释放内存           │
│       ↓                                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │ Session A   │  │ Session B   │  │ Session C   │  (并发)      │
│  │ (HIGH prio) │  │ (LOW prio)  │  │ (LOW prio)  │              │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘              │
│         └────────────────┼────────────────┘                      │
│                          ↓                                        │
│              Total Memory Limit (e.g., 2GB)                       │
│                          ↓                                        │
│         memcg BPF struct_ops (get_high_delay_ms, below_low)      │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Trace 数据来源

使用 SWE-rebench 18 任务数据集中的 trace：

| Trace | 峰值内存 | 平均内存 | 特点 |
|-------|---------|---------|------|
| Medical_Bio_Hard | ~4GB | ~264MB | 高峰值、高突发 |
| ML_Scientific_Hard | ~2GB | ~300MB | 中等峰值 |
| CLI_Tools_Easy | ~200MB | ~150MB | 低内存需求 |

### 2.3 实验配置

| 参数 | 设置 | 说明 |
|------|------|------|
| 并发 sessions | 3 | 1 HIGH + 2 LOW |
| 总内存限制 | 2GB | 强制产生竞争 |
| HIGH priority sessions | 1 | 需要保护的 agent |
| LOW priority sessions | 2 | 背景负载/竞争者 |
| Trace 回放速度 | 10x | 加速实验 |
| BPF delay 设置 | 2000ms | `over_high_ms` |

## 3. 实验组设计

### 3.1 对照组

| 组别 | 配置 | 说明 |
|------|------|------|
| **Baseline-Static** | 静态 memory.max = 666MB/session | 传统静态分配（按峰值/3） |
| **Baseline-NoLimit** | 共享 2GB，无隔离 | 完全共享，无保护 |
| **Baseline-HighOnly** | 仅 memory.high + 用户态监控 | 用户态控制对照 |

### 3.2 实验组

| 组别 | 配置 | 说明 |
|------|------|------|
| **AgentCgroup-BPF** | memcg_bpf_ops + 动态 delay | 本文方案 |

BPF 策略配置：
- HIGH session: 附加 `high_mcg_ops`，`below_low` 返回 true（受保护）
- LOW sessions: 附加 `low_mcg_ops`，`get_high_delay_ms` 返回 2000ms（被限流）

## 4. 测量指标

### 4.1 主要指标

| 指标 | 测量方法 | 意义 |
|------|---------|------|
| 完成时间 | 从 trace 开始到结束的墙钟时间 | 任务效率 |
| OOM 事件数 | 捕获 MemoryError / dmesg oom-kill | 稳定性 |
| p99 完成时间 | 多次运行的 99 分位 | 尾延迟 |
| 内存利用率 | 实际使用 / 限制 | 资源效率 |

### 4.2 辅助指标

| 指标 | 测量方法 |
|------|---------|
| BPF delay 触发次数 | BPF map 计数器 |
| memory.events.high 计数 | cgroup 文件 |
| 峰值内存时刻 | 时序采样 |

## 5. 实现组件

### 5.1 Trace Replay 工具

```python
#!/usr/bin/env python3
"""
trace_replay.py - 基于真实 agent trace 生成内存压力工作负载
"""
import json
import time
import sys
import os

def parse_mem_usage(mem_str):
    """解析 '147.9MB / 16.19GB' 格式"""
    used = mem_str.split('/')[0].strip()
    if 'GB' in used:
        return float(used.replace('GB', '')) * 1024 * 1024 * 1024
    elif 'MB' in used:
        return float(used.replace('MB', '')) * 1024 * 1024
    elif 'kB' in used:
        return float(used.replace('kB', '')) * 1024
    return 0

def load_trace(trace_path):
    """加载 resources.json"""
    with open(trace_path) as f:
        data = json.load(f)

    samples = []
    for s in data['samples']:
        mem_bytes = parse_mem_usage(s['mem_usage'])
        samples.append({
            'epoch': s['epoch'],
            'mem_bytes': int(mem_bytes),
            'cpu_percent': float(s['cpu_percent'])
        })
    return samples

def replay_memory_trace(samples, speed_factor=1.0):
    """
    按 trace 时序分配/释放内存
    返回: (completion_time, peak_memory, oom_count)
    """
    allocated = []
    start_time = time.time()
    trace_start = samples[0]['epoch']
    peak_mem = 0
    oom_count = 0

    for i, sample in enumerate(samples):
        # 计算应该等待的时间
        trace_elapsed = sample['epoch'] - trace_start
        real_elapsed = time.time() - start_time
        wait_time = (trace_elapsed / speed_factor) - real_elapsed
        if wait_time > 0:
            time.sleep(wait_time)

        target_mem = sample['mem_bytes']
        current_mem = sum(len(buf) for buf in allocated)

        try:
            if target_mem > current_mem:
                # 需要分配更多内存
                alloc_size = target_mem - current_mem
                buf = bytearray(alloc_size)
                # 触发实际分配 (touch pages)
                for j in range(0, alloc_size, 4096):
                    buf[j] = 1
                allocated.append(buf)
            elif target_mem < current_mem:
                # 释放内存
                while allocated and sum(len(b) for b in allocated) > target_mem:
                    allocated.pop()
        except MemoryError:
            oom_count += 1
            print(f"[OOM] at sample {i}, target={target_mem/1e6:.1f}MB",
                  file=sys.stderr)

        current_mem = sum(len(buf) for buf in allocated)
        peak_mem = max(peak_mem, current_mem)

        # 进度报告
        if i % 50 == 0:
            print(f"[{i}/{len(samples)}] mem={current_mem/1e6:.1f}MB",
                  file=sys.stderr)

    completion_time = time.time() - start_time
    return completion_time, peak_mem, oom_count

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <trace_path> [speed_factor]")
        sys.exit(1)

    trace_path = sys.argv[1]
    speed_factor = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0

    samples = load_trace(trace_path)
    print(f"Loaded {len(samples)} samples from {trace_path}")
    print(f"Speed factor: {speed_factor}x")

    comp_time, peak, ooms = replay_memory_trace(samples, speed_factor)

    # 输出结果 (JSON 格式便于后续分析)
    result = {
        "trace": trace_path,
        "speed_factor": speed_factor,
        "completion_time_sec": comp_time,
        "peak_memory_bytes": peak,
        "oom_count": ooms
    }
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()
```

### 5.2 实验运行脚本

```bash
#!/bin/bash
# run_memcg_experiment.sh - 运行多租户内存竞争实验

set -e

CGROUP_ROOT="/sys/fs/cgroup/agent_exp"
TOTAL_MEM="2147483648"  # 2GB in bytes
TRACE_DIR="/home/yunwei37/agentcgroup/experiments/batch_swebench_18tasks"
RESULTS_DIR="./experiment_results"
SPEED_FACTOR=10

# 高内存需求 trace
HIGH_TRACE="$TRACE_DIR/Medical_Bio_Hard/attempt_1/resources.json"
LOW_TRACE="$TRACE_DIR/Medical_Bio_Hard/attempt_1/resources.json"

setup_cgroups() {
    echo "Setting up cgroups..."
    sudo mkdir -p $CGROUP_ROOT
    echo "+memory" | sudo tee $CGROUP_ROOT/cgroup.subtree_control > /dev/null

    # 设置总内存限制
    echo $TOTAL_MEM | sudo tee $CGROUP_ROOT/memory.max > /dev/null
    echo 0 | sudo tee $CGROUP_ROOT/memory.swap.max > /dev/null

    # 创建 session cgroups
    sudo mkdir -p $CGROUP_ROOT/high_session
    sudo mkdir -p $CGROUP_ROOT/low_session_1
    sudo mkdir -p $CGROUP_ROOT/low_session_2

    # 启用子 cgroup 的 memory controller
    for dir in high_session low_session_1 low_session_2; do
        echo "+memory" | sudo tee $CGROUP_ROOT/$dir/cgroup.subtree_control > /dev/null 2>&1 || true
    done
}

cleanup_cgroups() {
    echo "Cleaning up cgroups..."
    for dir in high_session low_session_1 low_session_2; do
        sudo rmdir $CGROUP_ROOT/$dir 2>/dev/null || true
    done
    sudo rmdir $CGROUP_ROOT 2>/dev/null || true
}

run_baseline_static() {
    echo "=== Running Baseline-Static ==="
    local exp_dir="$RESULTS_DIR/baseline_static"
    mkdir -p $exp_dir

    # 静态分配: 每个 session 666MB
    local per_session=$((TOTAL_MEM / 3))

    for dir in high_session low_session_1 low_session_2; do
        echo $per_session | sudo tee $CGROUP_ROOT/$dir/memory.max > /dev/null
    done

    run_workloads $exp_dir
}

run_baseline_nolimit() {
    echo "=== Running Baseline-NoLimit ==="
    local exp_dir="$RESULTS_DIR/baseline_nolimit"
    mkdir -p $exp_dir

    # 无限制 (使用 max 表示无限)
    for dir in high_session low_session_1 low_session_2; do
        echo "max" | sudo tee $CGROUP_ROOT/$dir/memory.max > /dev/null
    done

    run_workloads $exp_dir
}

run_agentcgroup_bpf() {
    echo "=== Running AgentCgroup-BPF ==="
    local exp_dir="$RESULTS_DIR/agentcgroup_bpf"
    mkdir -p $exp_dir

    # 设置较高的软限制
    for dir in high_session low_session_1 low_session_2; do
        echo "max" | sudo tee $CGROUP_ROOT/$dir/memory.max > /dev/null
    done

    # 附加 BPF struct_ops
    # HIGH session: high_mcg_ops (below_low protection)
    # LOW sessions: low_mcg_ops (get_high_delay_ms throttling)

    echo "Attaching BPF struct_ops..."
    sudo keyctl session - python3 attach_memcg_bpf.py \
        --high-cgroup $CGROUP_ROOT/high_session \
        --low-cgroups $CGROUP_ROOT/low_session_1,$CGROUP_ROOT/low_session_2 \
        &
    BPF_PID=$!
    sleep 2  # 等待 BPF 附加完成

    run_workloads $exp_dir

    # 清理 BPF
    sudo kill $BPF_PID 2>/dev/null || true
}

run_workloads() {
    local exp_dir=$1
    local pids=()

    echo "Starting workloads..."

    # HIGH priority session
    (
        echo $BASHPID | sudo tee $CGROUP_ROOT/high_session/cgroup.procs > /dev/null
        python3 trace_replay.py $HIGH_TRACE $SPEED_FACTOR \
            > $exp_dir/high_session.json 2> $exp_dir/high_session.log
    ) &
    pids+=($!)

    # LOW priority sessions
    for i in 1 2; do
        (
            echo $BASHPID | sudo tee $CGROUP_ROOT/low_session_$i/cgroup.procs > /dev/null
            python3 trace_replay.py $LOW_TRACE $SPEED_FACTOR \
                > $exp_dir/low_session_$i.json 2> $exp_dir/low_session_$i.log
        ) &
        pids+=($!)
    done

    # 等待所有工作负载完成
    echo "Waiting for workloads to complete..."
    for pid in "${pids[@]}"; do
        wait $pid || true
    done

    # 收集 memory.events
    for dir in high_session low_session_1 low_session_2; do
        cat $CGROUP_ROOT/$dir/memory.events > $exp_dir/${dir}_memory_events.txt 2>/dev/null || true
    done

    echo "Results saved to $exp_dir"
}

main() {
    mkdir -p $RESULTS_DIR

    trap cleanup_cgroups EXIT

    setup_cgroups

    # 运行三组实验
    run_baseline_static
    sleep 5

    run_baseline_nolimit
    sleep 5

    run_agentcgroup_bpf

    echo "=== All experiments completed ==="
    echo "Results in $RESULTS_DIR"
}

main "$@"
```

### 5.3 BPF Attach 工具

```python
#!/usr/bin/env python3
"""
attach_memcg_bpf.py - 附加 memcg BPF struct_ops 到指定 cgroups
"""
import argparse
import os
import sys
import time
import signal

# 需要使用编译好的 test_progs 或自定义加载器
# 这里提供简化版本的逻辑框架

def get_cgroup_id(cgroup_path):
    """获取 cgroup 的 ID"""
    import ctypes
    # 使用 name_to_handle_at 获取 cgroup id
    # 简化: 直接读取 cgroup.stat 中的信息或使用 inode
    stat = os.stat(cgroup_path)
    return stat.st_ino

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--high-cgroup', required=True,
                        help='Path to HIGH priority cgroup')
    parser.add_argument('--low-cgroups', required=True,
                        help='Comma-separated paths to LOW priority cgroups')
    parser.add_argument('--delay-ms', type=int, default=2000,
                        help='Delay for LOW priority cgroups (ms)')
    args = parser.parse_args()

    high_cgroup = args.high_cgroup
    low_cgroups = args.low_cgroups.split(',')

    print(f"HIGH cgroup: {high_cgroup} (id={get_cgroup_id(high_cgroup)})")
    for lc in low_cgroups:
        print(f"LOW cgroup: {lc} (id={get_cgroup_id(lc)})")

    # TODO: 实际的 BPF 加载和附加逻辑
    # 需要:
    # 1. 加载 memcg_ops.bpf.o
    # 2. 设置 local_config (threshold, high_cgroup_id, over_high_ms)
    # 3. 附加 high_mcg_ops 到 high_cgroup
    # 4. 附加 low_mcg_ops 到每个 low_cgroup

    print("BPF struct_ops attached (placeholder)")
    print(f"Delay for LOW cgroups: {args.delay_ms}ms")

    # 保持运行直到收到信号
    def signal_handler(sig, frame):
        print("\nDetaching BPF struct_ops...")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while True:
        time.sleep(1)

if __name__ == '__main__':
    main()
```

## 6. 预期结果

### 6.1 定量预期

| 指标 | Baseline-Static | Baseline-NoLimit | AgentCgroup-BPF |
|------|-----------------|------------------|-----------------|
| HIGH 完成时间 | 基准 (T) | 1.5-2.0T (竞争) | ~1.0T (受保护) |
| LOW 完成时间 | T | 1.5-2.0T | 1.5-2.5T (被限流) |
| OOM 事件数 | 5-10 | 0-2 | 0-1 |
| 总吞吐量 | 低 | 中 | 高 |
| p99 延迟 (HIGH) | 高方差 | 极高 | 稳定 |

### 6.2 预期图表

#### Figure 1: 完成时间对比 (柱状图)

```
Completion Time (normalized to single-run baseline)

2.5 │
    │              ████
2.0 │              ████  ████      ████
    │        ████  ████  ████      ████
1.5 │        ████  ████  ████      ████
    │  ████  ████  ████  ████      ████
1.0 │  ████  ████  ████  ████  ████████
    │  ████  ████  ████  ████  ████████
0.5 │  ████  ████  ████  ████  ████████
    │  ████  ████  ████  ████  ████████
0.0 └──────────────────────────────────
       Static   NoLimit    BPF

    ████ HIGH session
    ████ LOW session (avg)
```

**预期结论**: AgentCgroup-BPF 的 HIGH session 完成时间接近基准，而 LOW sessions 被适当限流。

#### Figure 2: 内存使用时序 (折线图)

```
Memory Usage (MB)
     │
2000 ├─────────────────────────────── Total Limit
     │         ╱╲    ╱╲
1500 │   HIGH ╱  ╲  ╱  ╲────────────── HIGH 受保护，可 burst
     │       ╱    ╲╱    ╲
1000 │      ╱              ╲
     │     ╱                ╲
 500 │ LOW ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓────────── LOW 被限流
     │
   0 └──────────────────────────────────────
     0      100     200     300     400 (seconds)
```

#### Figure 3: OOM 事件对比 (柱状图)

```
OOM Events Count

 15 │ ████
    │ ████
 10 │ ████
    │ ████
  5 │ ████       ████
    │ ████       ████
  0 │ ████       ████        █
    └────────────────────────────
      Static   NoLimit    BPF
```

#### Figure 4: BPF Delay 触发统计

```
get_high_delay_ms() Triggers

     │                        ████████
 200 │                        ████████
     │                        ████████
 150 │                        ████████
     │                        ████████
 100 │                        ████████
     │                        ████████
  50 │                        ████████
     │                        ████████
   0 │   N/A         N/A      ████████
     └────────────────────────────────
       Static    NoLimit      BPF
```

### 6.3 统计分析

运行每组实验 5 次，计算：
- 平均值和标准差
- 95% 置信区间
- Mann-Whitney U 检验（非参数检验）

## 7. 科学意义

### 7.1 验证的核心假设

| 假设 | 验证方法 |
|------|---------|
| **H1**: Domain Alignment 有效 | HIGH session 完成时间在 BPF 组接近基准 |
| **H2**: Timescale Mismatch 可解决 | BPF delay 响应时间 vs 用户态监控延迟 |
| **H3**: 优先级隔离有效 | OOM 事件主要发生在 LOW sessions |

### 7.2 与论文 Characterization 的对接

| Characterization 发现 | 实验验证 |
|----------------------|---------|
| 内存峰值达 4GB，平均仅 264MB (15.4x 过度供给) | 使用 Medical_Bio_Hard trace 重现突发模式 |
| 资源突发在秒级发生 | 测量 BPF delay 的实际响应延迟 |
| 静态限制浪费 76-93% 资源 | 对比 BPF 动态控制的实际利用率 |
| CPU/Memory 强正相关 (91-95%) | 记录并分析时序相关性 |

### 7.3 论文贡献点

1. **首次将 memcg BPF struct_ops 应用于 AI agent 工作负载**
2. **基于真实 agent trace 的实验验证**（非合成负载）
3. **定量证明内核级控制相比用户态的优势**

## 8. 实现路径

### Week 1: Trace Replay 框架
- [ ] 实现 trace_replay.py
- [ ] 验证单 session 回放正确性
- [ ] 测试内存分配/释放时序

### Week 2: 多租户实验
- [ ] 实现 cgroup 管理脚本
- [ ] 实现 BPF attach 工具（基于 test_progs）
- [ ] 运行 3 组对比实验

### Week 3: 结果分析
- [ ] 收集完成时间、OOM、内存利用率数据
- [ ] 生成论文图表
- [ ] 撰写实验结果章节

## 9. 风险与备选方案

| 风险 | 影响 | 备选方案 |
|------|------|---------|
| memcg_bpf_ops 不稳定 | 实验无法完成 | 使用 memory.high + PSI + 用户态控制作为对照 |
| Trace 回放不准确 | 结果不可信 | 增加真实 agent 运行实验 |
| 内存竞争不充分 | 效果不明显 | 减少总内存限制或增加并发数 |

## 10. 参考资料

- memcg BPF struct_ops RFC: https://lore.kernel.org/all/cover.1738292406.git.teawater@antgroup.com/
- cgroup v2 文档: https://docs.kernel.org/admin-guide/cgroup-v2.html
- SWE-rebench 数据集: experiments/batch_swebench_18tasks/
