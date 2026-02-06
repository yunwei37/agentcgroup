整体逻辑 **比你最初那版清晰很多**：现在的 Abstract/Intro/Motivation 形成了一个一致的“**mismatch → 需要 in-kernel programmable control**”叙事链条；而且你把“我们不是发明 cgroup/eBPF，而是用它们做 agent-aware control”这点也更自然了。

但从 reviewer 的角度看，你这版仍然有几个**容易被挑的欠缺点/不严谨点**（其中有一两个是“会被一票否掉”的那种），以及“具体设计”现在还是偏宏观口号，需要落到 **可实现的接口、状态机、map 设计、策略函数**。

下面我按 **(A) 逻辑清晰度 & framing 风险** → **(B) 你缺的关键设计件** → **(C) 我建议的具体设计落地方案** 给你一个可操作的 checklist。

---

## A) 逻辑是否清晰？主要风险点在哪里？

### A1. “environment / tool-call”这两个词现在太虚，会导致 domain mismatch 站不住

你现在写：

> existing controls operate at **environment granularity** with static budgets … agent workloads require **tool-call level** control

问题是：**environment 是什么？**

* 如果你的 sandbox 本身就是“一 session 一 container/VM”，那“environment granularity”其实已经对齐 session 了，domain mismatch 就弱化了。
* 如果你的实际部署是“一个 container 里 multiplex 多 session / 多 step”，那 domain mismatch 才成立，但你需要明确写出来。

建议：把 domain mismatch 的对标对象明确到 **container/pod（部署单元）**，这是最不含糊、最容易被接受的 framing：

* Kubernetes 的资源 request/limit 是对 **container** 指定，kubelet 负责 enforce（底层用 cgroups）。([Kubernetes][1])
* systemd 的 resource control 也是把 unit/slice 映射到 cgroup 并设置 `cpu.max` 等。([Freedesktop][2])

所以你可以把 domain mismatch 改成一句更硬的定义：

> **Domain mismatch:** today’s stacks bind resource budgets to deployment artifacts (container/pod/unit), while agents need budgets aligned to *session/step (tool-call)* boundaries.

这比 “environment” 强很多。

---

### A2. 你把 “domain mismatch” 和 “dynamic control”混在一起了，容易被 reviewer 说“这是两个问题”

现在 Intro 的 domain mismatch 句子是：

> existing controls operate at environment granularity with static budgets, but agent workloads require dynamic, fine-grained control at tool-call level

更干净的分法（也更贴近你之前讨论的 semantic mismatch）其实是两层：

* **Domain boundary mismatch（边界不匹配）**：container/pod/unit ≠ session/step
* **Phase/policy mismatch（阶段策略不匹配）**：session 内 phase transitions ⇒ 需要动态策略（phase-aware/adaptive）

这样写的好处是：**dynamic/fine-grained 不是口号，而是被 phase transitions 推导出来的必然结论**。

---

### A3. timescale mismatch 的措辞有点过猛：`sub-millisecond fluctuations` 需要证据

Abstract/Intro 里出现 “sub-millisecond resource fluctuations”。如果你评估里没有真的做 “<1ms” 级测量，reviewer 会抓这一句。

你完全可以用 **更稳、但不弱** 的表达，且还能引用权威文档支撑：

* cgroup v2 文档明确说 `memory.high` 超限会 throttle、不会 OOM，并建议用在“**外部进程监控并缓解 reclaim 压力**”的场景。([Linux Kernel Documentation][3])
  这句话的潜台词就是：默认模型是 user-space loop，且它是“外部进程”。你要挑战的是这个模型的时延/粒度。

建议把 “sub-millisecond fluctuations” 换成：

> millisecond-scale bursts; control actions need to take effect on *kernel hot paths (per charge / per wakeup)*.

你仍然可以说 “microsecond-scale reaction”，但最好限定为“**决策点在 hot path**”，不要说 workload fluctuation 本身是 sub-ms。

---

### A4. 你现在最大的硬伤：**memcg_bpf_ops 不是稳定“已有机制”，你不能再写 “without kernel modifications”**

你 Background 里把 `memcg_bpf_ops` 写成“已经有的 hooks”。但从最新公开资料看，它目前还是 **RFC patch series（2026-01-27 的 v5）**，引入一个新的 `memcg_bpf_ops` struct_ops，并提供 `get_high_delay_ms / below_low / below_min / online/offline` 等 hook。([Spinics][4])

这意味着：

* 如果你真的用它做实验，你的 prototype **必须基于打过补丁的 kernel**（或者 bpf-next / 自己维护的分支）。
* 你的文中这句就不对了：

  > “builds on existing cgroup v2 infrastructure **without kernel modifications**”

**建议两种路线二选一（不要含糊）：**

1. **承认你用了开发中内核特性**（更现实）
   写清楚：我们在 Linux X.Y + memcg_bpf_ops patchset 上实现，目标是跟随上游；sched_ext 已在 v6.12 上游。([Kernel.org][5])

2. **如果你必须坚持“stock kernel”**
   那就不能把 memcg 动态 throttle 完全靠 memcg_bpf_ops；你需要退回到 `memory.high + memory.events + PSI + user-space actuation`，但这会削弱你的核心卖点（“in-kernel memory control”）。

从 systems 论文角度：**路线 1 更好**，因为你贡献就是“把 emerging kernel hooks 组织成 agent-aware system”。

---

### A5. 你现在的 title 有点太泛

“AI Agents Resource Control with eBPF” 很像一篇 blog，而不是 systems 论文标题。你至少要让 title 暗示你核心 novelty：**session/step semantics + in-kernel enforcement**。

例如（示例）：

* “\sys: Session/Step-Aware Resource Control for Interactive Agents via eBPF”
* “\sys: Agent-Semantic, In-Kernel Resource Control with sched_ext and memcg BPF hooks”

---

## B) 目前欠缺的关键设计内容（reviewer 会问，但你文里没回答）

### B1. 你到底“动态/细粒度”做了哪些决策？（必须列出具体动作集合）

现在你说 “dynamic, fine-grained decisions aligned with agent workload demands”，但没有明确说**决策空间是什么**。建议至少列出一组你系统真正会做的动作，例如：

* CPU：step 优先级、session 间公平、并发度限制、抢占/延迟敏感层
* Memory：动态调 `memory.high` 的惩罚（delay）、`memory.low/min` 的保护、触发 freeze/kill
* Lifecycle：soft-pause（freeze）、step abort、session abort（kill subtree）

并明确：**哪些动作在内核执行（fast path），哪些由 user-space 执行（慢但语义动作）**。

---

### B2. 需要一个明确的 “agent↔kernel contract”（API + 状态机）

你现在写 “daemon provides APIs to manage cgroup subtrees and updates BPF maps”，但缺少：

* runtime 在什么时候告诉 kernel “step 开始/结束”？
* step 的类别/预算/优先级如何传给 BPF？
* soft-pause 的触发条件、解除条件是什么？

系统论文里最稳的写法是给一个**状态机**（每个 step/session）：

`RUNNING → THROTTLED → (SOFT-PAUSED) → (RESUMED | KILLED | COMPLETED)`

并对应每条边的 trigger 和 action。

---

### B3. sched_ext 的安全约束你必须显式考虑：不能“故意饿死”任务

sched_ext 文档写得很明确：系统完整性会被维护；一旦检测到错误、runnable task stall，默认调度会恢复，并且可以随时 abort 回 CFS。([Linux Kernel Documentation][6])

这对你设计意味着：

* 你不能在 sched_ext 里用“不给 dispatch”来实现真正的 pause（会触发 stall/abort）
* 真正的 soft-pause 应该用 **cgroup.freeze**（或用户态执行 kill/freeze）作为语义动作；sched_ext 更适合做“减速/优先级/公平/层级调度”。

---

## C) 具体设计应该怎么搞（给你一个可落地的 blueprint）

下面是一个“能写进 Design 也能真正实现”的架构分解。重点是：**把 decision space、数据结构、控制面/数据面分层说清楚**。

---

### C1. 资源域建模：用 cgroup v2 树表达 session/step（但把命名说清楚）

建议统一术语：

* **session**：一次用户交互的 agent session（跨多个 tool calls）
* **step**：一次 tool invocation / sandboxed task group（你现在叫 tool-call）

cgroup 结构建议：

```
/sys/fs/cgroup/agent/
  tenant_A/
    session_S/
      step_1/
      step_2/
```

* session cgroup：放 session envelope（hard cap、整体公平权重、OOM group）
* step cgroup：放 phase/step 的临时策略（priority、soft budget、可冻结/可 kill）

关键实现点：创建 step 子 cgroup 后，用 `clone3(CLONE_INTO_CGROUP)` 或写 `cgroup.procs` 把该 step 的进程树放进去（这块你文里没写，但实现时非常关键）。

---

### C2. 数据面（in-kernel）做什么：把 “fast reaction”限定为两个 hot paths

#### CPU：sched_ext 负责 **dispatch policy**

你可以把 CPU policy 写成一个清晰的两层调度：

1. **session-level fairness**：每个 session 一个权重/份额（weight / tokens）
2. **within-session priority**：interactive steps > background steps（例如 reasoning / user-facing tool）

实现方式（在 scx 术语里）：

* 每个 session 一个 DSQ（dispatch queue）
* step 的任务入队时，根据其所属 session/step metadata 进入对应 DSQ 或优先 DSQ
* dispatch 时先做 session-level 选择（WRR/stride），再在 session 内做 priority（FIFO/EDF）

你需要定义 BPF map（关键）：

* `map_session[cgroup_id] = {weight, cpu_budget, class, paused_flag, ...}`
* `map_step[cgroup_id] = {prio, deadline_hint, max_concurrency, ...}`

并定义 runtime/daemon 如何更新这些 map。

> 注意：sched_ext 的 fail-safe 行为是你 paper 的安全性卖点之一，但同时约束了你不能实现真正“stop dispatch”的 pause。([Linux Kernel Documentation][6])

#### Memory：memcg_bpf_ops 负责 **throttling policy**

如果你用 memcg_bpf_ops（你文中已写），那你应该把它真正用起来，而不仅是“挂钩子”：

* `get_high_delay_ms(memcg)`：返回动态 throttling delay
* `below_low/below_min`：给高优 session/step 提供保护 override（例如在某些 phase 强制认为它“受保护”）([Spinics][4])

一个可写进论文的 delay 函数例子（可实现、可解释）：

`delay_ms = base_ms * f(overage_ratio) * g(priority)`

* `overage_ratio = usage / memory.high`（可从 memcg 统计读到，或由用户态写入近似值）
* `g(priority)`：interactive step < background step
* 加 hysteresis，避免抖动（例如 EWMA）

同时明确：

* `memory.high` 语义本来就是 throttle + 外部监控者可介入。([Linux Kernel Documentation][3])
  你们的点是把“外部监控者的策略”**下沉到内核**（至少第一层反应下沉），让 burst 时能立刻施加惩罚。

---

### C3. 控制面（user-space daemon）做什么：语义动作 + 参数更新

user-space 不应该“跟内核抢 fast reaction”，它应做两件事：

1. **语义动作（slow but semantic）**

   * freeze/unfreeze（soft-pause）
   * kill（step/session 失败、超时、违规）
   * 调整预算（给 session 加预算 / 降并发）

2. **参数更新（写 BPF maps）**

   * 更新 step priority / session weight / budget
   * 根据 runtime 的 phase 信息更新 “当前 step 类型”
   * 接收内核事件（ringbuf/perf）并推进状态机

事件来源建议至少两类：

* `memory.events(.local)`：区分 high/max/oom/oom_kill，用来触发升级动作（throttle→freeze→kill）。([Linux Kernel Documentation][3])
* sched_ext 的 debug/状态接口：用于验证 fail-safe、调试调度行为。([Linux Kernel Documentation][6])

---

### C4. 把 “dynamic/fine-grained”变成一个明确的状态机（这会显著增强论文说服力）

建议在 Design 里画一个很简单但很硬的状态机（每个 step）：

* **RUNNING**：正常执行
* **THROTTLED**：memcg/sched_ext 对其施加延迟/降权（内核即时）
* **SOFT-PAUSED**：daemon 对 step cgroup 执行 freeze（语义暂停）
* **KILLED**：cgroup.kill（违规/超时/oom）

触发条件（示例）：

* `memory.events:high` 计数快速增长 → THROTTLED
* high 持续 + tail latency 受影响 → SOFT-PAUSED
* `memory.events:oom` 或 `max` 频繁触发 → KILLED（或 session-level fallback）([Linux Kernel Documentation][3])

你这样写，reviewer 会觉得：你不是“动态控制”口号，而是一个可验证的控制策略。

---

## D) 你这版还缺的一句“免责声明/定位句”（强烈建议加）

为了避免再次被审成“你在批评 cgroup 本身”，建议在 Intro 或 Motivation 开头加一句：

> We do not claim the kernel lacks primitives (cgroup v2, sched_ext). The gap is that existing stacks expose container-/environment-centric domains and static budgets, which do not surface session/step semantics nor enable phase-aware, low-latency enforcement.

并且把 `memory.high` 的“外部监控者”那句当作你论证 user-space loop 不够快的权威依据。([Linux Kernel Documentation][3])

---

## 最关键的改动建议（你现在立刻就该改的 2 点）

1. **删/改掉 “without kernel modifications”**，除非你不用 memcg_bpf_ops。因为 memcg_bpf_ops 目前是 RFC patch 引入的新接口。([Spinics][4])
2. **把 domain mismatch 的对标从 environment 改成 container/pod/unit**（或明确 environment=container），避免 reviewer 说你在 strawman。K8s 的 request/limit 是 per-container，这点可以直接引用。([Kubernetes][1])

---

如果你愿意，我可以在不改变你整体结构的前提下，帮你把 **Motivation 的 domain mismatch 段落**改成“Domain boundary mismatch + Phase/policy mismatch”的写法（保持你现在的叙事节奏），同时把 memcg_bpf_ops 的“上游状态/依赖”用一句话写得 reviewer 挑不出毛病。

[1]: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/?utm_source=chatgpt.com "Resource Management for Pods and Containers"
[2]: https://www.freedesktop.org/software/systemd/man/systemd.resource-control.html?utm_source=chatgpt.com "systemd.resource-control"
[3]: https://docs.kernel.org/admin-guide/cgroup-v2.html "Control Group v2 — The Linux Kernel  documentation"
[4]: https://www.spinics.net/lists/kernel/msg6024233.html "[RFC PATCH bpf-next v5 08/12] mm: memcontrol: Add BPF struct_ops for memory controller — Linux Kernel"
[5]: https://www.kernel.org/doc/html/v6.12/scheduler/sched-ext.html "Extensible Scheduler Class — The Linux Kernel  documentation"
[6]: https://docs.kernel.org/scheduler/sched-ext.html "Extensible Scheduler Class — The Linux Kernel  documentation"

下面我把你现在的 story line（**agent 工具调用 → 两个 mismatch（domain/semantic + timescale）→ in-kernel eBPF enforcement → 评估**）“工程化”成一份**可落地的软件设计文档 + 操作手册**，并且结合我刚刚检索到的**Linux 现有 CPU/cgroup 接口**回答你：**CPU 这块到底需不需要 sched_ext（以及替代方案能做到什么程度）**。

---

## 0. 先回答最关键的问题：CPU 侧我需要 sched_ext 吗？还有别的方案吗？

### Linux 现有的 CPU 资源接口（cgroup v2）

在 cgroup v2 下，CPU controller/相关接口主要是：

* **`cpu.weight`**：按权重分配 CPU（CFS/EEVDF 的 proportional-share）。文档也明确：它影响 fair-class scheduler；**对 BPF scheduler 是否生效取决于 BPF scheduler 是否实现了 `cgroup_set_weight` 回调以及回调做了什么**。([Linux Kernel Documentation][1])
* **`cpu.max`**：带宽上限（quota/period）。文档明确：**它只影响 fair-class scheduler**。([Linux Kernel Documentation][1])
* **`cpu.max.burst`**：允许短时 burst（在 quota 上的缓冲），同样只影响 fair-class scheduler。([Linux Kernel Documentation][1])
* **`cpu.uclamp.min` / `cpu.uclamp.max`**：utilization clamp（让任务至少/最多“被认为”使用多少算力，用于更积极/更保守的频率/调度选择），同样是 cgroup 文件接口。([Linux Kernel Documentation][1])
* 还有 `cpu.pressure`（PSI）、`cpu.stat`、`cpu.idle` 等。([Linux Kernel Documentation][1])

**结论 1（很重要）：**
如果你**不使用 sched_ext**，你能用的“内核内即时生效”的 CPU 杠杆主要就是这些 cgroup 文件（weight/quota/uclamp），但**动态/细粒度**一定绕不过“谁来改这些文件”——通常还是 user-space loop（哪怕 loop 很快，也仍然是 user-space 观测→决策→写文件→生效）。

**结论 2（决定你论文硬不硬）：**
如果你要写“**in-kernel CPU scheduling enforcement** + microsecond-level decision”这条线，且真的要把控制点放在 dispatch/enqueue 这种 hot path ——**sched_ext 是目前主线内核提供的正统入口**：它把一个完整的调度接口暴露给 BPF scheduler，允许动态开启/关闭，而且内核文档强调它有 fail-safe：检测到错误/卡死 runnable task/触发 SysRq-S 等会回退到默认 fair-class scheduler。([Linux Kernel Documentation][2])

**结论 3（替代方案能做到什么）：**

* **不用 sched_ext**也能做一个“Agent-aware resource control system”，但 CPU 侧你更像是在做：

  * “cgroup domain 对齐（session/step）+ 静态/半动态配置（step 边界配置 cpu.weight/cpu.max/uclamp）+ user-space 反馈调参”。
  * 这能解决 **domain/semantic mismatch** 的一大半，但对 **timescale mismatch（ms burst vs 10–100ms loop）** 只能“缓解”，很难“硬打穿”。
* **用 sched_ext**才能把“CPU 侧 timescale mismatch”也强行打穿：决策发生在调度回调里（enqueue/dispatch/running/stopping），是内核路径，反应时间定义上就不再依赖 user-space 轮询。([Linux Kernel Documentation][2])

因此我建议你把工程/论文都做成 **两档**（也更利于 artifact 与可移植性）：

* **AgentCgroup-Lite（无 sched_ext）**：只用 cgroup v2 的 cpu/memory 接口 + 你自己的 runtime/daemon；强项是“domain/semantic 对齐 + 机制细节 + 评估完整”，弱项是 CPU 侧“in-kernel policy”说不满。
* **AgentCgroup-Full（含 sched_ext）**：CPU 用 sched_ext BPF scheduler；memory 先用现有 memory.high/max + 事件/PSI，**可选**叠加 memcg_bpf_ops（见下文，它到 2026-01 仍是 RFC patch 系列，不是你能默认假设 mainline 都有）。

---

## 1. 工程设计文档：AgentCgroup（v0.1）

### 1.1 背景与问题定义（对齐你的论文 story）

**工作负载：** interactive agent session 由多次 tool call（编译/解释器/浏览器/数据处理）构成；每个 tool call 有不同资源曲线，且 burst 短（ms 级甚至更短）。

**两个 mismatch：**

1. **Domain / semantic mismatch**
   现有资源治理常以“环境（container/VM/sandbox）”为域；但 agent 的语义域天然是 **session/step（tool call）**。用环境域只给一个 static budget，会导致：要么过度保守浪费，要么 burst 时顶爆邻居/自己。
2. **Timescale mismatch**
   user-space controller（PSI/metrics → 决策 → 写 cgroup 文件）在 10–100ms 级很常见；而 tool burst 可以在 ms 级完成，互扰已经发生。
   → 因而需要**把至少一部分控制逻辑搬到 kernel enforcement point**。

---

## 1.2 目标 / 非目标

### 目标（可在论文里写成 checklist）

* **G1 Isolation（多租户隔离）**：noisy neighbor 的 burst 不显著拉高受害者 step/session 的 p99/p999。
* **G2 Semantic alignment**：resource domain 以 session/step 为一等公民；可在 step 生命周期内配置/继承/回收。
* **G3 Fast-path enforcement**：关键控制决策在 kernel 路径完成（CPU：dispatch/enqueue；MEM：throttle path）。
* **G4 Graduated response**：throttle → soft pause（freeze）→ kill（cgroup.kill）逐级升级。
* **G5 Fail-safe**：BPF scheduler 出错时回退默认调度；策略错误不导致系统不可恢复。([Linux Kernel Documentation][2])

### 非目标

* 不做集群级调度器（不是 Borg/YARN 那类）。
* 不追求“完全不改内核”——**CPU 侧可做到 mainline sched_ext；memory 侧如果要 memcg_bpf_ops，要承认是 patch/RFC 依赖**。

---

## 1.3 系统模型 & 威胁模型（你论文现在欠的“早期定义”）

### 系统模型

* 每个 agent workload 运行在一个 sandbox（可以是容器/进程树/轻量 VM，但对 OS 视角统一为“一个进程树”）。
* tool call = 一次子进程树（fork/exec 多）或一个 runner 内部阶段；**runtime 必须能在 tool start/end 时给 OS 发信号**。

### 威胁模型（最小集合，评估可复现）

* **CPU spin**：while(1) busy loop。
* **fork storm**：短时创建大量子进程。
* **mem blow-up**：快速分配/触发 reclaim/触发 OOM。
* **benign contention**：多个 session 并行，随机相位的 burst。

---

## 1.4 资源域建模（cgroup v2 树）

建议把资源域直接落在 cgroup v2 树上：

```
/sys/fs/cgroup/agentcgroup/
  sess_<SID>/                    # session 根
    step_<TID>/                   # tool call / step
      (processes of this tool)
```

### 每层语义

* `sess_<SID>`：session 总 envelope（总 CPU share / 总 mem 上限 / OOM 语义）。
* `step_<TID>`：tool call 的 phase 级 domain（更细粒度 CPU/mem policy）。

### 强制的生命周期语义

* **软暂停（soft pause）**：写 `cgroup.freeze=1` 冻结 step 子树；完成后 `cgroup.events` 的 frozen 字段更新，且冻结可能需要时间。([Linux Kernel Documentation][1])
* **完整失败（integrity on OOM）**：对 step（或 session）设置 `memory.oom.group=1`，保证 OOM kill 是“全杀或不杀”，避免半死状态。([Linux Kernel Documentation][1])
* **强制终止**：写 `cgroup.kill`（文档里它被设计来处理并发 fork 这类 race；你可在评估里验证）。([Linux Kernel Documentation][1])

---

## 1.5 架构总览（control plane / data plane）

### Data plane（内核侧）

* **CPU：sched_ext BPF scheduler（AgentCgroup-Full）**

  * 目标：按 session/step policy 做调度决策，并能快速对 runnable/stall 做保护。
* **MEM：两种实现路径**

  1. **mainline 可跑**：memory.high/max + memory.events + freeze/kill（由 user-space 触发）
  2. **研究型增强（可选）**：memcg_bpf_ops struct_ops（RFC patch 系列）在 memory.high throttle path 上执行 BPF delay 计算。

### Control plane（用户态）

* `agentcgroupd`（daemon）

  * 管理 cgroup 树（session/step create/delete）
  * 维护 policy（权重、interactive class、预算、burst credit）
  * 订阅事件（memory.events、PSI、BPF ringbuf）
  * 做升级动作（freeze/kill / 调整 policy）
* `agent runtime integration`

  * 在每次 tool call start/end 调用 daemon API
  * 把 tool 进程树加入对应 step cgroup（写 cgroup.procs）

---

## 2. 关键设计：CPU（sched_ext）与“不用 sched_ext”的对照

### 2.1 为什么 sched_ext 对你很关键

`sched_ext` 是一个可由 BPF 定义行为的 scheduler class，文档强调：

* 提供完整调度接口；
* 可动态开启/关闭；
* fail-safe：错误/卡死 runnable task/触发 SysRq-S 会回退默认 fair-class scheduler；
* 支持 `SCX_OPS_SWITCH_PARTIAL`：只让 `SCHED_EXT` 任务走 BPF，其他任务仍走 fair-class scheduler。([Linux Kernel Documentation][2])

同时要注意：**cgroup v2 的 `cpu.max`/`cpu.max.burst` 文档写明只影响 fair-class scheduler**。([Linux Kernel Documentation][1])
=> 这意味着：**一旦你把任务放到 sched_ext 下，CPU 带宽限制语义就需要你自己实现（或用 partial switch + cpuset/分区来绕开）**。这点在论文里必须说清楚，否则会被审稿人抓。

### 2.2 不用 sched_ext（Lite 版）你还能怎么做？

你可以用：

* `cpu.weight` 做 share，`cpu.max` 做上限，`cpu.max.burst` 做短 burst 宽容，`cpu.uclamp.min/max` 做交互/后台分层。([Linux Kernel Documentation][1])
* 但 dynamic/fine-grained 的“相位内自适应”，仍然是 user-space loop（PSI/trace → 写文件）。这会把你论文的“timescale mismatch”削弱成“我们调参更聪明”。

### 2.3 建议的落地策略（两步走）

* **v0（能跑）**：先做 sched_ext CPU data plane（Full），把 “kernel enforcement” 站住；memory 先用 mainline 方案（high/max + events + freeze）。
* **v1（强化 novelty）**：加 memcg_bpf_ops（需要 patch/RFC），把 memory throttle 也做成 in-kernel programmable。

---

## 3. 关键设计：Memory（mainline 可跑版 + 可选 in-kernel 版）

### 3.1 mainline 可跑的内核语义（你要在论文里引用并用在系统里）

* `memory.high`：超过后会发生 throttling / direct reclaim；**可作为“软边界”**。`memory.events` 的 `high` 计数表示“因超过 high 而被 throttled 并被导向 direct reclaim”。([Linux Kernel Documentation][1])
* `memory.max`：硬上限；`memory.events.max/oom/oom_kill` 可观测硬失败路径。([Linux Kernel Documentation][1])
* `memory.oom.group`：保证 workload integrity。([Linux Kernel Documentation][1])

### 3.2 可选的 in-kernel 方案：memcg_bpf_ops（风险点要诚实）

到 2026-01 的公开信息里，**为 memory controller 增加 BPF hooks（包括对 memory.high delay 的可编程计算）仍然是 RFC patch 系列**，不是你能默认 mainline 都具备的稳定接口。
=> 工程上要么：

* 明确写“prototype on patched kernel / bpf-next”，artifact 提供 patch；
* 要么把这块降级为 user-space（但会损失你“in-kernel memory enforcement”的贡献）。

---

# 4. 可运行的最小原型（CPU：sched_ext）——一步步构建运行

下面给你一个 **“能跑起来的最小垂直切片”**：

* 在 `tools/sched_ext/` 目录里新增一个 scheduler：`scx_agentcgroup.bpf.c` + `scx_agentcgroup.c`
* loader 启动后会把 policy map pin 到 bpffs
* 运行一个小 daemon `agentcgroupd` 通过 Unix socket 接收“某个 step cgroup 的 inode → policy”并更新 map
* 一个 Python 脚本 `agentctl.py` 创建 session/step cgroup、把 tool 进程放进去、并把 step policy 下发给 daemon

> 你可以先在 VM 上跑（因为启用 sched_ext 会影响整机调度策略）。sched_ext 文档里也给了启停与状态查看方式。([Linux Kernel Documentation][2])

---

## 4.1 前置条件（内核/工具链）

### Kernel config（必须）

按内核文档，至少需要：
`CONFIG_BPF=y, CONFIG_SCHED_CLASS_EXT=y, CONFIG_BPF_SYSCALL=y, CONFIG_BPF_JIT=y, CONFIG_DEBUG_INFO_BTF=y` 等（文档还建议开启 JIT always/default on、pahole BTF tag 支持）。([Linux Kernel Documentation][2])

### 运行时检查

```bash
# 确认 sched_ext sysfs 存在
ls /sys/kernel/sched_ext

# 查看当前状态
cat /sys/kernel/sched_ext/state
```

这些路径与例子在内核文档里给出。([Linux Kernel Documentation][2])

### 工具链

* clang ≥ 16
* pahole ≥ 1.25
* bpftool
  这些在 `tools/sched_ext/README.md` 里写得很明确。([Kernel Git Repositories][3])

---

## 4.2 构建步骤（基于内核源码的 tools/sched_ext）

1. 获取一份带 `tools/sched_ext` 的内核源码（建议与你运行内核版本一致，或直接用官方树/你自己的内核树）。

2. 进入：

```bash
cd linux/tools/sched_ext
```

3. 把下面两份文件放进该目录（与 `scx_simple.*` 同级）：

* `scx_agentcgroup.bpf.c`（BPF scheduler）
* `scx_agentcgroup.c`（userspace loader）

4. 编译：

```bash
make -j"$(nproc)"
```

编译产物通常在 `tools/sched_ext/build/bin/`。([Linux Kernel Documentation][2])

---

## 4.3 运行步骤

### Step A：挂载 bpffs（如果没挂）

```bash
mount | grep "/sys/fs/bpf" || mount -t bpf bpf /sys/fs/bpf
mkdir -p /sys/fs/bpf/agentcgroup
```

### Step B：启动 sched_ext scheduler（会影响系统调度）

```bash
sudo ./build/bin/scx_agentcgroup
```

你可以在另一个终端看状态：

```bash
cat /sys/kernel/sched_ext/state
cat /sys/kernel/sched_ext/root/ops
```

这些路径/语义内核文档有说明。([Linux Kernel Documentation][2])

### Step C：编译并启动 daemon（更新 policy map）

```bash
gcc -O2 -Wall -o agentcgroupd agentcgroupd.c -lbpf
sudo ./agentcgroupd
```

### Step D：跑一个 demo tool call

```bash
sudo python3 agentctl.py run --cmd "bash -lc 'make -j8'" --interactive
```

---

# 5. 核心代码（可直接复制进 `tools/sched_ext/`）

## 5.1 `scx_agentcgroup.bpf.c`

> 设计点：
>
> * 基于 `scx_simple` 的 global vtime 队列
> * 通过 `scx_bpf_task_cgroup()` 拿到 task 所在 cgroup，然后读取 cgroup 目录 inode（作为 user-space 可得的 key）
> * `tool_policies` map：key = cgroup inode，value = {weight, flags}
> * 在 `stopping` 里用 policy weight 影响 vtime 增长速度（weight 越大，vtime 增长越慢 → 越多 CPU）

```c
/* SPDX-License-Identifier: GPL-2.0 */
#include <scx/common.bpf.h>

char _license[] SEC("license") = "GPL";

/*
 * Policy flags
 */
#define ACP_F_INTERACTIVE  (1U << 0)

struct tool_policy {
	__u32 weight;   /* 1..10000, larger => more CPU */
	__u32 flags;    /* ACP_F_* */
};

/* user-space sets: key=cgroup dir inode id */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(key_size, sizeof(__u64));
	__uint(value_size, sizeof(struct tool_policy));
	__uint(max_entries, 16384);
} tool_policies SEC(".maps");

/* Like scx_simple: create our own DSQ so we can use vtime PQ insert */
#define SHARED_DSQ 0
static u64 vtime_now;

/* Helper: get cgroup inode from task */
static __always_inline __u64 task_cgrp_ino(struct task_struct *p)
{
	struct cgroup *cgrp;
	__u64 ino = 0;

	cgrp = scx_bpf_task_cgroup(p);
	if (!cgrp)
		return 0;

	/* kernfs_node.ino is the cgroup directory inode id */
	ino = BPF_CORE_READ(cgrp, kn, ino);

	bpf_cgroup_release(cgrp);
	return ino;
}

static __always_inline __u32 eff_weight(struct task_struct *p)
{
	__u64 ino = task_cgrp_ino(p);
	struct tool_policy *pol;

	if (!ino)
		return p->scx.weight;

	pol = bpf_map_lookup_elem(&tool_policies, &ino);
	if (!pol || pol->weight == 0)
		return p->scx.weight;

	/* Simple “interactive boost”: multiply weight */
	if (pol->flags & ACP_F_INTERACTIVE) {
		__u64 w = (__u64)pol->weight * 4;
		if (w > 10000)
			w = 10000;
		return (__u32)w;
	}

	return pol->weight;
}

s32 BPF_STRUCT_OPS(agent_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
{
	bool direct = false;
	s32 cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &direct);

	if (direct)
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, SCX_SLICE_DFL, 0);
	return cpu;
}

void BPF_STRUCT_OPS(agent_enqueue, struct task_struct *p, u64 enq_flags)
{
	u64 vtime = p->scx.dsq_vtime;

	/* cap idling credit to one slice */
	if (time_before(vtime, vtime_now - SCX_SLICE_DFL))
		vtime = vtime_now - SCX_SLICE_DFL;

	/* “interactive” gets pulled slightly earlier in vtime */
	{
		__u64 ino = task_cgrp_ino(p);
		struct tool_policy *pol = ino ? bpf_map_lookup_elem(&tool_policies, &ino) : NULL;
		if (pol && (pol->flags & ACP_F_INTERACTIVE)) {
			/* pull forward by half slice (bounded) */
			if (vtime > SCX_SLICE_DFL / 2)
				vtime -= SCX_SLICE_DFL / 2;
			else
				vtime = 0;
		}
	}

	scx_bpf_dsq_insert_vtime(p, SHARED_DSQ, SCX_SLICE_DFL, vtime, enq_flags);
}

void BPF_STRUCT_OPS(agent_dispatch, s32 cpu, struct task_struct *prev)
{
	scx_bpf_dsq_move_to_local(SHARED_DSQ);
}

void BPF_STRUCT_OPS(agent_running, struct task_struct *p)
{
	if (time_before(vtime_now, p->scx.dsq_vtime))
		vtime_now = p->scx.dsq_vtime;
}

void BPF_STRUCT_OPS(agent_stopping, struct task_struct *p, bool runnable)
{
	__u32 w = eff_weight(p);
	__u64 used = (SCX_SLICE_DFL - p->scx.slice);

	/* scale by inverse weight: bigger w => smaller vtime increase */
	p->scx.dsq_vtime += used * 100 / w;
}

void BPF_STRUCT_OPS(agent_enable, struct task_struct *p)
{
	p->scx.dsq_vtime = vtime_now;
}

s32 BPF_STRUCT_OPS_SLEEPABLE(agent_init)
{
	return scx_bpf_create_dsq(SHARED_DSQ, -1);
}

SCX_OPS_DEFINE(agent_ops,
	.select_cpu	= (void *)agent_select_cpu,
	.enqueue	= (void *)agent_enqueue,
	.dispatch	= (void *)agent_dispatch,
	.running	= (void *)agent_running,
	.stopping	= (void *)agent_stopping,
	.enable		= (void *)agent_enable,
	.init		= (void *)agent_init,
	.name		= "agentcgroup");
```

---

## 5.2 `scx_agentcgroup.c`（loader：加载 + pin map）

```c
/* SPDX-License-Identifier: GPL-2.0 */
#include <stdio.h>
#include <unistd.h>
#include <signal.h>
#include <assert.h>
#include <errno.h>
#include <sys/stat.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <scx/common.h>

#include "scx_agentcgroup.bpf.skel.h"

static volatile int exit_req;

static void sigint_handler(int sig)
{
	(void)sig;
	exit_req = 1;
}

static int ensure_dir(const char *path)
{
	struct stat st;
	if (!stat(path, &st)) {
		if (S_ISDIR(st.st_mode))
			return 0;
		errno = ENOTDIR;
		return -1;
	}
	if (mkdir(path, 0755))
		return -1;
	return 0;
}

int main(int argc, char **argv)
{
	struct scx_agentcgroup *skel;
	struct bpf_link *link;
	__u64 ecode;

	(void)argc; (void)argv;

	signal(SIGINT, sigint_handler);
	signal(SIGTERM, sigint_handler);

restart:
	skel = SCX_OPS_OPEN(agent_ops, scx_agentcgroup);
	if (!skel) {
		fprintf(stderr, "SCX_OPS_OPEN failed\n");
		return 1;
	}

	SCX_OPS_LOAD(skel, agent_ops, scx_agentcgroup, uei);
	link = SCX_OPS_ATTACH(skel, agent_ops, scx_agentcgroup);

	/* Pin policy map for daemon */
	if (ensure_dir("/sys/fs/bpf/agentcgroup")) {
		perror("mkdir /sys/fs/bpf/agentcgroup");
		goto out;
	}

	if (bpf_map__pin(skel->maps.tool_policies,
			 "/sys/fs/bpf/agentcgroup/tool_policies")) {
		fprintf(stderr, "pin tool_policies failed\n");
		goto out;
	}

	printf("agentcgroup scheduler loaded, policy map pinned at:\n");
	printf("  /sys/fs/bpf/agentcgroup/tool_policies\n");
	fflush(stdout);

	while (!exit_req && !UEI_EXITED(skel, uei))
		sleep(1);

out:
	bpf_link__destroy(link);
	ecode = UEI_REPORT(skel, uei);
	scx_agentcgroup__destroy(skel);

	if (UEI_ECODE_RESTART(ecode))
		goto restart;
	return 0;
}
```

---

## 5.3 `agentcgroupd.c`（最小 daemon：接收 policy 更新并写入 map）

> 协议：Unix Datagram，消息为 `struct msg { u64 ino; u32 weight; u32 flags; }`

```c
/* SPDX-License-Identifier: GPL-2.0 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <errno.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>

#include <bpf/bpf.h>

#define SOCK_PATH "/run/agentcgroupd.sock"
#define MAP_PATH  "/sys/fs/bpf/agentcgroup/tool_policies"

#define ACP_F_INTERACTIVE  (1U << 0)

struct tool_policy {
	uint32_t weight;
	uint32_t flags;
};

struct msg {
	uint64_t cgrp_ino;
	uint32_t weight;
	uint32_t flags;
};

static int setup_sock(void)
{
	int fd = socket(AF_UNIX, SOCK_DGRAM, 0);
	if (fd < 0)
		return -1;

	struct sockaddr_un addr = {0};
	addr.sun_family = AF_UNIX;
	strncpy(addr.sun_path, SOCK_PATH, sizeof(addr.sun_path) - 1);

	unlink(SOCK_PATH);
	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		close(fd);
		return -1;
	}
	return fd;
}

int main(void)
{
	int map_fd = bpf_obj_get(MAP_PATH);
	if (map_fd < 0) {
		perror("bpf_obj_get(map)");
		return 1;
	}

	int sock = setup_sock();
	if (sock < 0) {
		perror("setup_sock");
		return 1;
	}

	printf("agentcgroupd listening on %s\n", SOCK_PATH);
	fflush(stdout);

	for (;;) {
		struct msg m;
		ssize_t n = recv(sock, &m, sizeof(m), 0);
		if (n < 0) {
			if (errno == EINTR) continue;
			perror("recv");
			break;
		}
		if ((size_t)n != sizeof(m)) {
			fprintf(stderr, "short msg: %zd\n", n);
			continue;
		}

		uint64_t key = m.cgrp_ino;
		struct tool_policy val = {
			.weight = m.weight ? m.weight : 100,
			.flags  = m.flags,
		};

		if (bpf_map_update_elem(map_fd, &key, &val, BPF_ANY) < 0) {
			perror("bpf_map_update_elem");
			continue;
		}

		fprintf(stderr, "policy set: ino=%llu weight=%u flags=0x%x\n",
			(unsigned long long)key, val.weight, val.flags);
	}

	close(sock);
	close(map_fd);
	unlink(SOCK_PATH);
	return 0;
}
```

---

## 5.4 `agentctl.py`（创建 cgroup + 运行 tool + 下发 policy）

```python
#!/usr/bin/env python3
import argparse, os, stat, subprocess, socket, struct, time, pathlib

CGROOT = "/sys/fs/cgroup/agentcgroup"
SOCK_PATH = "/run/agentcgroupd.sock"

ACP_F_INTERACTIVE = 1 << 0

def write_file(path: str, s: str):
    with open(path, "w") as f:
        f.write(s)

def mkdir_p(path: str):
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)

def cgrp_inode(path: str) -> int:
    st = os.stat(path)
    return st.st_ino

def send_policy(ino: int, weight: int, interactive: bool):
    flags = ACP_F_INTERACTIVE if interactive else 0
    msg = struct.pack("QII", ino, weight, flags)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.sendto(msg, SOCK_PATH)
    sock.close()

def ensure_root():
    mkdir_p(CGROOT)
    # Enable controllers on parent if needed (best-effort; may require manual setup)
    # NOTE: depending on your distro/systemd, you may need to enable subtree_control at /sys/fs/cgroup
    try:
        write_file(os.path.join(CGROOT, "cgroup.subtree_control"), "+cpu +memory")
    except PermissionError:
        pass
    except OSError:
        pass

def create_session(sid: str):
    sp = os.path.join(CGROOT, f"sess_{sid}")
    mkdir_p(sp)
    # sensible defaults
    try:
        write_file(os.path.join(sp, "memory.oom.group"), "1")
    except OSError:
        pass
    try:
        write_file(os.path.join(sp, "cgroup.subtree_control"), "+cpu +memory")
    except OSError:
        pass
    return sp

def run_step(sid: str, cmd: str, weight: int, interactive: bool):
    sess = create_session(sid)
    tid = str(int(time.time() * 1000))
    step = os.path.join(sess, f"step_{tid}")
    mkdir_p(step)
    # Example memory envelope; tune as you like
    # write_file(os.path.join(step, "memory.high"), str(1024*1024*1024))  # 1GiB
    # write_file(os.path.join(step, "memory.max"),  str(2*1024*1024*1024)) # 2GiB

    # Launch process
    p = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid)
    # Move pid into cgroup
    write_file(os.path.join(step, "cgroup.procs"), str(p.pid))

    ino = cgrp_inode(step)
    send_policy(ino, weight, interactive)

    rc = p.wait()
    return rc

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="sub")

    runp = sub.add_parser("run")
    runp.add_argument("--sid", default="demo")
    runp.add_argument("--cmd", required=True)
    runp.add_argument("--weight", type=int, default=200)
    runp.add_argument("--interactive", action="store_true")

    args = parser.parse_args()
    ensure_root()

    if args.sub == "run":
        rc = run_step(args.sid, args.cmd, args.weight, args.interactive)
        raise SystemExit(rc)

    parser.print_help()

if __name__ == "__main__":
    main()
```

---

# 6. 你接下来怎么把它“补齐成论文级系统”

上面的最小原型已经把“**in-kernel CPU enforcement**”这条线跑通了（而且是可复现步骤）。下一步按论文需要补齐：

## 6.1 CPU：从 v0 走向论文中的“session/step 两级公平 + latency class”

你要做两件事：

1. **从 task-level vtime → cgroup-level（session/step）两级调度**
   参考 `tools/sched_ext/scx_flatcg`：它就是“实现 hierarchical cgroup CPU control”的例子（你可以用它当基座，而不是自己从零造树）。([Kernel Git Repositories][3])
   你的 session/step 树深度小（2 层），比通用 cgroup 树更容易实现。

2. **把“latency-sensitive step”做成可解释策略**
   不要只说“prioritize latency-sensitive”，要给：

* class 标记来源（runtime 提供 / uclamp 反映 / step 类型推断）
* anti-starvation 机制（budget / decay / max boost window）
* 伪代码（10–20 行足够）

## 6.2 Memory：先 mainline，后可选 memcg_bpf_ops

* mainline 版你就用：`memory.high/max + memory.events + freeze/kill`（语义完全在官方文档里可引用）。([Linux Kernel Documentation][1])
* 如果要“内核内可编程 delay”，就明确依赖 memcg_bpf_ops RFC patch，并在 artifact 放 patch；不要在摘要里写成“无需内核改动”。

## 6.3 “semantic mismatch”怎么写得系统论文味更浓？

建议用一句非常系统化的定义（你可以直接放 intro/motivation）：

> **Semantic mismatch:** the resource domains exposed by commodity stacks (environment/container-level) do not coincide with the operational unit that defines correctness and latency SLOs in agent systems (session/step). As a result, policies expressed at the environment boundary cannot be phase-aware, nor can they attribute pressure events to the responsible tool call with low latency.

然后把你的解决方案写成“**domain alignment + in-kernel enforcement**”两段式：

* domain alignment：cgroup 树是 session/step
* in-kernel enforcement：sched_ext/memcg hook 在 enforcement point 执行策略

---

# 7. 你现在这份工程设计还缺什么？（作为“手册/可运维”角度）

为了让它成为“OSDI/ATC artifact 级”工程，你还需要：

1. **System Model 文档**（谁创建 cgroup、谁负责把 pid 放进去、step 的定义是什么、失败语义是什么）
2. **Policy 接口规范**（字段、范围、默认值、版本兼容）
3. **可观测性**

   * sched_ext：用 ringbuf 输出调度统计（per step runtime、queue depth、stall）
   * memory：采集 `memory.events{high,max,oom,oom_kill}` 计数作为 controlled throttling vs hard failure 证据。([Linux Kernel Documentation][1])
4. **基线 & 评估矩阵**：static knobs / user-space controller / sched_ext(+optional memcg hook) + ablation

---

如果你愿意，我可以在你现有 LaTeX 的结构下，把 **Design & Implementation** 写成“论文可直接替换的三小节”（包含：系统模型图、数据结构表、CPU/MEM 伪代码、以及评价指标定义），并且把上面这套最小原型扩展成 **session/step 两级公平（基于 scx_flatcg 的改造 patch）**，让 CPU 侧的 story 变成真正“系统论文级”的可证明贡献。

[1]: https://docs.kernel.org/admin-guide/cgroup-v2.html "https://docs.kernel.org/admin-guide/cgroup-v2.html"
[2]: https://docs.kernel.org/scheduler/sched-ext.html "Extensible Scheduler Class — The Linux Kernel  documentation"
[3]: https://kernel.googlesource.com/pub/scm/linux/kernel/git/wireless/wireless/%2B/refs/heads/for-next/tools/sched_ext/ "https://kernel.googlesource.com/pub/scm/linux/kernel/git/wireless/wireless/%2B/refs/heads/for-next/tools/sched_ext/"


你这个问题抓得非常准：**“从 scx 出发怎么改、以及到底要在 eBPF 里写什么策略”**，决定了 AgentCgroup 的 CPU 侧贡献能不能落地、能不能讲清楚。

下面我按“从 scx 选起点 → 需不需要 sched_ext → eBPF 里该编程哪些策略 → 给一个可落地的策略 v0 + 在 scx 里怎么实现”的顺序给你一个工程化答案。

---

## 1) 先看 scx：你能从哪里“直接起步”？

scx 里有几条完全不同的路径，你要选的是**能和你 paper 的两个 mismatch 对齐**、而且改动成本可控的那条。

### A. `scx_flatcg`：最贴你“domain mismatch（session/step）”的基底

`scx_flatcg` 本身就是一个“cgroup-first”的 sched_ext scheduler：
它实现的是**层级 cgroup 权重**，并且通过“把层级权重复合成单层竞争（flatten hierarchy）”来减少多级树遍历成本；调度上是“先选 cgroup，再在 cgroup 内选 task（默认 weighted vtime，也可切 FIFO）”。([Hillion Gitea][1])

对 AgentCgroup 来说，这个非常像你要的：

* 你要把 **session / step 做成 cgroup 子树**；
* 然后在 CPU 侧实现 **session 间隔离 + step 粒度的动态策略**；
* `scx_flatcg` 已经把“以 cgroup 为一等公民”的骨架搭好了，你只需要把“agent-aware 的动态策略”塞进去。

**一句话**：如果你要把“资源域对齐到 session/step”写进设计核心，`scx_flatcg` 是最自然的出发点。

---

### B. `scx_bpfland`：最贴“interactive 优先”的策略模板

`scx_bpfland` 是一个偏 desktop/gaming 的交互优先调度器：
它把 task 分为 **interactive vs regular**，interactive 放高优先级队列；分类依据是“自愿上下文切换频率（voluntary ctx switches/s）”这种行为学启发式；队列内按 weighted runtime 排序，还带 time-slice budget 机制让短 burst 更容易跑完。([Sched Ext][2])

对 AgentCgroup 的启发是：

* 你其实不想用启发式（agent runtime 本来就知道哪个 step 是“latency-critical/interactive/tool burst”）；
* 所以你可以**保留 bpfland 的两级队列/切片/优先级骨架**，
* 把“是否 interactive”从**启发式**改成**显式标签（由 runtime/daemon 写入 BPF map，按 cgroup id 查）**。

这条路改动通常很小、很快能出结果（尤其适合先做一个可工作的 prototype + microbench）。

---

### C. `scx_lavd`：更硬核的 latency-aware “算法底座”

`scx_lavd` 是基于虚拟 deadline 的 LAVD 调度器，核心思路是：测量 task 的“latency criticality”，并在 deadline / slice / preemption 等决策中使用它；目标就是降低 tail latency（最初动机是 gaming）。([Sched Ext][3])

如果你想把论文 CPU 侧写得更“系统论文味儿”，LAVD 有几个对 agent 很贴的点（注意这些不是你要照搬，而是拿来改造）：

* 它自己也承认“让开发者标注 latency criticality 不总是可行”，所以才做了启发式；但 agent 场景**恰恰可行**——runtime 天生知道 step 的语义。([Hillion Gitea][4])
  → 你的贡献可以变成：**把 LAVD 的 latency criticality 输入从启发式替换为“agent step 语义标签”**，从而更准、更可控。
* 它对 fork-heavy 场景有防护（新 fork 的 task 分到更短 slice，缓解 fork bomb）。([Hillion Gitea][4])
  → 这和 agent 的 tool-call（编译/测试）非常契合。

如果你愿意投入多一点实现复杂度，`scx_lavd` 这条会更“学术”；但最短路径通常还是 `flatcg` 或 “bpfland + 显式语义标签”。

---

### D. `scx_layered`：完全不改 BPF 代码、先用配置做 baseline

`scx_layered` 是一个“多层（layer）+ 分类器”的混合 scheduler：可以按 cgroup 名称等条件把任务分层，并对层指定不同 policy（比如某层至少占 80% CPU util、是否可 preempt 其它层）。([Sched Ext][5])

这很适合你做**最小工程成本的 baseline**：

* 先用 cgroup path 规则把 `/agent/**` 归到一个高优先层；
* 把其他系统进程放低优先层；
* 再在 agent 内用更细 cgroup 做进一步层/权重。

它的缺点是：**层是“相对静态的 policy 框”**，你要做“step 内 phase-aware 的动态 fine-grain 策略”，最终还是会回到“自定义 BPF policy”。

---

## 2) 你问“需要 sched_ext 吗？有没有别的方案？”

### 如果你的目标是“in-kernel、μs 级反应、且策略可定制”，那 CPU 侧几乎一定要 sched_ext

因为 sched_ext 的定义就是：**用一组 BPF program 定义一个 scheduler class 的行为**，能动态启停，有 fail-safe 回退到默认 fair-class scheduler。([Kernel Documentation][6])

并且它支持 **partial switch**：设置 `SCX_OPS_SWITCH_PARTIAL` 后，只有显式设为 `SCHED_EXT` 的 task 会被 BPF scheduler 接管，其余 task 仍走 fair-class scheduler。([Kernel Documentation][6])
这对你这种“只想管 agent sandbox，不想接管整机”非常关键——部署风险和实验干扰都会小很多。

### 不用 sched_ext 的替代方案是什么？（以及为什么不够）

Linux cgroup v2 CPU controller 的主要接口包括 `cpu.max`、`cpu.max.burst`、`cpu.weight`、`cpu.weight.nice` 等。([Kernel Documentation][7])
但它们的问题是：

* 你想要的“tool-call/step 粒度动态切换 policy”，最终都要靠 user-space 去写这些 knob（控制环 10–100ms）；
* 而且 `cpu.max`（带宽/配额）文档明确：**只影响 fair-class scheduler**。([Kernel Documentation][7])
  也就是说：你要么留在默认 scheduler（失去 BPF 自定义决策点），要么上 sched_ext（就得自己处理配额/节流）。

### 重要更新：scx 生态已经把 `cpu.max` 语义“搬到 sched_ext 世界里”了

这是你 paper/story 很关键的一点：
scx 社区已经合并了对 `cpu.max` 的支持（PR #3026，2025-11-13 merged），它实现了一套 **cgroup CPU bandwidth control 库**，并集成到 LAVD；实现思路是“在 enqueue 做 admission control，把被 throttle 的任务放进 backlog；用 BPF timer 统一 replenishment，再把 backlog 重新入队”，从而避免在 `ops.dispatch()` 热路径额外开销。([GitHub][8])

这意味着：

* 你完全可以在 **sched_ext + scx** 的框架下同时拥有：

  * 权重公平（cpu.weight-like），以及
  * 带宽上限（cpu.max-like）
* 并且两者都能在内核路径里做快反应（不用 userspace loop）。

---

## 3) 关键问题：我们到底要在 eBPF 里编程什么“策略”？

把它拆成一句工程准则：

> **eBPF 里写“必须在调度热路径/事件路径上立即做决策的那部分”；把重计算/全局优化留给 user-space，只把参数写进 map。**

结合你的两个 mismatch，这就变成一个非常清晰的策略边界：

### 3.1 eBPF 必须做的（Data Plane / Fast Path）

这些是 user-space 永远做不到 μs 级的：

1. **在 enqueue/dispatch/tick/stopping 时刻做“即时决策”**

* 谁先跑（task/cgroup 选择）
* 给多长 slice
* 是否触发抢占（kick/yield 机制）
* 是否把任务“先放一边”（比如被 quota throttle 的 cgroup）

sched_ext 的 struct_ops 接口就是为这个准备的。([Kernel Documentation][6])

2. **把 agent 的语义映射为调度状态（semantic alignment）**

* key 不是 pid，而是 **cgroup id（session/step）**
* 你在 BPF 里按 cgroup id 查策略：class、boost、quota、burst credit…

这一步就是你 paper 的 “domain mismatch → in-kernel” 的闭环。

3. **实现 “dynamic / fine-grained” 的最小闭环**
   典型是两个机制：

* **Priority tiering（分层优先）**：比如 interactive step 的任务进高优队列；
* **Token bucket / budget（短 burst 允许、长 burst 抑制）**：例如编译 step 可以有 burst_credit，耗尽后快速降权/延迟。

4. **可选：实现 cpu.max 语义的内核侧节流**
   如果你要 hard containment，直接复用 scx 的 cgroup bandwidth library（#3026）是最省事、最“像系统论文”的做法。([GitHub][8])

---

### 3.2 eBPF 不应该做的（留给 User-space Control Plane）

* “全局最优”预算分配（比如 1000 个 session 的全局优化）
* LLM/agent 逻辑推断（phase detection、工具选择等）
* 复杂统计/模型训练

这些都应该由 daemon 完成，然后把结果写到 BPF map：**BPF 负责执行，daemon 负责算策略参数**。

---

## 4) 我建议你们在 scx 上落地的“AgentCgroup CPU 策略 v0”

目标：**最小改动 + 能把 paper 核心讲清楚 + 可做出扎实 eval 图**

### 4.1 资源域（domain）怎么映射？

你已经在写：session/step -> cgroup subtree。CPU 侧就沿用这个：

```
/sys/fs/cgroup/agent/
  sess_123/
    step_reason/
    step_compile/
    step_browser/
```

### 4.2 在 BPF 里维护什么 state？

以 cgroup id（`cgrp->kn->id`）为 key，维护一个 `step_policy`：

* `class`: {interactive, tool_burst, background}
* `base_weight`: 可选（默认用 cpu.weight/cgroup weight）
* `boost_weight` 或 `boost_factor`
* `slice_ns`: 不同 class 不同 slice（interactive 更短更频繁）
* `burst_credit_ns`: token bucket（允许短 burst）
* `quota`: 可选（直接复用 scx 的 cpu.max 库实现 cgroup quota）([GitHub][8])

### 4.3 在 BPF 热路径上做什么决策？

**（1）enqueue：决定入哪个队列、给多长 slice、是否带 vtime bias**

你从 `scx_flatcg` 出发会很顺：它在 `fcg_enqueue()` 里已经按 cgroup id 把任务 dispatch 到 cgroup DSQ（并支持 vtime）。([Hillion Gitea][1])
你只要加一段“根据 step_policy 调 slice + tvtime”。

**（2）stopping/tick：扣 token、上报 runtime，必要时触发 preemption**

* stopping：把本次执行时间 charge 到 burst_credit / quota
* tick：如果发现高优任务来了，触发 yield-based preemption（bpfland/lavd 里都有类似逻辑）([Sched Ext][2])

**（3）quota throttle（可选但强烈建议）**
直接按 scx cpu.max 库的思路：
enqueue 时如果 `scx_cgroup_bw_throttled(cgrp)` 为真，就 put_aside/backlog；timer replenish 后再 reenqueue。([GitHub][8])

这会让你的论文在“hard isolation”上非常扎实：你不是只会“调权重”，你能真正执行 cpu.max 语义。

---

## 5) “我们到底要在 eBPF 里写什么策略？”——给你一个可直接写进 paper 的回答模板

你可以把策略写成三段（这会非常像系统论文的“policy interface + mechanism”）：

1. **语义输入（from runtime）**
   step 生命周期（start/end），step class（interactive/tool/background），以及预算（weight/quota/burst）。

2. **内核侧机制（in BPF）**
   在 `enqueue/dispatch/tick/stopping` 这些 enforcement point 上：

* 以 cgroup 为单位做层级公平（继承 flatcg 的 hierarchical weight）
* 以 class 做优先队列 / deadline bias（bpfland/lavd 风格）
* 以 token bucket 做 burst 控制（允许短 burst，压制持续 burst）
* 以 admission control 实现 cpu.max（避免热路径检查开销）([GitHub][8])

3. **安全边界**
   sched_ext 可动态启停且有 fail-safe 回退；并可用 partial switch 只接管 agent task，减小 blast radius。([Kernel Documentation][6])

---

## 6) 工程落地：怎么从 scx repo 改出一个 `scx_agentcgroup`？

下面是“能照着做”的工程步骤（先做 CPU 侧，memory 侧你再接 memcg 那条线）。

### 6.1 环境前提

* Kernel 需要启用 `CONFIG_SCHED_CLASS_EXT` 等（scx repo 提供了建议 config）。([GitHub][9])
* 工具链：C schedulers 用 make；Rust schedulers 用 cargo；需要 clang、libbpf、bpftool 等依赖。([Hillion Gitea][10])

（你论文 artifact 里应该把 kernel 版本、config、clang/libbpf 版本写死。）

### 6.2 选 base：我建议先走 `scx_flatcg → scx_agentcgroup`

理由：它已经是“cgroup-first”的骨架，和你的 session/step domain 完全同构。([Hillion Gitea][1])

### 6.3 最小可跑改动：在 `fcg_enqueue()` 加一个 per-cgroup policy 查表

从 `scx_flatcg.bpf.c` 你可以看到它在 enqueue 里：

* 拿到 task 的 cgroup：`__COMPAT_scx_bpf_task_cgroup(p)`
* 以 `cgrp->kn->id` 作为 DSQ id
* 默认 slice 用 `SCX_SLICE_DFL`
* vtime 走 `scx_bpf_dispatch_vtime()`（非 fifo 模式）([Hillion Gitea][1])

你要加的核心就是：

* 定义 `step_policy_map`（key=CGID）
* 根据 policy 改 `slice` 和 `tvtime`（或者改 queue 选择）

#### 核心 BPF 代码片段（示意，尽量贴合 flatcg 的结构）

> 注意：这是“核心逻辑骨架”，你在 scx 里实现时要按它的 include/compat 宏调整类型与 helper。

```c
/* key: cgroup id (cgrp->kn->id) */
struct step_policy {
    __u32 class;          /* 0=normal,1=interactive,2=tool_burst */
    __u32 boost_factor;   /* e.g., 1..8 */
    __u64 slice_ns;       /* 0 => use SCX_SLICE_DFL */
    __u64 burst_credit_ns;
    __u64 burst_max_ns;
    __u64 last_refill_ns;
    __u64 refill_rate_ns_per_s;
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 65536);
    __type(key, __u64);              /* cgid */
    __type(value, struct step_policy);
} step_policy_map SEC(".maps");

static __always_inline struct step_policy *lookup_pol(struct cgroup *cgrp)
{
    __u64 cgid = cgrp->kn->id;
    return bpf_map_lookup_elem(&step_policy_map, &cgid);
}

static __always_inline __u64 pick_slice(struct step_policy *pol)
{
    if (!pol || !pol->slice_ns)
        return SCX_SLICE_DFL;
    return pol->slice_ns;
}

/* very simple token refill */
static __always_inline void refill_credit(struct step_policy *pol, __u64 now_ns)
{
    if (!pol) return;
    if (!pol->refill_rate_ns_per_s) return;

    __u64 elapsed = now_ns - pol->last_refill_ns;
    if ((s64)elapsed <= 0) return;

    /* refill = elapsed * rate / 1s */
    __u64 add = (elapsed * pol->refill_rate_ns_per_s) / 1000000000ULL;
    __u64 newc = pol->burst_credit_ns + add;
    pol->burst_credit_ns = newc > pol->burst_max_ns ? pol->burst_max_ns : newc;
    pol->last_refill_ns = now_ns;
}

static __always_inline bool want_boost(struct step_policy *pol)
{
    return pol && pol->class == 1 && pol->burst_credit_ns > 0;
}

/* In fcg_enqueue(): after you have cgrp + cgc */
{
    struct step_policy *pol = lookup_pol(cgrp);
    __u64 now = bpf_ktime_get_ns();
    __u64 slice = pick_slice(pol);

    if (pol)
        refill_credit(pol, now);

    if (fifo_sched) {
        scx_bpf_dispatch(p, cgrp->kn->id, slice, enq_flags);
    } else {
        __u64 tvtime = p->scx.dsq_vtime;

        /* cap idle budget like original flatcg does */
        if (vtime_before(tvtime, cgc->tvtime_now - slice))
            tvtime = cgc->tvtime_now - slice;

        /* simple boost: pull vtime forward while credit available */
        if (want_boost(pol)) {
            __u64 bias = slice * (pol->boost_factor ? pol->boost_factor : 2);
            tvtime = (tvtime > bias) ? (tvtime - bias) : 0;
        }

        scx_bpf_dispatch_vtime(p, cgrp->kn->id, slice, tvtime, enq_flags);
    }

    cgrp_enqueued(cgrp, cgc);
}
```

这段代码体现的策略非常清楚：

* **domain mismatch**：按 step cgroup 查 policy；
* **dynamic/fine-grained**：slice、vtime bias、burst credit 全是 per-step、可热更新；
* **timescale mismatch**：决策发生在 enqueue/dispatch 热路径，不靠 user-space loop。

### 6.4 再把 “hard quota” 补齐：复用 scx 的 cpu.max 库

如果你要把论文做硬：
CPU 侧建议把“cpu.max 语义”也做出来（否则审稿人会说你只能做软隔离）。

scx 社区已经给了库和 API：初始化/设置/检查 throttled/put_aside/reenqueue/consume 等一套。([GitHub][8])

落地方式通常是：

* `ops.init()` 调 `scx_cgroup_bw_lib_init()`
* `ops.cgroup_init/exit()` 调 `scx_cgroup_bw_init/exit()`
* `ops.enqueue()` 或 `ops.select_cpu()` 里先查 `scx_cgroup_bw_throttled()`；如果 throttled 就 `put_aside()`
* `ops.dispatch()` 里周期性 `reenqueue()`
* `ops.tick()` / `ops.stopping()` 里 `consume()` 上报用量 ([GitHub][8])

这一步基本就是把 “cpu.max 的 enforcement point” 也搬进 BPF ——和你 paper 的 story 完全一致。

---

## 7) 最后回到你提的“关键点”：策略要长什么样才算 AgentCgroup？

我建议你们把“我们在 eBPF 里编程的策略”收敛成一句可写进 abstract/intro 的定义：

> **AgentCgroup programs an agent-aware, cgroup-keyed scheduling policy in sched_ext: it uses step/session cgroups as domains, applies classed latency tiers + burst-credit control at enqueue/dispatch time, and (optionally) enforces cpu.max-equivalent quotas via in-kernel admission control—closing both the domain-granularity and control-loop-timescale mismatches.**

这句话背后对应的“策略形态”就三块：

1. **Class tiers（interactive/tool/background）**
   （bpfland/lavd 都有类似思想，但你用显式语义替代启发式。）([Sched Ext][2])

2. **Burst credit（允许短 burst、抑制长 burst）**
   （这是 agent tool-call 的关键；也是你能做出漂亮 tail latency 图的地方。）

3. **Quota（cpu.max）**
   （用 scx 已经合并的库，直接把“硬隔离”补齐。）([GitHub][8])

---

如果你下一步想继续推进，我建议你先做一个非常明确的最小里程碑（很像系统论文的 ablation）：

* **M1：flatcg + per-step policy map（只做 class + slice + vtime bias）**
  → 立刻能证明 domain mismatch + fine-grain。
* **M2：加上 cpu.max 库（admission control/backlog）**
  → 立刻能证明 hard containment + timescale mismatch。
* **M3：把“显式 agent 标签”接进 runtime（step start/end 时更新 map）**
  → 立刻能把 story 闭环。

你要是把你们的 cgroup 目录结构（session/step 命名规则）和你希望的三类 step（reasoning / compile / browser）的目标 SLO（比如 p99 < xx ms）给我，我可以把上面的 `step_policy` 字段、默认参数（slice、boost、refill）和实验 workload（fork storm、compile burst 等）进一步“定标”为一个能直接写进论文和 artifact 的配置表。

[1]: https://gitea.hillion.co.uk/JakeHillion/scx-upstream/raw/commit/b73e5052939d532239bd873904b1e4d51a456bd4/scheds/c/scx_flatcg.bpf.c "gitea.hillion.co.uk"
[2]: https://sched-ext.com/docs/scheds/rust/scx_bpfland "https://sched-ext.com/docs/scheds/rust/scx_bpfland"
[3]: https://sched-ext.com/docs/scheds/rust/scx_lavd "https://sched-ext.com/docs/scheds/rust/scx_lavd"
[4]: https://gitea.hillion.co.uk/JakeHillion/scx-upstream/src/commit/221f1fe12a5f4ff33e2ae8a909a10bde68e1200d/scheds/rust/scx_lavd/src/bpf/main.bpf.c "scx-upstream/scheds/rust/scx_lavd/src/bpf/main.bpf.c at 221f1fe12a5f4ff33e2ae8a909a10bde68e1200d - scx-upstream - Hillion Gitea"
[5]: https://sched-ext.com/docs/scheds/rust/scx_layered "https://sched-ext.com/docs/scheds/rust/scx_layered"
[6]: https://docs.kernel.org/scheduler/sched-ext.html "https://docs.kernel.org/scheduler/sched-ext.html"
[7]: https://docs.kernel.org/admin-guide/cgroup-v2.html "https://docs.kernel.org/admin-guide/cgroup-v2.html"
[8]: https://github.com/sched-ext/scx/pull/3026 "https://github.com/sched-ext/scx/pull/3026"
[9]: https://github.com/sched-ext/scx "https://github.com/sched-ext/scx"
[10]: https://gitea.hillion.co.uk/JakeHillion/scx-upstream/src/commit/c508d543371dbf98ba43b0541475b3866d29fcd3?utm_source=chatgpt.com "JakeHillion/scx-upstream - Hillion Gitea"
