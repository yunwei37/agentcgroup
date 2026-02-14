# scx_flatcg 权重调度失效问题分析

## 问题现象

在运行 `test_weight_fairness.sh` 测试时，发现 CPU 分配没有按照 cgroup 权重比例进行：

**预期结果**（权重 100:200:300）：
- cg_w100: 16.67%
- cg_w200: 33.33%
- cg_w300: 50.00%

**实际结果**：
- cg_w100: ~33%
- cg_w200: ~33%
- cg_w300: ~33%

三个 cgroup 获得了几乎相等的 CPU 时间，权重完全被忽略。

## 问题分析

### 1. 初步诊断

查看 scx_flatcg 的日志输出：

```
[SEQ      1 cpu=100.0 hweight_gen=86]
       act:    40  deact:    43 global:   649 local:    18
```

关键指标：
- `global: 649` - 被路由到全局 fallback 队列的任务数
- `local: 18` - 被路由到 cgroup 专属队列的任务数

比例约为 36:1，大量任务进入了全局队列而不是 cgroup 队列。

### 2. 源码分析

在 `scx_flatcg.bpf.c` 的 `fcg_enqueue` 函数中：

```c
if (p->nr_cpus_allowed != nr_cpus) {
    stat_inc(FCG_STAT_GLOBAL);
    scx_bpf_dsq_insert(p, FALLBACK_DSQ, SCX_SLICE_DFL, enq_flags);
    return;
}
```

这段代码的意图是：如果任务有自定义 CPU 亲和性（不能在所有 CPU 上运行），就将其发送到全局 fallback 队列。

### 3. 根本原因

检查系统 CPU 配置：

```bash
$ cat /sys/devices/system/cpu/possible
0-127

$ cat /sys/devices/system/cpu/online
0-3
```

| 值 | 含义 | 本系统数值 |
|---|------|-----------|
| `cpu_possible` | 可热插拔的最大 CPU 数 | 128 |
| `cpu_online` | 当前活跃的 CPU 数 | 4 |

同时检查进程的 CPU 亲和性：

```bash
# PID 1 (init/systemd)
$ cat /proc/1/status | grep Cpus_allowed_list
Cpus_allowed_list:	0-127   # 128 CPUs

# 普通进程
$ cat /proc/self/status | grep Cpus_allowed_list
Cpus_allowed_list:	0-3     # 4 CPUs
```

**原代码的问题**：

```c
skel->rodata->nr_cpus = libbpf_num_possible_cpus();  // = 128
```

而普通进程的 `nr_cpus_allowed = 4`（基于 online CPUs）。

条件判断变成：
```c
if (4 != 128)  // 永远为 true
```

**所有普通进程都被误判为有自定义 CPU 亲和性，全部进入 FALLBACK_DSQ。**

## 修复方案

### 修复内容

只需修改用户态代码 `scx_flatcg.c`，不改动 BPF 代码：

**1. 修改 nr_cpus 的初始化**：
```diff
-	skel->rodata->nr_cpus = libbpf_num_possible_cpus();
+	/*
+	 * Use online CPUs, not possible CPUs. A task's nr_cpus_allowed
+	 * is set to the number of online CPUs, so we need to match that
+	 * to avoid routing all tasks to FALLBACK_DSQ.
+	 */
+	skel->rodata->nr_cpus = sysconf(_SC_NPROCESSORS_ONLN);
```

**2. 修复 per-CPU map 读取**（BPF per-CPU 数组仍按 possible CPUs 分配）：
```diff
 static void fcg_read_stats(struct scx_flatcg *skel, __u64 *stats)
 {
-	__u64 cnts[FCG_NR_STATS][skel->rodata->nr_cpus];
+	/*
+	 * Per-CPU BPF maps are sized by libbpf_num_possible_cpus(),
+	 * not the number of online CPUs.
+	 */
+	int nr_possible_cpus = libbpf_num_possible_cpus();
+	__u64 cnts[FCG_NR_STATS][nr_possible_cpus];
     ...
-	for (cpu = 0; cpu < skel->rodata->nr_cpus; cpu++)
+	for (cpu = 0; cpu < nr_possible_cpus; cpu++)
```

### 为什么不修改 BPF 代码？

曾考虑将 BPF 中的 `!=` 改为 `<`，但这会改变原作者的设计意图：

- 原代码使用 `!=` 是"精确匹配"语义
- 系统进程（如 PID 1）有 `nr_cpus_allowed = 128`，进入 fallback 是可接受的
- 用户进程才是权重调度的主要目标，它们现在能正常工作

## 修复后验证

### 调度统计

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| global | 649 | 151 |
| local | 18 | 38 |

剩余的 151 个 global 任务是继承了 `Cpus_allowed: 0-127` 的系统进程。

### 权重公平性测试

```
Cgroup     Weight   Expected %   Actual %   Deviation
cg_w100    100      16.67%       16.71%     +0.04%
cg_w200    200      33.33%       33.13%     -0.20%
cg_w300    300      50.00%       50.15%     +0.15%

Maximum deviation: 0.20%
TEST PASSED
```

## 总结

### 问题本质

原代码假设 `cpu_possible == cpu_online`，在支持 CPU 热插拔的系统上（如云虚拟机）这个假设不成立。

### 修复要点

1. 使用 `sysconf(_SC_NPROCESSORS_ONLN)` 获取 online CPU 数
2. 读取 per-CPU BPF map 时仍使用 `libbpf_num_possible_cpus()`
3. 不修改 BPF 代码，保持与上游一致

### 影响范围

此问题影响所有 `cpu_possible != cpu_online` 的系统，建议向上游 scx 项目报告。
