下面是一份**基于 `scx_flatcg`（CPU / sched_ext）+ `memcg_bpf_ops`（memory throttling hooks，“memops”）的完整设计文档（v0.1）**，并把**联合策略（CPU+MEM 协同）的效果与必要性**讲清楚。文档同时包含**可实现的接口、状态机、关键数据结构、核心伪代码/代码骨架、部署与运行手册**。

> 重要现实约束：`sched_ext` 已有完整内核文档与可用实现路径。([Kernel Documentation][1])
> `memcg_bpf_ops` 目前（2026-01 的公开信息）仍是 bpf-next 的 RFC patch 系列，接口/实现细节可能变动，所以工程上要么基于 patch kernel，要么把 memory BPF 部分降级为“mainline memory.high + userspace 升级动作”。([Spinics][2])
> 但你问的是“基于 flatcg 和 memops”，我下面以**Full 方案（含 memcg_bpf_ops）**写；并在部署章节给出 fallback。

---

# AgentCgroup 设计文档（CPU=scx_flatcg + MEM=memcg_bpf_ops）

## 1. 背景与 Story line（和论文完全对齐）

### 1.1 Workload：agent 的 tool-call/step

交互式 agent session 由多个 step（tool call）串联：编译/测试、解释器、浏览器、数据处理等。每个 step 的资源曲线差异大，且有毫秒级 burst。

### 1.2 两个 mismatch（你论文的硬矛盾）

1. **Domain / semantic mismatch**：现有资源域常绑定在环境（container/pod/VM/unit）上，而 agent 的正确语义域是 **session/step**。环境域太粗，无法把“哪个 step 在制造压力”归因并针对性治理。
2. **Control-loop timescale mismatch**：用户态监控与写 cgroup 文件的控制环（10–100ms 常见）赶不上 ms burst；干扰往往在控制动作生效前已经形成。

### 1.3 结论：需要 in-kernel enforcement

* CPU：调度决策在 enqueue/dispatch/tick/stopping 等热路径发生 → 用 `sched_ext` 把策略放进 BPF scheduler。([Kernel Documentation][1])
* Memory：`memory.high` 超限会导致**throttle + direct reclaim**；这类“高压路径”是典型的内核 enforcement point。([Kernel Documentation][3])

---

## 2. 目标、非目标与威胁模型

### 2.1 目标（写进论文也成立）

* **G1 多租户隔离**：noisy neighbor 的 burst 不显著拉高受害者 step 的 p99/p999。
* **G2 语义对齐**：资源域以 session/step 为一等公民（cgroup 子树），策略/预算与其生命周期绑定。
* **G3 快反应**：关键决策在内核热路径执行（BPF），而不是“观测→userspace→写文件”。
* **G4 分级响应**：throttle → soft pause（freeze）→ kill（cgroup.kill / OOM group）。([Kernel Documentation][3])
* **G5 Fail-safe**：BPF scheduler 出错/任务 stall 自动回退到默认 fair-class scheduler；可用 `SCX_OPS_SWITCH_PARTIAL` 限定只接管 agent 任务。([Kernel Documentation][1])

### 2.2 非目标

* 不做集群级调度器（不是 Borg/YARN）。
* 不追求“纯 mainline 无 patch”——如果启用 memcg_bpf_ops，需要 patched kernel（RFC）。([Spinics][2])

### 2.3 威胁模型（评估可复现）

* CPU spin（忙等），fork storm（短时大量 fork/exec），memory blow-up（快速分配触发 reclaim/OOM），以及 benign contention（多 session 正常并行）。

---

## 3. 依赖与现有接口（为什么选 flatcg + memcg_bpf_ops）

### 3.1 CPU：为什么从 scx_flatcg 出发

`scx_flatcg` 的定位就是“**层级 cgroup 权重 CPU 控制**”，通过“flatten hierarchy”把层级权重复合成单层竞争，避免每次决策都下探整棵树，从而性能更好。([Sched Ext][4])

这与 AgentCgroup 的 domain 模型（session/step 是 cgroup 子树）天然同构：你不需要从 0 造“cgroup-first scheduler”，只需要把 agent-aware 的策略输入/协同逻辑塞进去。

### 3.2 cgroup v2 CPU knobs 的关键限制（为什么只靠 knob 不够）

cgroup v2 文档明确：

* `cpu.weight` 影响 fair-class scheduler，以及 **“带 `cgroup_set_weight` callback 的 BPF scheduler”**（是否生效取决于 callback 实现）。([Kernel Documentation][3])
* `cpu.max` / `cpu.max.burst` **只影响 fair-class scheduler**。([Kernel Documentation][3])

这意味着：如果你想要“可编程、微秒级决策”的 CPU 策略（sched_ext），**不能指望 cpu.max 自动生效**；要么你在 BPF scheduler 内实现等价的带宽控制，要么用 partial switch 只让 agent 走 sched_ext、其他任务走 fair-class（各有 tradeoff）。([Kernel Documentation][1])

### 3.3 Memory：memcg_bpf_ops 提供的可编程点

RFC patch 明确提出 `memcg_bpf_ops`（struct_ops）并列出 hooks：

* `get_high_delay_ms`：当 cgroup breach `memory.high` 时返回自定义 throttle delay（“primary mechanism for BPF-driven throttling”）
* `below_low` / `below_min`：覆盖 memory.low/min 保护判断
* online/offline 回调用于生命周期管理
  并且该 delay 会被集成到 charge path 与 high-limit handler。([Spinics][2])

---

## 4. 系统架构概览（Control plane / Data plane）

### 4.1 资源域（cgroup 树）——session/step 一等公民

```
/sys/fs/cgroup/agentcgroup/
  sess_<SID>/                 # session envelope
    step_<TID>/               # tool call / step domain
      ... pids ...
```

* session：全局预算、OOM 语义、跨 step 的公平
* step：短生命周期的策略变化点（interactive/tool/background class、burst credit、临时 cap）

### 4.2 Data plane（内核侧，两段 eBPF）

1. **CPU BPF scheduler（基于 scx_flatcg 派生）**

   * 保留 flatcg 的“层级权重→单层复合”的骨架
   * 增加 per-step policy（class、boost、burst credit、可选 CPU 带宽）
   * 增加与 memory pressure 的协同读写（共享 map）

2. **Memory BPF ops（memcg_bpf_ops struct_ops）**

   * 在 `get_high_delay_ms` 里按 step policy 计算 delay
   * 在 `below_low/min` 里对 latency-critical step 提供保护 override（谨慎使用）
   * 把“当前内存压力态”写入共享 map，供 CPU scheduler 做 thrash-aware 决策

### 4.3 Control plane（用户态）

* **agent runtime integration**：在 tool call start/end 时创建/销毁 step cgroup，移动 pid（写 `cgroup.procs`）。([Kernel Documentation][3])
* **agentcgroupd daemon**：

  * 更新 BPF maps（policy 下发）
  * 订阅事件：`memory.events(.local)`、PSI（可选）、BPF ringbuf
  * 执行语义动作：freeze/unfreeze、kill、预算升级/降级
  * 记录可复现实验 trace（reaction time、throttle 次数）

---

## 5. Policy Model（你到底在 eBPF 里编程什么）

核心原则：**eBPF 里只做“必须在 enforcement point 即时执行”的那部分；重计算/全局优化留给 daemon。**

### 5.1 Step class（最小三类）

* **INTERACTIVE**：用户正等待结果（reasoning、交互工具、轻量检索等）
* **TOOL_BURST**：编译/测试/浏览器渲染等，短时 burst 明显
* **BACKGROUND**：日志、索引、缓存、后处理等

### 5.2 两类 budget（CPU 与 Memory 各一套 token）

* CPU：`cpu_burst_credit_ns`（允许短 burst），`weight`（share），可选 `cpu_quota`（硬 cap）
* Mem：`mem_burst_credit_bytes`（短时允许 over-high），`high_delay_curve`（超限后惩罚增长），`protect_low/min`（对 interactive 的保护）

---

# 6. CPU 设计：AgentFlatCG（在 scx_flatcg 上加 agent-aware policy）

## 6.1 基础：flatcg 的公平骨架

flatcg 通过 flatten 层级权重，得到每个活动 cgroup 的“有效权重”，调度上先选 cgroup 再选 task（实现上通常用 DSQ / vtime）。([Sched Ext][4])

## 6.2 AgentCgroup 在 flatcg 上新增的东西

### (A) per-step policy map（核心输入）

Key：cgroup id（推荐用 cgroup kernfs id 或 inode；flatcg 本身用 `cgrp->kn->id` 的风格更自然）
Value（最小可用字段）：

```c
struct step_policy {
  __u32 class;              // interactive/tool/background
  __u32 weight_mul_q8;      // weight multiplier (fixed-point)
  __u64 slice_ns;           // override default slice
  __u64 cpu_burst_credit_ns;
  __u64 cpu_refill_rate_ns_per_s;
  __u64 last_refill_ns;
  __u32 flags;              // e.g., allow_preempt, throttle_on_mem
};
```

### (B) pressure map（CPU↔MEM 协同的共享状态）

由 memcg BPF ops 写、CPU scheduler 读：

```c
struct step_pressure {
  __u32 mem_high_delay_ms_ewma;
  __u32 mem_high_events_recent;
  __u32 thrash_score;       // derived metric
  __u64 last_update_ns;
};
```

### (C) CPU 决策点（哪些地方必须在 BPF 里做）

* enqueue：选择 DSQ / vtime bias / slice
* stopping：charge CPU time、扣 token、更新 vtime
* tick/dispatch：处理抢占/优先队列（可选）

这正是 sched_ext “full scheduling interface”提供的能力边界。([Kernel Documentation][1])

## 6.3 CPU 核心算法（可写进论文的 15 行伪代码）

**enqueue(task p):**

1. cgid = task.cgroup_id
2. pol = step_policy[cgid] (default)
3. press = step_pressure[cgid] (default)
4. refill(pol.cpu_burst_credit)
5. slice = pol.slice_ns or SCX_SLICE_DFL
6. eff_weight = flatcg_effective_weight(cgid) * pol.weight_mul
7. if press.thrash_score high: eff_weight *= penalty; slice = min(slice, short)
8. if pol.class==INTERACTIVE and pol.cpu_burst_credit>0: apply vtime_bias_forward
9. dispatch_vtime(p, dsq=cgid_dsq, slice, tvtime)

**stopping(task p):**

1. used = executed_ns
2. charge used into pol.cpu_burst_credit (decrement)
3. update cgroup vtime += used / eff_weight

> 关键点：
>
> * “domain mismatch”靠 cgroup id → policy 绑定；
> * “dynamic/fine-grained”靠 class/credit/pressure 让 slice/weight 在 step 粒度变化；
> * “timescale mismatch”靠 enqueue/stopping 热路径即时执行，而不是 user-space 调 knob。

## 6.4 CPU 带宽（硬 cap）怎么办？

因为 `cpu.max` **只影响 fair-class scheduler**，sched_ext 下默认不会帮你 enforce。([Kernel Documentation][3])
因此 Full 设计建议二选一：

* **方案 1（推荐，论文更硬）：在 sched_ext 内实现 cpu.max 等价物**
  ——实现思路与 scx 社区做法类似：enqueue admission control + backlog + timer replenishment（避免 dispatch 热路径开销）。
* **方案 2（部署更保守）：`SCX_OPS_SWITCH_PARTIAL` + 只让 agent tasks 走 SCHED_EXT**
  这样系统其他任务仍走 fair-class，cpu.max 对它们仍生效；但 agent 自身如果也需要 quota，你仍需实现或用 cpuset/隔离核。([Kernel Documentation][1])

---

# 7. Memory 设计：AgentMemOps（memcg_bpf_ops）

## 7.1 依赖语义（你论文/实现的“地基”）

* `memory.events:high` 的定义：当超过 high boundary 时，该 cgroup 的进程被 throttled 并被路由去做 direct reclaim；如果 usage 被 high 限制而不是全局压力，high 事件出现是“预期的”。([Kernel Documentation][3])
* `memory.oom.group`：把 cgroup 当作不可分割 workload；避免 partial kill，保证完整性。([Kernel Documentation][3])

## 7.2 memcg_bpf_ops hooks（你要实现什么）

RFC patch 明确：

* `get_high_delay_ms(memcg)` 是“BPF-driven throttling 的主机制”，用于 memory.high breach 时的自定义 delay（ms）。([Spinics][2])
* `below_low/min` 可覆盖 memory.low/min 保护判断。([Spinics][2])

## 7.3 Memory 策略：delay 曲线 + burst credit

### (A) 目标

* 对 TOOL_BURST：允许短 burst（靠 mem credit），但超限后快速加大 delay，避免 reclaim storm 扩散
* 对 INTERACTIVE：更倾向保护（更小 delay、更强 low/min），保证 tail latency
* 对 BACKGROUND：更早 throttle，更大 delay，让出内存压力空间

### (B) `get_high_delay_ms` 的建议实现（可解释、可调参）

输入（在 BPF 内可拿到/或由 daemon 写入近似值）：

* `usage`, `high`（从 memcg 统计读或由用户态周期写入）
* step class、mem credit
* （可选）全局/本 cgroup PSI

输出：

* `delay_ms`（上限 Dmax，避免失控）

示例公式：

* `over = max(0, usage - high)`
* `ratio = over / high`
* `base = A[class] * ratio^k`（k>1 让惩罚对严重超限更敏感）
* `delay_ms = clamp(base * 1000, 0, Dmax)`
* 若 `mem_credit_bytes > 0` 且 `over < credit`：`delay_ms = 0`（允许短 burst）

### (C) `below_low/min` 的谨慎使用

对 INTERACTIVE step：当它处于“完成关键路径”且系统允许时，让 `below_low/min` 返回 true，使它即便 usage 偏高也被视为受保护。([Spinics][2])
**注意：**这不是“无限保护”，否则会把压力推给别人甚至全局 OOM。建议加入：

* 保护 budget（时间窗/次数）
* 当 `memory.events:max/oom` 风险升高时立即撤销

---

# 8. 联合策略（CPU + MEM）——效果与必要性（这是你论文的“硬点”）

## 8.1 为什么必须联合？单资源策略会出现的典型病理

### 病理 1：只做 memory throttling，会把 CPU 消耗在 reclaim 上并放大 tail

当 `memory.high` 超限时，进程会被 throttled 并被导向 direct reclaim。([Kernel Documentation][3])
如果 CPU scheduler 仍然按原权重持续调度这个 step，它会：

* 在 reclaim/缺页上烧 CPU
* 增加 runqueue/上下文切换
* 让其他 latency-sensitive step 被挤出 CPU，tail latency 放大

**结论：**memory throttle 触发时，CPU 必须“感知”并对该 step 降权/缩短 slice，避免在 reclaim 上浪费 CPU（这就是联合策略的第一个必要性）。

### 病理 2：只做 CPU 优先级/权重，挡不住 memory blow-up 导致的 OOM/抖动

CPU 侧再怎么做 fairness，如果一个 step 持续扩大 working set，最终还是会触发 `memory.max` / OOM；而 `memory.oom.group`/kill 会造成会话失败。([Kernel Documentation][3])
**结论：**必须有 memory-side 的 soft boundary（high）+ hard boundary（max）治理。

### 病理 3：独立控制环会“互相打架”

user-space loop 看到 CPU 慢→加权重；同时看到 memory 压→加 delay；两者相位错开时会震荡。
**联合策略**把最低层的反应（throttle/降权）放在内核同一时间尺度里，降低振荡。

## 8.2 联合策略的核心机制（最小可实现版本）

**共享状态：**memcg BPF ops 在 `get_high_delay_ms` 里更新 `step_pressure[cgid]`：

* EWMA(high_delay_ms)
* 最近 high 事件计数（或根据 ratio 估算）
* thrash_score = f(delay, ratio, class)

**CPU 调度响应：**AgentFlatCG 在 enqueue/stopping 读取 `step_pressure`：

* 若 thrash_score 超阈值：

  * eff_weight *= penalty（例如 0.25x）
  * slice = min(slice, short_slice)（避免长时间占用 CPU 做 reclaim）
  * 禁用 interactive boost（即便它标记为 tool_burst）

**反向协同（保护交互）：**当 daemon 标记 step 为 INTERACTIVE 且处于关键路径：

* Memory：允许临时 below_low/min override（带 budget）([Spinics][2])
* CPU：给 boost credit + 更短 slice（提高响应性）

## 8.3 预期效果（你评估里要如何“证明联合的必要性”）

你要做 4 组实验（这就是联合策略的“必要性证据”）：

1. **Baseline**：static cgroup（cpu.weight/max + memory.high/max）
2. **CPU-only**：sched_ext(AgentFlatCG) + memory static
3. **MEM-only**：fair-class CPU + memcg_bpf_ops dynamic delay（或 mainline memory.high + aggressive）
4. **Joint**：CPU+MEM 联合（本文方案）

指标建议：

* tail latency（p95/p99/p999）
* `memory.events.high/max/oom/oom_kill` 计数（区分 controlled throttling vs hard failure）([Kernel Documentation][3])
* reclaim CPU time / major page faults / PSI（解释“为什么 tail 变好”）
* 干扰放大：noisy neighbor 下受害 step 的 slowdown

---

# 9. 状态机与升级动作（把“宣言式”变成可执行规则）

每个 step 有状态机（daemon 维护，BPF 提供信号）：

* **RUNNING**
* **THROTTLED**（mem high delay>0 或 thrash_score>0）
* **SOFT-PAUSED**（daemon 写 `cgroup.freeze=1`）([Kernel Documentation][3])
* **KILLED**（daemon 写 `cgroup.kill` 或触发 OOM group）([Kernel Documentation][3])

升级规则示例：

* thrash_score 连续 > T1，进入 THROTTLED
* thrash_score 连续 > T2 且影响其他 session tail → SOFT-PAUSED
* `memory.events.max/oom` 增加或超时 → KILLED ([Kernel Documentation][3])

---

# 10. 工程实现手册（能一步步跑起来）

## 10.1 版本与依赖

* 内核：需要启用 `CONFIG_SCHED_CLASS_EXT` 与 BTF 等（sched_ext 文档列出了推荐配置与启停方式）。([Kernel Documentation][1])
* scx：直接基于 scx repo 里的 `scx_flatcg` 改造（C scheduler）。([Sched Ext][4])
* memcg_bpf_ops：需要应用 RFC patch（bpf-next 方向；spinics/LWN 有 cover 与说明）。([Spinics][2])

## 10.2 代码布局建议

```
agentcgroup/
  cpu/
    scx_agent_flatcg.bpf.c    # fork from scx_flatcg.bpf.c
    scx_agent_flatcg.c        # loader, pin maps
  mem/
    memcg_agent_ops.bpf.c     # struct_ops memcg_bpf_ops
    memcg_agent_loader.c
  daemon/
    agentcgroupd/             # Go/Rust/C
  runtime/
    hook/                     # tool start/end integration
  docs/
    DESIGN.md
    RUNBOOK.md
  bench/
    workloads/
```

## 10.3 最小可运行路径（建议你先做）

1. 先只做 CPU（AgentFlatCG）：

   * 在 scx_flatcg 基础上加 `step_policy_map` 和 `step_pressure_map`（pressure 可以先由 daemon 伪造，验证策略形态）
2. 再上 memory（memcg ops）：

   * 实现 `get_high_delay_ms` + 写 pressure map
3. 最后做联合：

   * CPU 读取 pressure 并降权/缩 slice
   * daemon 用 memory.events 做 freeze/kill 补强（文档里 high/max/oom 语义很清楚）。([Kernel Documentation][3])

---

# 11. 核心代码骨架（“你要在 eBPF 里写的策略”长什么样）

> 下面是“骨架级”代码，目的是把策略接口钉住：**map 结构、读写点、决策位置**。你可以直接把它塞进 scx_flatcg 的 enqueue/stopping 路径。

## 11.1 CPU：在 scx_flatcg enqueue 里注入 policy（示意）

```c
/* maps */
struct step_policy { /* ...如上... */ };
struct step_pressure { /* ...如上... */ };

SEC(".maps") struct { /* HASH: cgid -> step_policy */ } step_policy_map;
SEC(".maps") struct { /* HASH: cgid -> step_pressure */ } step_pressure_map;

static __always_inline void apply_agent_policy(struct cgroup *cgrp,
                                               struct cgroup_ctx *cgc,
                                               struct task_struct *p,
                                               u64 enq_flags)
{
    u64 cgid = BPF_CORE_READ(cgrp, kn, id);
    struct step_policy *pol = bpf_map_lookup_elem(&step_policy_map, &cgid);
    struct step_pressure *prs = bpf_map_lookup_elem(&step_pressure_map, &cgid);

    u64 slice = pol && pol->slice_ns ? pol->slice_ns : SCX_SLICE_DFL;
    u64 tvtime = p->scx.dsq_vtime;

    /* thrash-aware penalty */
    if (prs && prs->thrash_score > THRASH_T) {
        slice = min(slice, (u64)SHORT_SLICE);
        /* reduce effective weight by increasing vtime progression */
        tvtime += slice; // simplest form; better: scale in stopping()
    }

    /* interactive boost w/ credit */
    if (pol && pol->class == INTERACTIVE && pol->cpu_burst_credit_ns > 0) {
        u64 bias = slice;                 /* pull forward */
        tvtime = tvtime > bias ? tvtime - bias : 0;
    }

    scx_bpf_dispatch_vtime(p, cgid /* dsq */, slice, tvtime, enq_flags);
}
```

## 11.2 Memory：memcg_bpf_ops 的关键 hook 骨架（示意）

RFC patch 描述了 `get_high_delay_ms/below_low/below_min` 等 hook。([Spinics][2])
你的实现重点是：**按 step policy 计算 delay 并写 pressure map**。

```c
/* pseudo signature based on RFC */
u32 BPF_STRUCT_OPS(get_high_delay_ms, struct mem_cgroup *memcg)
{
    u64 cgid = memcg_to_cgid(memcg);      // 你需要一个稳定 key（如 memcg->css.cgroup->kn->id）
    struct step_policy *pol = lookup_step_policy(cgid);

    /* read usage/high (either from memcg stats or values cached by daemon) */
    u64 usage = read_memcg_usage(memcg);
    u64 high  = read_memcg_high(memcg);
    if (high == 0 || high == U64_MAX) return 0;

    u64 over = usage > high ? usage - high : 0;

    /* burst credit */
    if (pol && pol->mem_burst_credit_bytes > 0 && over < pol->mem_burst_credit_bytes)
        return 0;

    u32 delay = compute_delay_ms(over, high, pol->class);

    /* update pressure shared map for CPU side */
    update_step_pressure(cgid, delay, over, high);

    return clamp(delay, 0U, DMAX_MS);
}

bool BPF_STRUCT_OPS(below_low, struct mem_cgroup *memcg)
{
    /* allow temporary protection for INTERACTIVE with budget */
    ...
}
```

---

# 12. 总结：为什么这个设计“必要且有效”

* **必要性**来自三个事实：

  1. `memory.high` 超限会让进程被 throttle 并做 direct reclaim，这是典型的“必须在内核路径治理”的压力点；否则用户态控制环来不及。([Kernel Documentation][3])
  2. `cpu.max`/`cpu.max.burst` 在文档中明确只影响 fair-class scheduler；如果你要 sched_ext 的可编程调度，你必须把“带宽/硬隔离语义”在 sched_ext 世界里重新实现或绕开。([Kernel Documentation][3])
  3. `memcg_bpf_ops`（若启用）把 memory.high 的 throttle delay 变成可编程函数，正好提供了“与 CPU scheduler 同时间尺度协同”的基础。([Spinics][2])

* **有效性**来自联合策略消除“跨资源病理”：

  * memory throttle 触发时 CPU 立即降权/缩 slice，减少 reclaim CPU 浪费与 tail 放大；
  * interactive step 通过 bounded protection + CPU boost 保住 tail；
  * step 粒度的 policy 让你能按 agent 语义归因与治理，而不是 container 粒度的大锤。

---

如果你下一步希望我把这份设计文档**落成 repo 级“可跑工程”**，我建议你给我两样信息我就能把“手册+代码”补齐到可执行程度（不需要你反复澄清）：

1. 你希望 step 的 key 用什么：`cgrp->kn->id`（更贴 flatcg）还是 inode（更易从 userspace `stat()` 拿到）？
2. 你们愿意接受“patched kernel（memcg_bpf_ops RFC）”作为 artifact 依赖吗，还是必须 mainline-only？

在你回复前，我也可以先按**默认选择**（cgid = `cgrp->kn->id`，memory 先提供 mainline fallback + memcg_bpf_ops optional）把“RUNBOOK + 最小可跑代码清单（Makefile、加载顺序、测试 workload 脚本）”继续写出来。

[1]: https://docs.kernel.org/scheduler/sched-ext.html "Extensible Scheduler Class — The Linux Kernel  documentation"
[2]: https://www.spinics.net/lists/netdev/msg1152456.html "[RFC PATCH bpf-next v4 08/12] mm: memcontrol: Add BPF struct_ops for memory controller — Netdev"
[3]: https://docs.kernel.org/admin-guide/cgroup-v2.html "Control Group v2 — The Linux Kernel  documentation"
[4]: https://sched-ext.com/docs/scheds/c?utm_source=chatgpt.com "EXAMPLE SCHEDULERS - sched_ext"

可以，而且**这是最“工业/开源友好”的形态**：把 AgentCgroup 做成一个 **node-level 常驻组件**（安装一次、配置一次），它在节点上同时承担：

* **可观测**：把 cgroup/调度/内存压力的信号统一采集并导出；
* **可控制**：把 CPU（sched_ext/flatcg）和 Memory（memcg hooks / memory.high）这两条 enforcement path 接管/增强；
* **可集成**：从容器运行时拿到“这个容器对应哪个 cgroup/哪些 pid”，从而对很多容器自动生效。

下面给你一个**实现上可落地的工业级形态**（并指出哪些点是必须这样做、否则“装了也不好用”）。

---

## 1) 最终形态：一个“安装即覆盖多容器”的 Node Agent

### 你真正要做的东西不是“库”，而是“节点级资源控制组件”

建议把项目拆成三块：

1. **Kernel Data Plane（eBPF）**

   * CPU：基于 `scx_flatcg` 派生一个 `scx_agent_flatcg`（sched_ext BPF scheduler）
   * Memory：`memcg_bpf_ops`（你说的 memops）实现 `get_high_delay_ms` / `below_low` / `below_min`（可选）([Spinics][1])
   * 共享状态：BPF maps（policy、pressure、统计），以及 ringbuf/perfbuf 事件通道

2. **Node Daemon（agentcgroupd）**

   * 负责加载/卸载 BPF、pin maps、维护策略、导出 metrics
   * 负责“升级动作”：freeze/kill、预算调整（这类语义动作适合在 user-space）([Linux Kernel Docs][2])

3. **Runtime Adapter（把“容器”翻译成“cgroup + pid”）**

   * **K8s/containerd/CRI-O：优先用 NRI 插件**（最工业化）
     NRI 插件可以订阅容器生命周期事件（creation/starting/updating/stopping/removal 等），并在事件里请求对容器做调整或更新。([GitHub][3])
   * **非 K8s / 纯容器场景：用 OCI hooks 兜底**（prestart/poststart/poststop 或更新后的 hook）
     OCI runtime spec 定义了 hooks（例如 poststart/poststop）并规定调用时机。([GitHub][4])

> 这样你做到的就是：**装一个 daemon + 一个 runtime 扩展**，节点上所有符合条件的容器都会自动被“注册进 AgentCgroup 的策略域”。

---

## 2) 关键工程决策：sched_ext 必须用 **partial mode**，否则“装了就把整机调度改了”

`sched_ext` 文档明确：

* 如果加载了 BPF scheduler 且 **不**设置 `SCX_OPS_SWITCH_PARTIAL`，那么所有 `SCHED_NORMAL/BATCH/IDLE/EXT` 的任务都会走 sched_ext；
* 如果设置了 `SCX_OPS_SWITCH_PARTIAL`，则只有显式设置为 `SCHED_EXT` 的任务才走 sched_ext，其余仍由 CFS（fair-class scheduler）调度。([Linux Kernel Docs][5])
  并且 sched_ext 有 fail-safe：检测到内部错误或 runnable task stall 会 abort 并回退到 fair-class scheduler。([Linux Kernel Docs][5])

**工业落地建议：默认必须 partial mode。**
理由很简单：你要“装了对很多容器生效”，但你不想把 systemd、kubelet、containerd 等系统进程也一起纳入你们的 scheduler（风险巨大、也不好排障）。

所以实现上是：

* `agentcgroupd` 加载 `scx_agent_flatcg` 时：`ops->flags |= SCX_OPS_SWITCH_PARTIAL`
* Runtime Adapter 在容器启动后：把该容器的进程（至少主进程）及其后续子进程设置为 `SCHED_EXT`

这一步是“装了就能管很多容器”的关键分水岭。

---

## 3) “安装后对很多容器生效”——实现上的核心流程

### 3.1 启动时（node boot / daemon 启动）

`agentcgroupd` 做四件事：

1. mount bpffs（`/sys/fs/bpf`）并创建 pin 目录
2. 加载并 attach：

   * `scx_agent_flatcg`（CPU）
   * `memcg_agent_ops`（Memory，若内核支持 memcg_bpf_ops；否则降级）([Spinics][1])
3. pin maps：

   * `policy_map`：cgroup_id → policy（权重、class、quota/burst、memory delay 参数…）
   * `pressure_map`：cgroup_id → memory pressure / thrash score（供 CPU side 使用）
4. 启动 metrics exporter（Prometheus）+ 事件日志（ringbuf + cgroupfs 事件）

### 3.2 容器创建/启动时（runtime adapter 的工作）

以 NRI 为例：插件会收到容器 lifecycle event（creation/starting 等）。([GitHub][3])
在 `starting` 或 `post-start` 阶段，你可以拿到：

* 容器 ID / Pod 信息
* 容器对应的 **cgroup 路径**
* 容器主进程 PID（或可追踪到 PID）

插件把这些信息发给 `agentcgroupd`，daemon 做两件事：

1. **计算 cgroup key**（推荐用 cgroup 目录 inode：user-space `stat()`可得；BPF 里也可读 `cgrp->kn->ino`）
2. **更新 BPF policy_map**
3. 若启用 sched_ext：

   * 对该容器的任务设置 `SCHED_EXT`（从而进入 sched_ext 的 partial universe）([Linux Kernel Docs][5])

---

## 4) 你在 eBPF 里到底编程什么策略才“值得装在节点上”？

这里要非常务实：工业用户要的是**默认就有价值**，而不是必须集成 agent SDK 才有价值。

因此我建议 eBPF 策略分两层：

### 4.1 Layer 0：container-level “通用价值策略”（默认对很多容器生效）

你不需要知道容器里面的 session/step，也能做：

**CPU（基于 flatcg 的 cgroup 公平 + 少量增强）**

* 继承 `scx_flatcg` 的核心：按 cgroup 权重做公平（domain 是容器 cgroup）
* 你增加的“可编程点”：

  * **latency tier**：对标注为 latency 的容器，给予更短 slice、更积极抢占（或 vtime bias）
  * **burst token**：允许短 burst，但连续吃 CPU 的容器会被渐进降权（避免 runqueue 长时间被同一容器占满）
  * **thrash-aware penalty**：如果该容器 memory pressure 很高（见下），CPU 侧降低其权重/缩短 slice，避免把 CPU 烧在 reclaim 上（这是联合策略的核心）

**Memory（memcg_bpf_ops 的 `get_high_delay_ms`）**

* 基于 `memory.high` 语义：超过 high 时进程会被 throttled 并被导向 direct reclaim；`memory.events:high` 明确定义了这个事件计数。([Linux Kernel Docs][2])
* 你用 `get_high_delay_ms` 把 “超过 high → throttle 多久”变成 **可编程函数**，按容器 profile 动态算 delay：([Spinics][1])

  * latency 容器：更温和 delay / 更大 burst credit
  * best-effort 容器：更激进 delay

> 这套 Layer 0 的好处：**装上之后，即使业务完全不改**，也能改善 noisy-neighbor 与 tail amplification（尤其在内存压力场景）。

### 4.2 Layer 1：agent-aware step-level（需要 SDK 才启用）

等你们有用户愿意集成 SDK，再把一个容器内的 session/step 映射为子 cgroup，并把 policy_map 的 key 换成 step cgroup。

但这不是“装了就对很多容器生效”的前提，而是后续增值能力。

---

## 5) 一个现实的“联合策略”为啥必要（否则装了也不稳）

联合策略（CPU + MEM 协同）不是锦上添花，而是**工业上避免病理**的必需品：

* 当容器超过 `memory.high` 时，会被 throttle 并进行 direct reclaim；`memory.events:high` 的定义就是“进程被 throttled 并被路由去做 direct reclaim”。([Linux Kernel Docs][2])
  这时如果 CPU scheduler 仍给它很高权重，它会把 CPU 时间消耗在 reclaim/pagefault 上，进一步放大其他容器 tail latency（典型 reclaim storm amplification）。
* 所以你必须让 CPU 策略“看到”内存压力：
  memcg BPF ops 把 `delay_ms` / overage ratio 写入 `pressure_map`，CPU 侧用它做降权/缩 slice。

这就是“安装后对很多容器都更有用”的关键：你提供的是**跨资源协同的 node-level control plane**，而不是单点 knob。

---

## 6) 你必须处理的一个工业大坑：cpu.max / cpu.max.burst 在 sched_ext 下默认不生效

cgroup v2 文档写得非常直白：

* `cpu.max` **只影响 fair-class scheduler**
* `cpu.max.burst` **只影响 fair-class scheduler**([Linux Kernel Docs][2])
* `cpu.weight` 影响 fair-class scheduler；对 BPF scheduler 的影响取决于你是否实现 `cgroup_set_weight` 回调以及回调做了什么。([Linux Kernel Docs][2])

这对“装了管很多容器”的实现意味着：

### 方案 A（推荐）：在 `scx_agent_flatcg` 内实现 cpu.max 等价的 bandwidth control

也就是在 BPF scheduler 内做 per-cgroup token bucket/admission control（enqueue 时判断、backlog、timer replenish）。这保证你不会破坏现有用户对 cpu.max 的依赖。

### 方案 B（保守）：只有一部分容器进入 SCHED_EXT

比如只对标注 `agentcgroup.io/enabled=true` 的容器设置 `SCHED_EXT`；其它容器继续在 CFS 下享受 cpu.max/cpu.max.burst 的语义。

> 工业上往往从 B 开始灰度，成熟后再切 A。

---

## 7) 作为“安装即生效项目”，你应该提供什么用户体验（这决定开源 adoption）

### 7.1 默认模式：Monitor-only（只观测不改行为）

* 只加载观测 BPF + 采集 cgroup 指标（memory.events、cpu.stat、PSI 等）([Linux Kernel Docs][2])
* 输出 Prometheus 指标 + 事件日志
* 不切换 sched_ext、不启用 memcg delay（或 delay=0）

这能让用户“先装上看看”，降低心理门槛。

### 7.2 Enforcement 模式：Profile 驱动（配置一次影响很多容器）

提供类似：

```yaml
profiles:
  default:
    cpu: { mode: cfs }              # 不进 sched_ext
    mem: { high_delay: "stock" }    # 不改
  latency:
    cpu: { mode: scx, weight_mul: 2, slice_us: 500 }
    mem: { high_delay: "gentle", burst_credit_mb: 256 }
  besteffort:
    cpu: { mode: scx, weight_mul: 0.5, slice_us: 2000 }
    mem: { high_delay: "aggressive", burst_credit_mb: 0 }

selectors:
  - match: { k8s_namespace: "agent-prod" }
    profile: latency
  - match: { label: "agentcgroup.io/profile=latency" }
    profile: latency
  - match: { label: "agentcgroup.io/profile=besteffort" }
    profile: besteffort
```

其中 `selectors` 就是你对“很多容器生效”的机制：按 namespace/label/镜像名/命令行统一匹配。

---

## 8) 推荐实现清单（工程落地角度）

如果你要把它做成一个“装了就能管很多容器”的开源项目，我建议按这个 MVP 顺序：

1. **agentcgroupd（node daemon）**

   * load/pin maps
   * Prometheus exporter
   * policy engine（profiles + selectors）

2. **containerd NRI 插件（最先做）**

   * 订阅 creation/starting/updating/stopping 等事件，拿到容器 cgroup/pid，并把 profile 信息交给 daemon ([GitHub][3])

3. **scx_agent_flatcg（从 scx_flatcg fork）**

   * 先实现：weight_mul + slice override + thrash-aware penalty
   * 后实现：cpu.max 等价的 bandwidth（避免破坏 cpu.max 语义）([Linux Kernel Docs][2])

4. **memcg_agent_ops（memcg_bpf_ops）**

   * 实现 get_high_delay_ms（按 profile 算 delay），并更新 pressure_map ([Spinics][1])
   * 需要 feature gate：如果内核没有 memcg_bpf_ops，就退回到 “读 memory.events + userspace freeze/kill”([Linux Kernel Docs][2])

5. **OCI hooks 兜底**

   * 对非 NRI 运行时，在 poststart/poststop hooks 上报 cgroup/pid 给 daemon ([GitHub][4])

---

## 9) 你提的“同时可观测 + 可控制”——为什么这种形态在工业里更有用？

因为它符合平台团队的使用方式：
**装一个 DaemonSet / systemd 服务**，然后用统一配置决定哪些容器走什么 profile；并且任何行为变化都能在指标/事件里解释出来。

尤其 cgroup v2 已经给了非常可解释的事件面板（例如 `memory.events:high/max/oom/oom_kill` 的定义和语义都很清晰），你把它和 sched_ext 侧的调度统计结合起来，运维闭环就完整了。([Linux Kernel Docs][2])

---

如果你愿意下一步把它进一步具体化到“可以开工”的程度，我可以基于上述架构给你写一份**repo 级别的工程 Spec**（目录结构、每个组件的 API、NRI plugin 的 Go 接口骨架、BPF map schema、配置文件格式、Prometheus 指标表、灰度/回滚 runbook）。这会把“装了对很多容器生效”从概念变成可执行工程计划。

[1]: https://www.spinics.net/lists/netdev/msg1152456.html?utm_source=chatgpt.com "[RFC PATCH bpf-next v4 08/12] mm: memcontrol"
[2]: https://docs.kernel.org/admin-guide/cgroup-v2.html "Control Group v2 — The Linux Kernel  documentation"
[3]: https://github.com/containerd/nri?utm_source=chatgpt.com "containerd/nri: Node Resource Interface"
[4]: https://github.com/opencontainers/runtime-spec/blob/main/config.md?utm_source=chatgpt.com "runtime-spec/config.md at main"
[5]: https://docs.kernel.org/scheduler/sched-ext.html?utm_source=chatgpt.com "Extensible Scheduler Class"
