你描述的这条线（在 *SWE-bench 类*任务上，把“生产级 coding agent（如 Claude Code）”当黑盒跑起来，并把**端到端全量 trace**（prompt / 工具调用 / 时间戳 / 成本 / 资源占用 / 失败原因）系统化收集下来）——**不是空白**，但也**远远没被做“标准化 + 大规模 + 可复现 + 可公开”的程度**。所以：

* **“有人做吗？”**：有，而且碎片化地做了很多；但你想要的“全量 trace + 资源指标 + 大规模 + 公开数据/可复现”这一档，公开领域里仍然有明显空位。
* **“能发论文吗？”**：能，但前提是你不是写成“我们跑了 Claude Code 并把日志存起来”。你要把它写成：**新数据 + 新度量/新方法 + 新洞察（最好还能带来可操作改进）**。
* **“novelty 怎么 claim？”**：核心是把“trace”当成研究对象（measurement + methodology + systems/SE insight），而不是当成附带产物。下面我按你问的三个点来拆。

---

## 1) 现在公开领域已经有哪些“类似工作”？

### A. 纯轨迹数据集：已经很大规模（但通常缺系统级时间/资源）

公开界最接近你想要的，是一批“agent trajectories”数据集：把每一步的 **thought/action/observation**、tool calls、最终 diff、测试日志等存下来。

* **nebius/SWE-agent-trajectories**：声称包含 **80,036** 条轨迹，字段里有 `trajectory`（包含系统提示在内的日志）、最终 patch、以及 `eval_logs`（跑测试的日志）等。它明确把“轨迹”作为训练数据来收集。 ([Hugging Face][1])
* **nebius/SWE-rebench-openhands-trajectories**：更“工程化”的 OpenHands 轨迹集，给出 **67,074** 条轨迹，并且字段里有完整 `trajectory`（system/assistant/user/tool 四类 message），以及 patch、是否 resolved。它还强调了一个差异点：**额外评估了 agent 生成的 tests**，并声称“据其所知，没有其他公开轨迹集包含 agent-generated tests 的评估”。 ([Hugging Face][2])

这类数据集的共同点：**内容级 trace 很全**（模型输出、工具调用、环境观测、最终 diff、测试日志）。但常见缺点是：

* **时间戳**不统一/不完整（有时根本没有 per-step wall time）；
* **资源占用（CPU/mem/IO/GPU）**基本没有；
* **“生产级 agent（Claude Code/Cursor）”**的 trace 往往不公开或只提供部分。

### B. 轨迹“格式/可观测性”在产品侧其实已经具备：Claude Code 自带 OpenTelemetry

你提到的“完整时间戳、工具调用、资源占用”这件事，在 Claude Code 生态里其实已经有官方的可观测性接口：

* Claude Code 官方文档明确：它通过 OpenTelemetry 导出 metrics + events。

  * Metrics 里有 `claude_code.cost.usage`（美元成本）、`claude_code.token.usage`（token 计数）、`claude_code.active_time.total` 等； ([Claude Code][3])
  * Events 里有 `claude_code.user_prompt`（含 **ISO 8601 时间戳**、序号、prompt 长度；prompt 内容默认脱敏，可用 `OTEL_LOG_USER_PROMPTS=1` 打开）、`claude_code.tool_result`（含 **duration_ms**、success、error、tool_parameters 等）、`claude_code.api_request`（含 cost_usd、duration_ms、input/output/cache tokens）等。 ([Claude Code][3])

这意味着：**你要的“prompt + tool calls + 完整时间戳 + 每次 API request 的耗时与 token/cost”**，Claude Code 官方链路上已经支持得很扎实。缺的是：

* 你额外想要的 **CPU/memory/IO/GPU 等资源占用**（OTel 里没直接给出，需要你自己在容器/宿主机层面补 span/metric）；
* 以及把这些 telemetry 与 SWE-bench 评测 harness 对齐、做成可复现的数据资产。

此外也有第三方/开源工具链在接 Claude Code 的 trace：

* LangSmith 给了“Trace Claude Code”的集成思路：通过 Stop hook 读取 Claude Code 的 transcript，把 user/tool/assistant 的内容发到 LangSmith；并明确指出 **系统 prompt 不会从 Claude Code transcript 返回，因此 trace 里不包含 system prompts**。 ([LangChain Docs][4])
* MLflow 的 Claude Code tracing 页面也明确：自动捕获 user/assistant、tool usage、**conversation timing and duration**、tool execution results、token usage、session metadata 等。 ([MLflow][5])
* Honeycomb 也写了如何用 OTel 把 Claude Code 的 metrics/logs 导进去做 ROI/usage 分析。 ([Honeycomb][6])

所以：**“收集完整 trace”在 Claude Code 上技术上不是难点**，难点是“把它变成研究贡献”。

### C. “用 Claude Code 做 testbed 并公开轨迹”的研究/数据：已经出现，但规模不大、任务不等价于 SWE-bench

你问的是 SWE-bench 类任务；但至少已经有人用 Claude Code 作为 testbed 并公开“完整轨迹”：

* **CC-Bench-trajectories（zai-org）**：说明“用 Claude Code 作为 agentic coding testbed”，包含 **74 个 coding tasks**，并提供字段：`trajectory`（完整 Claude Code 交互轨迹）、总 input/output tokens、tool_calls、tool_failures 等。 ([Hugging Face][7])

这个数据集证明：**公开 Claude Code 轨迹**是可行的。但它不是 SWE-bench；而且评测过程看起来还有人工多轮交互（不是纯自动化 harness）。 ([Hugging Face][7])

### D. 学术界已经开始“把轨迹当研究对象”来发论文

如果你担心“轨迹收集算不算 research”，这里有两个方向已经在发：

1. **软件工程/实证方向**：直接研究轨迹

* 《Understanding Software Engineering Agents: A Study of Thought-Action-Result Trajectories》把“agent trajectories”定义为 thought-action-result triples，并明确说“目前没有成熟方法系统化研究它们”，然后做了跨 agent 的统一格式、统计分析、序列模式挖掘和质性分析（成功 vs 失败）。 ([arXiv][8])

2. **系统方向**：用轨迹（含时间信息）做推理系统优化

* 《Continuum》这类系统论文会“收集并分析 agent traces”，并用 server-side 时间戳计算 inter-request interval（本质上就是 tool latency 的观测），来做 tool-call-aware 调度与 KV cache pinning。它明确提到：收集了 500 条 mini-swe-agent 跑 SWE-Bench/BFCL 的 traces，并研究 tool call 执行时间分布。 ([arXiv][9])

换句话说：**轨迹/trace 本身已经足够成为论文的对象**，前提是你给出方法论、指标和可复现的结论。

### E. 生产 agent 在 SWE-bench(-Pro) 上的大规模跑分：有人在做（但未必公开全量 trace）

你问“生产环境 agent（Claude Code）大规模跑 SWE-bench 类任务”——至少在公开报道里，确实有人这么做：

* Augment Code 的文章写到：他们在 Scale AI 的 **SWE-bench Pro（731 problems）**上跑了 Auggie、Cursor、Claude Code、Codex，并强调“同一个底座模型（Opus 4.5）因为 agent architecture 不同分数差很多”。 ([Augment Code][10])

这类文章不会给你“全量 trace 数据集”，但它说明：**生产 agent 大规模跑类似 SWE-bench 的评测**，在业界已经是常规动作，只是数据公开与复现往往缺失。

---

## 2) 你想做的“完整 trace”项目：如果只是收集日志，novelty 很弱；但如果你把它做成“可复现测量科学”，novelty 是实在的

### 先把话说死：

**“我们跑了 Claude Code，然后存了 prompt+tool calls+timestamp”**——这基本就是工程工作，审稿人会问：

* 这和现有 trajectory 数据集（SWE-agent/OpenHands）相比新在哪里？ ([Hugging Face][1])
* Claude Code 官方都能导出事件与成本/耗时，你只是把它打开了开关而已。 ([Claude Code][3])
* 你没有提出新的指标、没有因果解释、没有带来可操作改进，那就是一堆日志。

所以你要的不是“收集”，而是**把收集变成研究贡献**。下面是我建议的三种“硬核 claim 路线”（你可以选一种作为主线，其余作为副贡献）。

---

## 3) 可以发什么类型的论文？建议把你的工作对齐到三条主线之一

### 路线 1：Systems/MLSys —— “Trace-driven agent serving / cost-latency frontier”

如果你把 trace 采集做到 per-step 时间戳 + tool duration + token/cost，再加上 CPU/mem/IO（容器级），你就能做一整套“系统测量 + 优化”的论文。Continuum 已经证明：**tool-call 的时间结构本身就能支撑系统创新**。 ([arXiv][9])

你能讲的核心问题包括：

* 工具调用造成的**等待/空泡**在端到端时间里占比多少？（Claude Code 已提供 tool_result duration_ms、api_request duration_ms） ([Claude Code][3])
* 在固定预算（$ 或 tokens 或 wall-time）下，哪种策略更优：多轮短思考 vs 少轮长思考？
* 什么时候值得 rerun tests？什么时候应该 early-stop？
* tool latency variance 对成功率的影响，以及如何做动态 timeout / retry policy？（Claude Code 的 tool_result 事件里就有成功/失败与 duration_ms） ([Claude Code][3])

**论文贡献可以很硬**：提出 trace-aware scheduling/budgeting/cache/parallel tool execution 之类的机制，并用真实 SWE-bench(-Pro) traces 驱动评估。

**潜在 venue**：MLSys / EuroSys / NSDI / OSDI（更系统）、ICLR/NeurIPS 的 systems track 也可能吃。

### 路线 2：SE/Empirical —— “A measurement study of production coding agents”

如果你更像在做“软件工程实证研究”，那就把重点放在：

* 生产 agent（Claude Code/Cursor/…）与研究 scaffolds（SWE-agent/OpenHands）的**行为差异**
* 成功/失败的 trace pattern、反模式（anti-pattern）
* 以及把这些规律总结成可验证的设计建议

这在 SE 圈是可发表的：上面那篇轨迹研究就把“没有成熟方法论”当作切入点。 ([arXiv][8])
你的优势在于：你要研究的是“生产级 agent”，而不是论文里那套实验 harness。

**你需要做的**：

* 不止跑一次：做多次重复（同一任务多 seed / 多天重复）
* 建立“变更控制”：记录 Claude Code 版本、模型版本、工具配置（Claude Code telemetry 里本身有 app.version、model 等属性） ([Claude Code][3])
* 输出可复现的分析 pipeline（最好开源）
* 给出“可操作建议”或“可预测指标”：例如 early trace features 预测 success/failure/cost blow-up

**潜在 venue**：ICSE / FSE / ASE / ISSTA（尤其如果你有 test generation、debugging pattern、failure taxonomy）。

### 路线 3：Datasets/Benchmarks —— “A standardized full-fidelity trace dataset for agentic coding”

你也可以走 benchmark/dataset 路线：

* 你提出一个**统一 trace schema**（覆盖 prompt、tool calls、tool outputs、timing、resource usage、final diff、test results、errors），
* 并发布一个大规模数据集（比如 SWE-bench Verified/Pro，外加你自己构造的 holdout），
* 同时给出基线分析（cost/runtime/success frontier）。

这一类论文是能发的，但评审要求是：

1. 数据真的新（维度新、规模新、覆盖新、可复现）；
2. 有明确的研究问题与 baselines；
3. 许可证/合规说清楚（尤其代码内容可能涉及上游 repo license）。Nebius 的数据集都专门提醒要尊重每个 repo 的 license。 ([Hugging Face][1])

---

## 4) 你这类工作“novelty 可以从哪来”？给你一套可直接写进论文的 claim 模板

下面这些 claim 你不需要全做，但至少要有 1 个主贡献 + 1–2 个副贡献。

### Claim A：**“全量 trace 的新维度”**（现有轨迹集大多没有）

现有 SWE-agent/OpenHands 轨迹集主要是“内容级轨迹 + patch + test logs”，但你要补齐：

* per-step **wall-clock timestamps**
* per-tool-call **duration distributions**（Claude Code 自带 duration_ms；系统层面更可补） ([Claude Code][3])
* per-run **resource usage**：CPU/mem/disk IO/network/GPU（你自己采集，现有公开轨迹集基本不给）
* per-run **failure taxonomy**：API errors、tool errors、sandbox issues、nondeterminism

**可写的 novelty 句式**：

> We introduce the first publicly available trace dataset for agentic software engineering that aligns model calls, tool invocations, wall-clock timestamps, and system-level resource utilization under a unified schema, enabling end-to-end cost/latency/energy analysis beyond token-based accounting.

这句话要能站得住，就得确保真的“first”——至少在你引用的对比里，nebius 的轨迹集没有这些系统级指标。 ([Hugging Face][1])

### Claim B：**“生产 agent 的可复现评测方法论”**

Claude Code 这种工具，版本更新快、内部 prompt 不公开、行为可能漂移。你如果能提出一套“可复现协议”，就是贡献：

* 记录 Claude Code 版本与模型版本（OTel attributes 支持） ([Claude Code][3])
* 记录工具权限策略（tool_decision event） ([Claude Code][3])
* 记录每次 API request 的 duration/token/cost（api_request event） ([Claude Code][3])
* 用固定容器镜像、固定 repo commit、固定测试命令重放（SWE-bench 本身就是这套范式）

然后你可以做“跨天重复跑”来量化 nondeterminism / regression。这一点在 Anthropic 的 eval 文章里也强调了“eval harness 记录所有步骤并聚合结果”，并指出 SWE-bench Verified 走向饱和后分数变化会变得 deceptive。 ([Anthropic][11])

### Claim C：**“Trace-based 解释：为什么同一个底座模型在不同 agent 上差这么多？”**

Auggie 的文章已经把核心现象说得很直白：同一个 Opus 4.5，Auggie/Cursor/Claude Code 分数不同，原因是 agent architecture（尤其 retrieval/context）。 ([Augment Code][10])

但它是博客，不是可复现科学。你可以把这个现象变成论文：

* 固定底座模型
* 固定任务集（SWE-bench Pro/Verified）
* 固定预算（tokens/$/time）
* 对比多个 harness（Claude Code、SWE-agent、OpenHands）
* 用 trace 指标解释差异：检索深度、文件访问覆盖率、test invocation pattern、edit locality、回滚频率、工具失败率等

这类工作 SE/MLSys 都吃，因为它把“agent architecture”从口号变成可测量的量。

### Claim D：**“基于 trace 的新指标/新预测任务”**

你完全可以把 trace 数据变成新的预测任务，从而变成 ML 论文的一部分：

* **early-exit prediction**：跑到第 k 步时预测最终能否成功，从而动态分配预算
* **cost overrun prediction**：预测 token/cost 爆炸的 run
* **tool failure prediction**：预测下一次 tool call 是否会失败（基于当前上下文、工具类型、repo 状态等）
* **trajectory clustering**：发现成功 run 的“宏观策略簇”

这跟那篇轨迹研究里做的 RQ（轨迹长度、token、动作序列模式、成功 vs 失败差异）是同一思路，但你的数据如果加入时间与资源维度，就能扩展出新的研究问题。 ([arXiv][8])

### Claim E：**“隐私/许可友好的 trace release 方案”**

这是真正容易被忽略但很值钱的点：

* SWE-bench 的任务来自开源 repo；你把 prompt、file contents、diff、test output 全放出来，可能牵涉到不同 repo license。nebius 的轨迹集专门提醒要尊重每个 repo license。 ([Hugging Face][1])
* Claude Code 的 telemetry 默认会对 prompt 内容脱敏，只有显式开启才记录内容（这从产品角度就是在避免泄漏）。 ([Claude Code][3])

如果你能提出一个“可公开的 trace 表示”（例如只公开结构与哈希、patch diff、tool metadata 与 timing，而不公开完整文件内容），同时保证研究可复现，这本身是 contribution。

---

## 5) 如果你们真要做：我建议的“最像论文”的系统设计（不绕弯）

下面给你一个按“论文可交付物”倒推的工程清单。

### (1) Trace schema：不要只存 transcript，存 event log（可重放的）

建议两层数据：

**Layer 1：事件流（event log，JSONL）**
每条 event 至少包含：

* `run_id`, `task_id`, `agent`, `agent_version`, `model`, `repo`, `commit`, `seed`
* `event_type`: `llm_request`, `llm_response`, `tool_call`, `tool_result`, `test_run`, `sandbox_metric`, `error`, `final_patch` …
* `t_start`, `t_end`, `duration_ms`
* `payload`: 对应内容（可脱敏/可哈希）

Claude Code OTel 事件已经覆盖了 `user_prompt`、`tool_result`、`api_request` 等关键字段（含 timestamp、duration、token/cost）。 ([Claude Code][3])
你要做的是把它们与 SWE-bench harness 的阶段（checkout、install、reproduce、edit、run tests、submit）对齐。

**Layer 2：可派生的 summary 表（parquet/csv）**
每个 run 一行：success、total_cost、total_tokens、wall_time、tool_time_breakdown、tool_fail_rate、#tests, #edits 等。

这样论文里分析用 Layer 2，复现和后续研究用 Layer 1。

### (2) 资源占用：用容器/cgroup 采集，不要指望 Claude Code 自带

Claude Code 事件里有 tool duration、API request duration、token/cost，但没有 CPU/mem。 ([Claude Code][3])
你自己加：

* 每个 run 在独立 container/cgroup 中执行
* 采集 `cpu_time`, `max_rss`, `io_read_bytes`, `io_write_bytes`, `net_tx/rx`
* 如果跑本地 GPU 推理，再加 `gpu_util`, `gpu_mem`, `energy`（如果能拿到）

这一步的意义在于：**把“token 成本”与“真实 wall-clock/资源成本”分离开**。系统论文尤其吃这一点。

### (3) “生产 agent”最大坑：版本漂移与系统 prompt 不可见

你必须承认并处理两件事：

* Claude Code 的 system prompt 很多不公开；LangSmith 集成文档也明确 system prompts 不会从 transcript 返回。 ([LangChain Docs][4])
* 因此你做研究时要把“可控的 prompt 部分”外置：比如把你自己的 system instruction 放在 repo 里的 `CLAUDE.md` / `.claude/settings.json` 等，保证能随 run archive 记录。

否则审稿人会说：你结果不可复现，因为你自己都不知道模型看到的完整 prompt。

### (4) 评测集：别只盯 SWE-bench Verified（可能饱和），把 Pro/Multilingual 拉进来

Anthropic 的 eval 文章已经提到 SWE-bench Verified 分数逼近饱和后，增量变得 deceptive。 ([Anthropic][11])
业界也已经在用 SWE-bench Pro（更难、更多语言）来拉开差距。 ([Augment Code][10])
所以你如果只做 Verified，很容易做成“重复造轮子 + 还不够难”。更好的组合是：

* SWE-bench Verified（对齐已有文献）
* SWE-bench Pro（避免饱和、展示可扩展性） ([Augment Code][10])
* 再加一个你们自建 holdout（防污染、展示泛化）

### (5) 最好能给一个“trace 驱动的改进”作为 bonus

纯 measurement 也能发，但更稳的是：

* 你用 trace 提出一个 policy（budgeting / early stop / tool retry / retrieval depth control）
* 在相同预算下提升 success-per-dollar 或 success-per-minute

这样就从“日志数据集”变成“基于数据集的系统/算法改进”。

---

## 6) 你们写论文时最容易翻车的点（提前把雷排掉）

1. **“公开完整 prompt + repo 文件内容”会触碰 license/版权**
   nebius 数据集已经明确提醒要尊重每个 repo license。 ([Hugging Face][1])
   你如果做公开数据集，必须设计脱敏/最小披露策略。

2. **系统不稳定导致结果不可信**
   Anthropic 自己在 SWE-bench Verified 博客里也提到：环境 setup issues、patch 被重复应用等系统问题会污染评测。 ([Anthropic][12])
   所以你要把 harness 做到“失败可归因”：是模型问题、工具问题、环境问题，trace 里要能区分。

3. **只做一次 run 没意义**
   agent 评测本质上 stochastic。你至少要对关键任务做多次重复，给方差/置信区间；否则 reviewer 会说“你在测服务器状态”。

---

## 结论：你问的三个问题，我给一个直球答案

### 1) 现在有没有“大规模跑生产 agent + 全量 trace”的工作？

* **“轨迹大规模”**：有，SWE-agent / OpenHands 这类公开轨迹集已经到了 10^5 级别，并包含完整对话/工具/patch/测试日志。([Hugging Face][1])
* **“Claude Code 生产 agent 的 trace 能力”**：有，Claude Code 官方 OTel 事件本身就带 timestamp、tool duration、API duration、token/cost；并且已经有 LangSmith/MLflow/Honeycomb 这类生态在做 tracing。([Claude Code][3])
* **“公开的、SWE-bench 级别、全量 trace（含资源占用）的 Claude Code 数据集”**：我没看到类似 nebius 那种规模与开放程度的东西；现有更像 CC-Bench 这种较小规模任务集。([Hugging Face][7])
  所以答案是：**碎片都存在，但“你要的那种统一、可复现、可公开的大规模全量 trace”仍然是空位**。

### 2) 这类工作可以发论文吗？

可以。

* SE 圈已经在发“轨迹实证研究”。([arXiv][8])
* 系统圈也在用 SWE-bench traces 做 tool-call-aware 推理系统。([arXiv][9])
  关键是：你要让它像研究，而不是像日志归档。

### 3) Novelty 怎么 claim？

别 claim “我们记录了 prompt 和 tool calls”。
你应该 claim：

* **第一套**把生产 agent（Claude Code）的 telemetry 与 SWE-bench harness 对齐的**可复现协议/trace schema**；
* **第一个**公开的、包含 **timing + token/cost +（你补的）资源占用**的 agentic coding trace 数据集；
* 或者 **第一个**用 trace 系统解释/预测“同底座模型、不同 harness 分数差异”的实证研究；
* 最好再加一个 trace-driven policy/optimization 作为强化贡献。

如果你告诉我你们更偏向 **systems** 还是 **SE**（或者就是想投 NeurIPS/ICLR D&B），我可以把上面这些内容进一步收敛成一份“论文大纲 + 贡献点列表 + 实验矩阵 + ablation 计划”，直接能开写的那种。

[1]: https://huggingface.co/datasets/nebius/SWE-agent-trajectories "https://huggingface.co/datasets/nebius/SWE-agent-trajectories"
[2]: https://huggingface.co/datasets/nebius/SWE-rebench-openhands-trajectories "https://huggingface.co/datasets/nebius/SWE-rebench-openhands-trajectories"
[3]: https://code.claude.com/docs/en/monitoring-usage "https://code.claude.com/docs/en/monitoring-usage"
[4]: https://docs.langchain.com/langsmith/trace-claude-code "https://docs.langchain.com/langsmith/trace-claude-code"
[5]: https://mlflow.org/docs/latest/genai/tracing/integrations/listing/claude_code/ "https://mlflow.org/docs/latest/genai/tracing/integrations/listing/claude_code/"
[6]: https://www.honeycomb.io/blog/measuring-claude-code-roi-adoption-honeycomb "https://www.honeycomb.io/blog/measuring-claude-code-roi-adoption-honeycomb"
[7]: https://huggingface.co/datasets/zai-org/CC-Bench-trajectories "https://huggingface.co/datasets/zai-org/CC-Bench-trajectories"
[8]: https://arxiv.org/html/2506.18824v1 "https://arxiv.org/html/2506.18824v1"
[9]: https://arxiv.org/html/2511.02230v1 "https://arxiv.org/html/2511.02230v1"
[10]: https://www.augmentcode.com/blog/auggie-tops-swe-bench-pro "https://www.augmentcode.com/blog/auggie-tops-swe-bench-pro"
[11]: https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents "https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents"
[12]: https://www.anthropic.com/research/swe-bench-sonnet "https://www.anthropic.com/research/swe-bench-sonnet"
