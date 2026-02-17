# 完整计划：Per-Tool-Call Cgroup + Bash Wrapper + 双向资源协商

## 一、问题诊断：论文 claim 与实现的差距

### 1.1 论文声称什么

论文 Section 5（`main.tex:285`）写道：

> "AgentCgroup organizes resources using a hierarchical cgroup v2 structure where each agent workload maps to a cgroup node with tool calls as child nodes, enabling per-tool-call resource constraints while maintaining overall workload budgets."

还提到：

> "AgentCgroup executes control logic directly at kernel cgroup enforcement points via eBPF at the per-tool-call child cgroups described above"

### 1.2 实际实现是什么

`agentcgroupd.py` 的 `setup_cgroup_hierarchy()`（line 62-78）只创建了两个扁平 cgroup：

```
/sys/fs/cgroup/agentcg/
  session_high/    ← 所有进程被扔到这里
  session_low/
```

`handle_event()`（line 97-121）对 EXEC 事件只做一件事：把 PID 写进 `session_high`。没有任何 per-tool-call 的 child cgroup。

### 1.3 核心差距

| 论文 Claim | 实际实现 | 差距 |
|-----------|---------|------|
| tool calls as child nodes | 只有 session_high/session_low | 没有 child cgroup |
| per-tool-call resource constraints | 所有进程共享一个 cgroup | 没有隔离粒度 |
| detecting tool-call boundaries | handle_event 只分配 PID | 没有边界检测 |
| in-kernel enforcement at per-tool-call cgroups | BPF 附加在 session_high | 只有 session 级别 |

### 1.4 评估差距

当前评估（`trace_replay.py`）只验证了 HIGH vs LOW 的优先级隔离——即 session 之间的隔离。没有验证同一 agent 内 per-tool-call 的资源控制。

---

## 二、设计方案：Bash Wrapper + Per-Tool-Call Ephemeral Cgroup + 双向资源协商

### 2.1 核心思路

每个 bash tool call 获得自己的 child cgroup。通过 bash wrapper 透明拦截，不需要修改 agent 框架。

### 2.2 Cgroup 层级结构

```
/sys/fs/cgroup/agentcg/
  session_high/                          ← agent session（已有）
    cgroup.subtree_control: +memory +cpu ← 新增：启用子树控制
    tool_<pid>_<ts>/                     ← 新增：per-tool-call ephemeral cgroup
      memory.peak                        ← 可读取峰值内存
      memory.current                     ← 实时内存
      cgroup.procs                       ← 工具进程及其子进程
    tool_<pid>_<ts>/                     ← 另一个工具调用
  session_low/                           ← 低优先级 session（已有）
```

关键点：
- memcg_bpf_ops 附加在 `session_high` 上，cgroup v2 层级继承使其自动对所有 child cgroup 生效
- 不需要改 BPF 代码
- child cgroup 生命周期与 tool call 严格对齐

### 2.3 Bash Wrapper 工作流

```
Claude Code 发出: bash -c "pytest tests/"
  → wrapper 被调用（替代原 bash）
  → 解析 resource hint（如果有环境变量声明）
  → 创建 child cgroup: session_high/tool_<pid>_<timestamp>/
  → 把自身 PID 写入 child cgroup
  → 执行 /usr/bin/real-bash -c "pytest tests/"
  → 命令结束
  → 如果 exit code 137 (OOM)：往 stderr 写资源反馈
  → 记录资源使用到日志
  → 清理 child cgroup
```

### 2.4 双向资源协商（Bidirectional Resource Negotiation）

**这是传统工作负载完全不具备的特性。** Agent 不只是被动承受资源限制——它是一个有决策能力的智能体，可以：

1. **系统→Agent（下行反馈）**：当资源不足时，wrapper 通过 stderr 告诉 agent 发生了什么、建议怎么做
2. **Agent→系统（上行声明）**：agent 在发起 tool call 时，通过环境变量声明资源需求 hint

#### 2.4.1 下行反馈：系统→Agent

当 tool call 遇到资源问题时，wrapper 在 stderr 注入语义化反馈：

```
[Resource] Command killed (OOM, exit 137). Peak memory: 1.8GB.
[Resource] This command exceeded the available memory budget.
[Resource] Suggestions: run more targeted tests (e.g., pytest tests/test_specific.py
  instead of pytest tests/), or reduce data size, or split into smaller operations.
```

这不是简单的"告诉应用资源紧张"（PSI notification 也能做）。关键区别在于：
- PSI 是一个数字信号，传统应用无法理解其含义
- Agent 能理解自然语言建议，并做出**语义层面的行为调整**（跑 specific test 而不是 full suite）

#### 2.4.2 上行声明：Agent→系统

Agent 可以在 tool call 中通过环境变量声明资源需求：

```bash
# Agent 在 tool call 的命令中可以设置环境变量
AGENT_RESOURCE_HINT="memory:high" bash -c "pytest tests/"
AGENT_RESOURCE_HINT="memory:low"  bash -c "git status"
AGENT_RESOURCE_HINT="memory:2g"   bash -c "python train.py"
```

Wrapper 解析这些 hint，据此设置 child cgroup 的 `memory.high`：

| Hint | memory.high 设置 | 含义 |
|------|-----------------|------|
| `memory:low` | 256MB | Agent 认为这是轻量操作 |
| `memory:medium`（默认） | 1GB | 普通操作 |
| `memory:high` | 不设限 | Agent 认为这需要大量内存 |
| `memory:<N>g` | N GB | Agent 精确声明需要的量 |

**为什么这可以工作？**

Agent 对即将执行的命令有语义理解。它知道：
- `git status` 几乎不需要内存 → `memory:low`
- `pytest tests/` 可能需要较多内存 → `memory:high`
- `python -c "import pandas; df = pd.read_csv('large.csv')"` → `memory:2g`

这种声明不需要精确——它是一个 hint，系统可以据此做更好的资源分配决策。如果 agent 低估了，系统的下行反馈机制会告诉它，agent 可以在下次调整 hint。

#### 2.4.3 协商闭环

```
轮次 1：
  Agent: AGENT_RESOURCE_HINT="memory:medium" bash -c "pytest tests/"
  系统: [OOM, exit 137, peak 1.8GB] → stderr 反馈

轮次 2：
  Agent 读到反馈，理解需要更多内存或更小范围
  选择 A: AGENT_RESOURCE_HINT="memory:2g" bash -c "pytest tests/"      ← 请求更多资源
  选择 B: bash -c "pytest tests/test_specific.py -x"                    ← 缩小操作范围

轮次 3：
  成功完成
```

这是一个自适应的协商循环：
- Agent 拥有语义知识（什么命令做什么）
- 系统拥有资源知识（有多少可用、谁在竞争）
- 双方通过简单协议交换信息，共同达到最优执行

---

## 三、实现细节

### 3.1 bash_wrapper.sh

**位置**：`agentcg/bash_wrapper.sh`

**安装方式**：在容器中，把原 bash 移到 `/usr/bin/real-bash`，wrapper 安装为 `/usr/bin/bash`。或通过 bind mount 覆盖。

```bash
#!/usr/bin/real-bash
# AgentCgroup Bash Wrapper
# 为每个 bash -c 调用创建 per-tool-call ephemeral cgroup

REAL_BASH="/usr/bin/real-bash"
CGROUP_ROOT="${AGENTCG_ROOT:-/sys/fs/cgroup/agentcg/session_high}"
TOOL_CG="$CGROUP_ROOT/tool_$$_$(date +%s%N)"
LOG_FILE="${AGENTCG_LOG:-/tmp/agentcg_tools.jsonl}"

# 非 -c 调用直接透传（交互 bash、source 脚本等）
if [ "$1" != "-c" ]; then
    exec "$REAL_BASH" "$@"
fi

# 解析 resource hint
HINT="${AGENT_RESOURCE_HINT:-medium}"
MEM_HIGH="max"
case "$HINT" in
    memory:low)    MEM_HIGH=$((256 * 1024 * 1024)) ;;
    memory:medium) MEM_HIGH=$((1024 * 1024 * 1024)) ;;
    memory:high)   MEM_HIGH="max" ;;
    memory:*g)
        NUM="${HINT#memory:}"
        NUM="${NUM%g}"
        MEM_HIGH=$(echo "$NUM * 1024 * 1024 * 1024" | bc 2>/dev/null || echo "max")
        ;;
    *)             MEM_HIGH="max" ;;
esac

# 创建 per-tool-call cgroup
mkdir -p "$TOOL_CG" 2>/dev/null
if [ $? -eq 0 ]; then
    # 设置 memory.high（如果有 hint）
    if [ "$MEM_HIGH" != "max" ]; then
        echo "$MEM_HIGH" > "$TOOL_CG/memory.high" 2>/dev/null
    fi
    # 把自身移入 child cgroup
    echo $$ > "$TOOL_CG/cgroup.procs" 2>/dev/null
    IN_CG=1
else
    IN_CG=0
fi

# 记录开始时间
START_NS=$(date +%s%N 2>/dev/null || echo 0)

# 执行实际命令
"$REAL_BASH" "$@"
EXIT_CODE=$?

# 记录结束时间
END_NS=$(date +%s%N 2>/dev/null || echo 0)

# 收集资源数据
PEAK_MEM="unknown"
CURRENT_MEM="unknown"
if [ "$IN_CG" = "1" ]; then
    PEAK_MEM=$(cat "$TOOL_CG/memory.peak" 2>/dev/null || echo "unknown")
    CURRENT_MEM=$(cat "$TOOL_CG/memory.current" 2>/dev/null || echo "unknown")
fi

# OOM 反馈（下行：系统→Agent）
if [ $EXIT_CODE -eq 137 ]; then
    PEAK_MB="unknown"
    if [ "$PEAK_MEM" != "unknown" ]; then
        PEAK_MB=$((PEAK_MEM / 1024 / 1024))
    fi
    echo "[Resource] Command killed (OOM, exit 137). Peak memory: ${PEAK_MB}MB." >&2
    echo "[Resource] The command exceeded the available memory budget." >&2
    echo "[Resource] Suggestions: run more targeted operations (e.g., specific test" >&2
    echo "  files instead of full test suite), reduce data size, or split into" >&2
    echo "  smaller steps. You can also request more memory by setting" >&2
    echo "  AGENT_RESOURCE_HINT=\"memory:<size>g\" before the command." >&2
fi

# 记录到日志（JSON Lines 格式）
if [ "$IN_CG" = "1" ]; then
    DURATION_MS=$(( (END_NS - START_NS) / 1000000 ))
    CMD_PREVIEW=$(echo "$2" | head -c 200)
    echo "{\"ts\":$START_NS,\"pid\":$$,\"cgroup\":\"$TOOL_CG\",\"cmd\":\"$CMD_PREVIEW\",\"exit\":$EXIT_CODE,\"duration_ms\":$DURATION_MS,\"peak_mem\":\"$PEAK_MEM\",\"hint\":\"$HINT\"}" >> "$LOG_FILE" 2>/dev/null
fi

# 清理：移回父 cgroup，删除 child cgroup
if [ "$IN_CG" = "1" ]; then
    echo $$ > "$CGROUP_ROOT/cgroup.procs" 2>/dev/null
    rmdir "$TOOL_CG" 2>/dev/null
fi

exit $EXIT_CODE
```

### 3.2 agentcgroupd.py 修改

**改动点 1**：`setup_cgroup_hierarchy()` 启用 subtree_control

```python
def setup_cgroup_hierarchy(root: str) -> bool:
    high = os.path.join(root, "session_high")
    low = os.path.join(root, "session_low")

    if not cgroup_create(high) or not cgroup_create(low):
        return False

    # 在 root 启用子控制器
    cgroup_write(root, "cgroup.subtree_control", "+memory +cpu")

    # 新增：在 session_high 也启用 subtree_control，允许 per-tool-call child cgroup
    cgroup_write(high, "cgroup.subtree_control", "+memory +cpu")

    cgroup_write(high, "cpu.weight", "150")
    cgroup_write(low, "cpu.weight", "50")

    log.info("Cgroup hierarchy ready at %s (subtree_control enabled for per-tool-call)", root)
    return True
```

**改动点 2**：`handle_event()` 记录 tool call 事件（而不是分配 cgroup，因为 wrapper 自己做了）

```python
def handle_event(event: dict, cgroup_root: str) -> None:
    event_type = event.get("event")
    pid = event.get("pid")
    comm = event.get("comm", "?")

    if event_type == "EXEC":
        # wrapper 会自己创建 child cgroup 并移入
        # daemon 这边只记录事件
        log.info("EXEC: %s (%d) - tool call detected", comm, pid)

    elif event_type == "EXIT":
        duration = event.get("duration_ms")
        extra = f" (duration={duration}ms)" if duration else ""
        log.info("EXIT: %s (%s)%s", comm, pid, extra)
```

**改动点 3**：新增 `scan_tool_cgroups()` 方法，daemon 可以扫描并监控 child cgroup

```python
def scan_tool_cgroups(self) -> list:
    """扫描 session_high 下的 per-tool-call child cgroup"""
    high_path = os.path.join(self.cgroup_root, "session_high")
    tool_cgroups = []
    try:
        for entry in os.scandir(high_path):
            if entry.is_dir() and entry.name.startswith("tool_"):
                tool_cgroups.append(entry.path)
    except OSError:
        pass
    return tool_cgroups
```

### 3.3 memcg_controller.py 修改

在 `CgroupMemcgController.poll()` 中增加对 child cgroup 的扫描和监控：

```python
def poll(self) -> None:
    if not self._config:
        return

    # 原有逻辑：检测压力、管理保护窗口
    # ...（保持不变）

    # 新增：扫描 per-tool-call child cgroup，对新出现的设置默认 memory.high
    self._manage_tool_cgroups()

def _manage_tool_cgroups(self) -> None:
    """扫描 session_high 下的 child cgroup，对新出现的设置资源限制"""
    if not self._config:
        return
    high_path = self._config.high_cgroup
    try:
        for entry in os.scandir(high_path):
            if entry.is_dir() and entry.name.startswith("tool_"):
                cg_path = entry.path
                if cg_path not in self._known_tool_cgroups:
                    self._known_tool_cgroups.add(cg_path)
                    # daemon 可以在这里对 child cgroup 设置额外限制
                    # wrapper 已经根据 hint 设了 memory.high
                    # daemon 可以根据全局压力状况做进一步调整
                    log.debug("New tool cgroup detected: %s", cg_path)
    except OSError:
        pass
```

### 3.4 run_swebench.py 修改

在容器启动命令中安装 wrapper：

```python
# 在 cmd_script 开头添加 wrapper 安装
cmd_script = f'''
# Install bash wrapper for per-tool-call cgroup
if [ -f /tmp/agentcg/bash_wrapper.sh ]; then
    cp /usr/bin/bash /usr/bin/real-bash
    cp /tmp/agentcg/bash_wrapper.sh /usr/bin/bash
    chmod +x /usr/bin/bash
    export AGENTCG_ROOT="/sys/fs/cgroup/agentcg/session_high"
    export AGENTCG_LOG="/tmp/agentcg_tools.jsonl"
fi

git config user.email "test@test.com"
...
'''

# 在容器 mount 中添加 wrapper 文件
container_cmd.extend([
    "-v", f"{wrapper_path}:/tmp/agentcg/bash_wrapper.sh:ro",
])
```

---

## 四、评估计划

### 实验 1：Wrapper 开销微基准测试

**目的**：证明 wrapper 的额外开销可以忽略。

**方法**：
- 跑 1000 次 `bash -c "echo hello"`：有 wrapper vs 无 wrapper
- 跑 100 次 `bash -c "git status"`：有 wrapper vs 无 wrapper
- 测量每次的额外延迟（cgroup mkdir + echo PID + rmdir）

**预期**：< 5ms overhead per tool call（cgroup 操作是内核 syscall，很快）

**脚本**：`bench/microbench_wrapper.sh`

### 实验 2：Per-Tool-Call 资源可见性

**目的**：验证 per-tool-call cgroup 能准确测量每个工具调用的资源。

**方法**：
- 在真实 agent 执行中启用 wrapper
- 选 5-10 个任务（覆盖不同类别：test execution, git ops, package install）
- 收集每个 tool call 的 `memory.peak`、`memory.current` 时序
- 对比 characterization 阶段从 container-level trace 推断的 per-tool-call 数据 vs wrapper 直接测量的数据

**产出**：per-tool-call 资源使用分布图

### 实验 3：Per-Tool-Call Limits 减少资源浪费

**目的**：验证 per-tool-call limits 比 container-level limits 更高效。

**方法**：
- 多个 agent 并发，总内存受限
- 对比条件：
  - Baseline：container-level `memory.high`
  - Per-tool-call：每个 child cgroup 独立 `memory.high`
- 用 trace replay 做（和现有评估基础设施一致）

**Metrics**：OOM 次数、资源利用率、高优先级 agent P95 延迟

### 实验 4：双向资源协商效果

**目的**：验证上行 hint + 下行反馈的闭环能改善 agent 在受限环境下的表现。

**方法**：
- Agent 在 1GB 内存限制下执行任务
- 对比条件：
  - A) 无反馈：OOM 时 agent 只看到 "Killed"
  - B) 仅下行反馈：OOM 时 wrapper 输出语义化建议
  - C) 双向协商：agent 声明 hint + OOM 时收到反馈
- 选 10-15 个会触发 OOM 的任务

**Metrics**：任务完成率、重试次数、总执行时间、peak memory utilization

**这个实验需要 live agent 执行。**

### 实验 5：Resource Hint 准确性分析

**目的**：量化 agent 的 resource hint 是否有价值。

**方法**：
- 在实验 4 的基础上，分析 agent 声明的 hint vs 实际资源使用的匹配程度
- 计算 hint 的准确率（低估/高估/匹配）
- 分析 hint 准确率与任务成功率的相关性

---

## 五、论文修改计划

### 5.1 Design Section 扩展

现有的三段话扩展为完整的 subsection 结构：

```
5.1 Per-Tool-Call Resource Domains
  5.1.1 Bash Wrapper as Agent-OS Bridge
    - wrapper 机制描述
    - 如何透明地为每个 tool call 创建 ephemeral cgroup
    - overhead 分析（实验 1）

  5.1.2 Hierarchical Cgroup Structure
    - session → tool call 的两层结构
    - 每个 tool call 独立的 memory.high / memory.peak 监控
    - cgroup lifecycle: create on exec, destroy on exit
    - BPF 层级继承：不需要改 BPF 代码

  5.1.3 Bidirectional Resource Negotiation
    - 下行反馈：OOM 时 wrapper 注入语义化反馈
    - 上行声明：agent 通过环境变量声明 resource hint
    - 协商闭环：agent 理解反馈 + 调整策略 + 声明新 hint
    - 与传统方案的区别（见下文讨论）

5.2 In-Kernel Enforcement（已有内容，微调）

5.3 Runtime-Adaptive Policies（已有内容，微调）
```

### 5.2 新增讨论：Semantic Elasticity 与双向协商

在 Design 或 Discussion 中加一段：

**核心论点**：Agent 工作负载有一个传统工作负载完全不具备的属性：**语义弹性（semantic elasticity）**。

传统资源管理是单向的：系统施加限制，应用被动承受。PSI notification 可以告诉应用"资源紧张了"，但应用能做的回应非常有限——要么降低质量（视频降码率），要么排队等待。这些回应都是在**同一个执行策略**内的参数调整。

Agent 不同。它同时具备：
1. **语义理解**：知道"跑 pytest tests/"和"跑 pytest tests/test_specific.py -x"在语义上达到类似目的，但资源消耗差几个数量级
2. **策略生成**：能根据自然语言反馈重新规划执行策略
3. **需求预判**：对即将执行的命令有先验知识（知道 git status 是轻量的，pytest 可能很重）

基于这三个属性，资源管理可以从单向控制变成**双向协商**：

- **Agent→系统**：agent 声明 resource hint（"我接下来要跑 pytest，可能需要 2GB 内存"），系统据此做更精准的资源预分配和调度决策
- **系统→Agent**：当资源不足时，系统不仅告知"OOM了"，还用自然语言建议替代策略（"请尝试跑更小范围的测试"），agent 能理解并执行

这不是简单的"让应用感知资源"——传统应用感知资源后只能做参数调整（线程数、buffer 大小）。Agent 可以做**语义层面的策略重构**：换一种完全不同的方法来达到同样的目标。

### 5.3 Evaluation Section 扩展

加入实验 1-5 的结果：

1. Wrapper overhead → 证明方案开销可忽略
2. Per-tool-call 资源可见性 → 核心 claim 的直接验证
3. Per-tool-call limits → 比 container-level 更高效
4. 双向协商效果 → 最 novel 的结果
5. Hint 准确性分析 → 理解 agent 的资源感知能力

---

## 六、修改的文件清单

| 文件 | 改动类型 | 描述 |
|------|---------|------|
| `agentcg/bash_wrapper.sh` | **新文件** | Bash wrapper：per-tool-call cgroup 创建/销毁 + 双向协商 |
| `agentcg/agentcgroupd.py` | 修改 | `setup_cgroup_hierarchy` 启用 subtree_control；`handle_event` 改为记录事件 |
| `agentcg/memcg_controller.py` | 修改 | `poll()` 中扫描 child cgroup；新增 `_manage_tool_cgroups()` |
| `scripts/run_swebench.py` | 修改 | 容器内安装 wrapper（bind mount + PATH 设置） |
| `agentcg/memcg/multi_tenant_test/trace_replay.py` | 修改 | 扩展支持 per-tool-call 模式的 trace replay |
| `bench/microbench_wrapper.sh` | **新文件** | Wrapper 开销微基准测试脚本 |
| `bench/eval_bidirectional.py` | **新文件** | 双向协商评估脚本 |
| `paper-repo/main.tex` | 修改 | Design section 扩展、新增实验结果、讨论 |

---

## 七、实现顺序

**阶段 1（1 天）**：核心实现
1. 编写 `bash_wrapper.sh`
2. 修改 `agentcgroupd.py`（subtree_control + handle_event）
3. 修改 `memcg_controller.py`（child cgroup 扫描）
4. 实验 1：overhead microbenchmark

**阶段 2（1 天）**：集成与基础评估
5. 修改 `run_swebench.py`（容器内安装 wrapper）
6. 实验 2：per-tool-call 资源可见性（live agent, 5-10 个任务）
7. 实验 3：per-tool-call limits vs container limits（trace replay）

**阶段 3（1-2 天）**：双向协商评估
8. 实验 4：双向资源协商效果（live agent, 10-15 个任务）
9. 实验 5：resource hint 准确性分析

**阶段 4（并行）**：论文修改
10. Design section 扩展
11. Evaluation section 扩展
12. 新增讨论：Semantic Elasticity

---

## 八、风险与备选方案

### 风险 1：cgroup subtree_control 与 BPF 继承

**风险**：在 `session_high` 上启用 `subtree_control` 后，进程不能直接驻留在 `session_high` 中（cgroup v2 "no internal processes" 约束），所有进程必须在 child cgroup 中。

**缓解**：wrapper 确保所有 `bash -c` 调用都移入 child cgroup。对于非 bash 进程（如 Node.js agent runtime），可以创建一个 `tool_framework/` 常驻 child cgroup。

### 风险 2：wrapper 对非 tool-call bash 调用的影响

**风险**：容器内可能有其他 bash 调用不应被 wrapper 拦截。

**缓解**：wrapper 只对 `bash -c "..."` 模式生效，交互式 bash 和其他调用模式直接透传。

### 风险 3：resource hint 被 agent 忽略

**风险**：Claude Code 不一定会设置 `AGENT_RESOURCE_HINT` 环境变量。

**缓解**：hint 是可选的。没有 hint 时使用默认值（`memory:medium`）。即使没有上行声明，下行反馈仍然有效。评估时可以通过修改 prompt 引导 agent 使用 hint。

### 风险 4：cgroup 创建/删除的并发问题

**风险**：多个 tool call 并发时，cgroup 操作可能有竞争。

**缓解**：每个 wrapper 使用唯一的 cgroup 路径（`tool_<pid>_<timestamp>`），不会冲突。rmdir 在 cgroup 非空时会失败，但这是安全的——意味着还有子进程在运行。
