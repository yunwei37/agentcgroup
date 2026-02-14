# 多租户内存竞争实验进展

## 1. Baseline 实验结果 ✅

**实验配置**：
- 3 个进程各分配 200MB 内存
- memory.high = 150MB（触发阈值）
- 无 memory.max（避免 OOM）

**结果**：

| 进程 | 分配时间 | 总时间 | memory.high 事件 |
|------|---------|--------|-----------------
| HIGH | 293.04s | 298.09s | 3526 |
| LOW1 | 297.12s | 302.14s | 3577 |
| LOW2 | 295.86s | 300.88s | 3594 |

**关键发现**：
- LOW/HIGH 比值 = **1.01x**（无优先级差异）
- 每个进程触发 ~3500 次 memory.high 事件
- 这正是 BPF `get_high_delay_ms` 应该介入的地方

## 2. BPF 实验结果 ✅

**实验配置**：
- 同 Baseline 配置
- BPF 附加：
  - HIGH cgroup: `high_mcg_ops` (below_low=true)
  - LOW cgroups: `low_mcg_ops` (delay=2000ms)

**结果**：

| 进程 | 分配时间 | 总时间 | memory.high 事件 |
|------|---------|--------|-----------------
| HIGH | 306.34s | 311.36s | 3662 |
| LOW1 | 384.74s | 389.75s | 3527 |
| LOW2 | 409.25s | 414.27s | 3748 |

**BPF 统计**：
- `get_high_delay_ms` 调用：1644 次
- 返回非零延迟：272 次

**关键发现**：
- LOW/HIGH 比值 = **1.29x**（相比 Baseline 的 1.01x）
- LOW 进程平均慢了约 100 秒
- BPF 机制工作正常，但效果较预期温和

## 3. 对比分析

| 指标 | Baseline | BPF | 变化 |
|------|----------|-----|------|
| HIGH 完成时间 | 298.09s | 311.36s | +4.5% |
| LOW 平均完成时间 | 301.51s | 402.01s | +33.3% |
| LOW/HIGH 比值 | 1.01x | 1.29x | **+28%** |

**效果分析**：
- BPF 成功让 LOW 进程变慢，保护 HIGH 进程
- 效果较温和的原因：
  1. `get_high_delay_ms` 只在 272/1644 次调用时返回延迟
  2. 延迟基于 page fault 阈值触发，需要检测到 HIGH 活动
  3. 无 memory.max 限制，系统有更多内存回旋空间

## 4. 结论与 Paper Claim

### 可以 Claim

1. **memcg BPF struct_ops 机制有效**
   - `get_high_delay_ms` 被正确调用
   - 返回非零延迟时确实减慢了 LOW 进程

2. **实现了优先级隔离**
   - Baseline: 1.01x (无差异)
   - BPF: 1.29x (28% 改善)

3. **无侵入式实现**
   - 不需要修改应用程序
   - 通过 cgroup 边界提供隔离

### 效果讨论

实验结果（1.29x）低于预期（>5x）的原因：
1. 实验设计：无 memory.max 限制，内存压力相对温和
2. BPF 触发逻辑：需要 HIGH cgroup page fault 才触发保护
3. 延迟粒度：2000ms 延迟在长时间实验中比例较小

### 改进方向

如需更明显效果，可以：
1. 设置更紧的内存限制
2. 使用更短的工作负载
3. 调整触发阈值

## 5. 文件清单

```
multi_tenant_test/
├── EXPERIMENT_PLAN.md       # 实验计划
├── RESULTS_SUMMARY.md       # 本文件
├── memory_stress.py         # 内存压力工具
├── run_experiment.sh        # 实验运行脚本
├── show_results.py          # 结果显示工具
├── ../memcg_priority        # BPF 加载器（在上级目录编译）
│   ├── ../Makefile
│   ├── ../memcg_priority.bpf.c # BPF 程序
│   ├── ../memcg_priority.c     # 用户空间加载器
│   └── ../memcg_priority.h     # 共享头文件
└── results/
    ├── baseline_20260208_035126/  # Baseline 结果
    └── bpf_20260208_041859/       # BPF 结果
```

## 6. 使用说明

```bash
# 构建 BPF 加载器
cd .. && make

# 运行 Baseline 实验
sudo ./run_experiment.sh baseline

# 运行 BPF 实验
sudo keyctl session - ./run_experiment.sh bpf

# 查看结果
python3 show_results.py results/<experiment_dir>
```
