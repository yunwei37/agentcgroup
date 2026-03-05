# Branch Context Motivation 实验计划

## 背景

本文提出 "branch context"——一种面向 agentic exploration 的新 OS 抽象，提供 copy-on-write 状态隔离、fork/explore/commit 生命周期、first-commit-wins 语义和嵌套支持。论文的 Section 2（Motivation）做出了以下核心声明，需要实验数据支撑：

1. **AI agent 确实在频繁地进行多路径探索**，现有 ad-hoc 方案（git stash、容器克隆、临时目录）不够用
2. **六项需求（R1-R6）是真实存在且未被满足的**
3. **现有 OS 机制有具体的功能缺陷**（论文 Table 1 和 Table 2）

现有 AgentCgroup 项目已有 SWE-bench trace 数据（144 个任务），但**现有 trace 数据不足以支撑所有分析**——缺少文件系统快照、进程列表、/tmp 状态等关键数据。需要重跑一组任务并增强数据收集。

### 论文中需要 motivate 的六项需求

| 需求 | 含义 | 核心挑战 |
|------|------|---------|
| R1: 隔离的并行执行 | 多条探索路径同时运行，各自有独立的文件系统视图、进程空间和内存状态 | 并发修改同一文件/共享内存会导致状态污染 |
| R2: 原子提交 + 单赢者决议 | 成功路径原子应用（文件系统+进程状态），兄弟路径自动失效并释放资源 | 需要事务语义，手动 merge 容易出错，失败路径的内存/进程需可靠回收 |
| R3: 层次嵌套 | Tree-of-Thoughts 等模式需要嵌套分支，每层向父级提交 | 现有机制要么不支持嵌套，要么嵌套后性能退化 |
| R4: 完整状态覆盖 | agent 产生文件系统变更（构建产物、包安装）、内存状态（加载的数据/模型）、临时资源（/tmp、IPC） | git 只追踪已跟踪文件，进程内存和 IPC 状态完全不可回滚 |
| R5: 轻量、无需特权、可移植 | 分支创建必须亚毫秒级、不需要 root、跨文件系统可用 | VM 快照/特权容器/文件系统绑定方案均违反此要求 |
| R6: 进程协调 | 分支内的进程在 commit/abort 时必须可靠终止并释放内存，兄弟分支间相互隔离 | 进程组可逃逸，cgroup 需 root，PID 命名空间有 init 开销 |

### 需要隔离的状态维度

Branch context 不仅仅是文件系统隔离——agent 的探索路径会产生**多维状态**，都需要在 abort 时被干净地回收：

| 状态维度 | 具体内容 | abort 时需要的操作 | 现有方案能否处理 |
|----------|---------|-------------------|-----------------|
| **文件系统** | 源文件修改、包安装、构建产物、缓存 | 丢弃所有 CoW 页面 | git：仅部分；OverlayFS：需 root |
| **进程内存** | 加载的测试数据、编译器内存、运行时堆 | 终止所有进程，释放内存 | kill -PGID：可逃逸；cgroup：需 root |
| **临时文件/IPC** | /tmp 文件、Unix socket、共享内存段、管道 | 清理所有临时资源 | 无统一机制，需逐个追踪 |
| **环境状态** | 环境变量、工作目录、ulimit 设置 | 恢复到分支创建时的快照 | 无机制支持 |
| **网络状态** | 监听端口、建立的连接 | 关闭所有 socket | PID namespace 可以，但开销大 |

---

## 第零部分：增强数据收集——eBPF 内核级追踪

### 现有数据的不足

现有 trace 数据（`tool_calls.json` + `resources.json` + `trace.jsonl`）只记录了：
- 工具调用类型、时间戳、输入参数（Bash 命令文本、Edit 的 file_path/old_string/new_string）
- 工具返回结果的 preview（截断到 500 字符）
- 1 秒粒度的容器级 CPU/内存采样

**关键缺失**：
1. **无文件系统变更追踪**——不知道 `pip install`/`npm install`/`pytest` 到底改了多少文件、哪些目录
2. **无进程生命周期和协调事件**——不知道是否有残留后台进程、进程组逃逸、fork 行为
3. **无网络端口使用**——不知道 agent 是否启动了 server、绑定了哪些端口
4. **无 per-process 内存分解**——只有容器总内存
5. **无共享内存/mmap 追踪**——不知道跨进程状态共享情况

### 增强采集方案：eBPF 内核级追踪（AgentSight）

**弃用旧方案**：bash wrapper + podman exec 周期采集存在以下问题：
- `find /testbed` 在 4GB 工作空间上耗时数秒，干扰 agent 行为
- 10 秒采样粒度遗漏短暂状态变化（如短命后台进程、临时端口绑定）
- `podman exec` 本身有 ~100ms 开销
- 无法追踪 syscall 级别的细粒度行为（谁删了哪个文件、谁 bind 了哪个端口）

**新方案**：直接使用 AgentSight 的 `agentsight/bpf/process_new`，通过 eBPF tracepoints 在**内核层面**捕获关键 syscall。

详细设计见 `agentsight/docs/design/PLAN_process_tracer_extension.md`。核心要点：

#### 追踪能力

在现有 process tracer（EXEC/EXIT/FILE_OPEN/BASH_READLINE）基础上，通过 feature flags 新增：

| Flag | 追踪内容 | 对应 syscall tracepoints |
|------|---------|------------------------|
| `--trace-fs` | 文件删除/重命名/目录创建/写入/截断/chdir | unlink/unlinkat, rename/renameat/renameat2, mkdir/mkdirat, write, ftruncate, chdir |
| `--trace-net` | 端口绑定/监听/连接 | bind, listen, connect |
| `--trace-signals` | 进程组变更/会话创建/信号发送/fork | setpgid, setsid, kill, sched_process_fork |
| `--trace-mem` | 共享内存映射 | mmap（仅 MAP_SHARED） |
| `--trace-cow` | CoW page fault 计数 | kprobe/do_wp_page（内核相关） |
| `--trace-all` | fs + net + signals + mem（不含 cow） | — |

#### 关键设计

- **零开销 feature flags**：BPF `const volatile` 变量，JIT 优化禁用分支为 nop
- **统一内核聚合**：所有新事件走 BPF hash map 内核聚合（不经 ring buffer），用户空间每 5 秒 flush
- **PID 过滤复用**：现有 pid_tracker 对所有事件统一生效，`-c python` 同时过滤所有事件类型
- **向后兼容**：不加新 flag 时行为、性能完全不变

#### 输出格式

现有事件不变。新增事件统一输出为 SUMMARY 格式：

```jsonl
{"timestamp":260,"event":"SUMMARY","pid":1234,"comm":"pip","type":"DIR_CREATE","detail":"/testbed/venv/lib/requests","count":47,"extra":"/testbed/venv/lib/requests/utils"}
{"timestamp":260,"event":"SUMMARY","pid":1234,"comm":"pip","type":"WRITE","detail":"fd=5","count":1847,"total_bytes":4521984}
{"timestamp":260,"event":"SUMMARY","pid":5678,"comm":"python","type":"NET_BIND","detail":"0.0.0.0:8080","count":1}
{"timestamp":260,"event":"SUMMARY","pid":1234,"comm":"kill","type":"SIGNAL_SEND","detail":"target=5678,sig=9","count":1}
```

### 实验运行方式

在宿主机上用 AgentSight 追踪容器内的 agent 行为：

```bash
# 启动 SWE-bench 容器
podman run -d --name swebench_task ...

# 在宿主机上直接运行 process_new（需 root/CAP_BPF）
mkdir -p experiments/branchfs_motivation/<task_name>
sudo ./agentsight/bpf/process_new -m 2 -c "python,node,bash,sh,pip,pytest" --trace-all \
    > experiments/branchfs_motivation/<task_name>/ebpf_trace.jsonl \
    2> experiments/branchfs_motivation/<task_name>/ebpf_trace.stderr &
echo $! > experiments/branchfs_motivation/<task_name>/ebpf_trace.pid

# 任务结束后用 SIGINT 停止（会触发最终 flush，避免丢最后一批 SUMMARY）
sudo kill -INT "$(cat experiments/branchfs_motivation/<task_name>/ebpf_trace.pid)"
```

eBPF 运行在宿主机内核，天然能看到容器内进程的所有 syscall——无需在容器内安装任何东西。

### Agent 运行模式（Sonnet / Haiku / 本地模型）

#### A) Haiku（云端 Anthropic API，默认）

单任务（推荐先做 smoke test）：

```bash
python scripts/run_swebench.py \
  swerebench/sweb.eval.x86_64.encode_1776_starlette-1147 \
  --model haiku --run-tests
```

按任务清单批量跑（10-15 个任务）：

```bash
python scripts/run_all_swebench_images.py \
  --task-list task_list.json \
  --model haiku \
  --resume
```

#### B) Sonnet（可选）

```bash
python scripts/run_swebench.py \
  swerebench/sweb.eval.x86_64.encode_1776_starlette-1147 \
  --model sonnet --run-tests
```

#### C) 本地模型（Anthropic-compatible endpoint）

当前仓库有两条现成路径：

1. `run_all_swebench_images.py --model qwen3`  
   脚本会注入：
   - `ANTHROPIC_BASE_URL=http://localhost:8080`
   - `ANTHROPIC_AUTH_TOKEN=llama`
   - `ANTHROPIC_API_KEY=""`
2. `batch_test_swebench.py --local-model <name>`  
   脚本会注入：
   - `ANTHROPIC_BASE_URL=http://localhost:4000`
   - `ANTHROPIC_MODEL=<name>`

本地模型全流程（llama-server + 批量任务）可直接用：

```bash
# 启动本地推理服务 + SWE-bench 批量任务
bash scripts/run_experiment.sh start

# 查看状态
bash scripts/run_experiment.sh status

# 停止
bash scripts/run_experiment.sh stop
```

若你只想手动批量跑，不用监控脚本：

```bash
python scripts/run_all_swebench_images.py \
  --task-list task_list.json \
  --model qwen3 \
  --resume
```

### 容器实验（与 process_new 联动）

`run_swebench.py`/`run_all_swebench_images.py` 会自动：
- `podman pull` 镜像
- 修复 `/testbed` 权限并生成 fixed image
- 在容器内运行 `claude --model ...`
- 导出 `trace.jsonl`、`tool_calls.json`、`resources.json`

推荐的单任务联动流程：

```bash
# 1) 启动 eBPF 追踪
mkdir -p experiments/branchfs_motivation/<task_name>
sudo ./agentsight/bpf/process_new -m 2 -c "python,node,bash,sh,pip,pytest" --trace-all \
  --cgroup-filter "$CGROUP_PATH" \
  > experiments/branchfs_motivation/<task_name>/ebpf_trace.jsonl \
  2> experiments/branchfs_motivation/<task_name>/ebpf_trace.stderr &
echo $! > experiments/branchfs_motivation/<task_name>/ebpf_trace.pid

# 2) 跑容器任务（默认 haiku；本地模型也可）
python scripts/run_swebench.py <docker_image> --model haiku --run-tests
# 本地模型建议走已内置 endpoint 配置的脚本（二选一）：
python scripts/run_all_swebench_images.py --task-list task_list.json --model qwen3 --resume
# 或
python scripts/batch_test_swebench.py --task "ML/Scientific,Medium,2" --local-model qwen3

# 3) 停止 eBPF（触发 final flush）
sudo kill -INT "$(cat experiments/branchfs_motivation/<task_name>/ebpf_trace.pid)"
```

其中 `CGROUP_PATH` 可通过容器运行后获取，例如：

```bash
podman inspect --format '{{.State.CgroupPath}}' <container_id>
```

### 最新验证（2026-03-05）

为验证 cgroup 硬过滤与子 cgroup 匹配，做了三组同条件对照（同一容器、同一条 `bash -lc` 标记命令）：

产物目录：`/tmp/cgroup_children_check4_20260304_230628`

| 组别 | 关键参数 | 结果摘要 |
|------|---------|---------|
| 不过滤 | 无 `--cgroup-filter` | `lines=423`, `bash_exec=1`, `unique_comm=19`，能抓到目标 bash，但宿主/系统噪声明显 |
| 父 cgroup only | `--cgroup-filter <parent>` | `lines=2`（仅 `CLOCK_SYNC`），`bash_exec=0`，抓不到子 cgroup 中的目标 bash |
| 父 + children | `--cgroup-filter <parent> --cgroup-filter-children` | `lines=105`, `bash_exec=1`, `unique_comm=4`，抓到目标 bash 且噪声明显降低 |

结论：

1. 只用父 cgroup 精确匹配会漏掉子 cgroup 工作负载。  
2. `--cgroup-filter-children` 可以在保留低噪声的同时抓到容器内实际执行。  
3. `run_swebench_new.py` 默认自动注入容器 cgroup，且在执行 Claude 前等待 tracer `CLOCK_SYNC(start)`，可减少启动窗口漏采。

### 小样本 Pilot（Haiku，2026-03-05）

为验证“少量数据是否已经能读出有效信号”，运行了 2 类轻量实验（每类 3 次）：

实验目录：`/home/yunwei37/workspace/agentcgroup/experiments/branchfs_motivation/pilot_haiku_small_20260304_231642`

| 实验 | Prompt 目标 | 运行次数 | 成功率 | Bash tool call |
|------|-------------|---------|-------|----------------|
| A（读/列目录） | `echo + pwd + ls` | 3 | 3/3 | 3/3 |
| Bfix（文件副作用） | `echo > file; mkdir; mv; rm; rmdir` | 3 | 3/3 | 3/3 |

关键观测：

1. **链路稳定**：6/6 run 均成功；`trace_ready_elapsed_s` 平均约 0.30s；自动 cgroup 注入均生效。  
2. **语义差异可见**：Bfix 相比 A，`DIR_CREATE/FILE_RENAME/FILE_DELETE` 更明显，且能抓到带 run-tag 的路径事件。  
3. **marker 级验证**（Bfix）：3/3 run 抓到 marker 相关 `FILE_RENAME`；2/3 抓到 `FILE_DELETE`；2/3 抓到 `DIR_CREATE`。  
4. **已识别一个执行层坑点**：若 prompt 中命令使用 `$var`，会被外层 shell 提前展开，可能导致命令畸形（先前 `pilotB_r*` 3/3 无 tool call 即由此引起）。  
   - 规避方式：pilot/batch prompt 中避免 `$` 变量，直接用完整绝对路径；或在 runner 层做 prompt 安全转义。

Pilot 结论：

- 小样本已经能验证数据链路正确，并能区分不同类型 workload 的 syscall 副作用模式。  
- 在进入大批量之前，应加入一个轻量质量门槛：`tool_calls>0` 且出现预期 marker/事件类型，否则自动重试或标记无效样本。

### 当前还差什么（process_new 直连版）

当前链路已经可以支撑**动机实验的主证据采集**，但要达到论文可复现/可量化标准，仍有以下缺口：

1. **WRITE 路径仍是 best-effort**：已支持 `/proc/<pid>/fd/<fd>` 解析路径（`detail` + `fd` + `path_resolved`），但短生命周期 FD/进程退出后仍可能回退到 `fd=N`。
2. **子 cgroup 刷新有时间窗**：`--cgroup-filter-children` 通过 userspace 周期刷新子树 inode 集合实现，极短生命周期子 cgroup 仍可能在刷新间隙漏掉部分事件。
3. **容器内背景噪声仍在**：虽然宿主机噪声已通过 cgroup 过滤大幅下降，但容器内部辅助进程（如 `git`、shell helper）仍会进入 trace，需要分析阶段做语义去噪。
4. **时间对齐自动化缺失**：`CLOCK_SYNC` 锚点已具备，但尚缺统一脚本把 `ebpf_trace.jsonl` 与 `trace.jsonl`/`tool_calls.json` 做批量对时归一化。
5. **每 tool-call 的资源归因未自动打通**：当前是“容器级资源采样 + syscall 聚合”，尚未形成稳定的 tool-call 级归因流水线。

> 结论：  
> 对 Section 2 Motivation（证明“探索存在 + 副作用广泛 + 现有机制不足”）已经**够用**。  
> 对“严格因果归因到每次 tool-call、并给出批量统计显著性”还需要补分析自动化。

### 任务选择

不需要重跑所有 144 个任务。选择 **10-15 个代表性任务**：

**选择标准**：
1. 有大量重试的任务（探索频率高）——从已有数据中选 retry 最多的 5 个
2. 有 pip/npm install 的任务——从已有数据中选有 install 命令的 5 个
3. 有 git stash/checkout 回滚操作的任务——从已有数据中选有回滚的 3-5 个

**已有的候选**：

| 任务 | 特征 | 数据集 |
|------|------|--------|
| `tobymao__sqlglot-4415` | 199 个工具调用，123 Bash（大量探索） | GLM |
| `Algebra8__pyopenapi3-91` | 178 个工具调用，107 Bash | GLM |
| `getsentry__sentry-python-2148` | 21 Bash 含大量 pip install + venv 创建 | Haiku |
| `12rambau__sepal_ui-574` | 有 git stash/pop 回滚 | GLM |
| `AzureAD__microsoft-authentication-library-for-python-186` | 有 git stash + 测试回滚 | GLM |
| `facelessuser__pymdown-extensions-2576` | 有 git stash + 测试回滚 | GLM |
| `beeware__briefcase-2212` | 77 工具调用 | Haiku |
| `numba__numba-9636` | 有 __pycache__ 清理 + git checkout 回滚 | GLM |
| `getsentry__sentry-python-1053` | 有 git checkout + git stash 回滚 | GLM |
| `dask__dask-2205` | 两个数据集都有，可做交叉验证 | Both |

### 重跑的输出格式

每个任务在 `experiments/branchfs_motivation/<task_name>/` 下产出：

```
tool_calls.json          # 原有（工具调用序列）
resources.json           # 原有（CPU/内存采样）
trace.jsonl              # 原有（完整 trace）
ebpf_trace.jsonl         # 新增：eBPF 内核级追踪（所有事件）
```

`ebpf_trace.jsonl` 包含以下事件类型：

| 事件类型 | 来源 | 说明 |
|----------|------|------|
| EXEC/EXIT | 现有 ring buffer | 进程启动/退出 + 完整命令行 + 退出码 |
| CLOCK_SYNC | userspace anchor | monotonic 与 wall-clock 对齐锚点（start/end） |
| FILE_OPEN | 现有 ring buffer | 文件打开（带 dedup） |
| BASH_READLINE | 现有 ring buffer | bash 命令输入 |
| SUMMARY: DIR_CREATE/FILE_DELETE/FILE_RENAME | BPF map flush | 文件系统变更聚合（含 count + 目录前缀） |
| SUMMARY: WRITE | BPF map flush | 写入聚合（`write/pwrite64/writev`；含 total_bytes，尽量解析路径） |
| SUMMARY: NET_BIND/NET_LISTEN/NET_CONNECT | BPF map flush | 网络事件（含 addr:port） |
| SUMMARY: PGRP_CHANGE/SESSION_CREATE/SIGNAL_SEND/PROC_FORK | BPF map flush | 进程协调事件 |
| SUMMARY: MMAP_SHARED | BPF map flush | 共享内存映射 |

**单一文件包含所有维度**——比旧方案（7+ 文件散布在多个目录）大幅简化分析。

### 实施步骤

1. **固定采集链路为 `process_new` 直连**（不走 `agentsight record/trace`）
2. **新增 `scripts/run_swebench_new.py`**：统一编排 SWE-bench + eBPF，自动拉起/停止 `process_new`
3. **已实现核心能力**：`pwrite64/writev` 采集、WRITE 路径解析（best-effort）、可选 `--cgroup-filter`
4. **已补关键编排细节**：容器先启动、自动提取 cgroup、等待 tracer `CLOCK_SYNC(start)` 就绪后再放行 Claude，减少启动窗口漏采
5. **继续补分析自动化**：解析并归一化 `ebpf_trace.jsonl`（与 `trace.jsonl` 时间对齐）
6. **选择 10-15 个任务**重跑

### `run_swebench_new.py` 设计（已新增）

目标：一条命令完成“容器任务执行 + 宿主机 eBPF 追踪 + 统一产物落盘”，避免手工起停 tracer。

脚本位置（已创建）：

- `scripts/run_swebench_new.py`（保留现有 `run_swebench.py`，新脚本做编排层）

核心职责：

1. 先启动 idle 容器并拿到 `container_id`
2. 自动解析容器 cgroup path（可手工覆盖）
3. 启动 `process_new`（宿主机，`--trace-all` 或细分 flags）
4. 等待 tracer 输出 `CLOCK_SYNC(start)` 作为 ready 信号
5. 用 `podman exec` 在同一容器里执行 Claude
6. 在 `finally` 中发送 `SIGINT` 停止 tracer 并 flush SUMMARY
7. 统一组织输出目录，写入 run metadata（便于后续分析脚本批处理）

建议 CLI（第一版）：

```bash
python scripts/run_swebench_new.py <docker_image> \
  --model haiku \
  --run-tests \
  --task-name <name> \
  --trace-all \
  --trace-commands "python,node,bash,sh,pip,pytest,git,claude" \
  --trace-mode 2
```

参数设计：

- `docker_image`：透传给现有 SWE-bench 执行逻辑
- `--model`：默认 `haiku`
- `--run-tests` / `--prompt` / `--memory` / `--cpus`：透传
- `--task-name`：用于输出目录命名（缺省为 image + timestamp）
- `--trace-bin`：默认 `./agentsight/bpf/process_new`
- `--trace-mode`：默认 `2`
- `--trace-commands`：默认 `python,node,bash,sh,pip,pytest,git,claude`
- `--trace-all | --trace-fs --trace-net --trace-signals --trace-mem --trace-cow`：追踪开关
- `--trace-resources` / `--sample-interval`：可选，透传给 `process_new`
- `--trace-cgroup-filter`：可选，传给 `process_new --cgroup-filter` 做容器级硬过滤
- `--trace-cgroup-children`：可选，匹配 `--trace-cgroup-filter` 的子 cgroup
- `--no-trace-cgroup-auto`：可选，禁用自动容器 cgroup 注入
- `--trace-ready-timeout`：可选，等待 tracer ready 的超时秒数

执行时序（强约束）：

1. 创建任务输出目录
2. 启动 idle 容器并自动解析 cgroup path
3. 启动 `process_new`（stdout->`ebpf_trace.jsonl`，stderr->`ebpf_trace.stderr`，pid->`ebpf_trace.pid`）
4. 等待 `CLOCK_SYNC(start)` ready
5. 在容器内执行 Claude 任务
6. 无论成功/失败，`SIGINT process_new`，等待退出
7. 写 `run_manifest.json`（记录命令、时间、退出码、各文件路径）

输出目录设计：

```text
experiments/branchfs_motivation/<task_name>/
  ebpf_trace.jsonl
  ebpf_trace.stderr
  ebpf_trace.pid
  run_manifest.json
  swebench/
    results.json
    resources.json
    trace.jsonl
    tool_calls.json
    claude_output.txt
```

失败处理策略：

- `process_new` 启动失败：直接 fail fast，不执行任务
- SWE-bench 运行失败：仍然停止 tracer 并落盘，保留现场供分析
- tracer 停止超时：升级 `SIGTERM`，并在 manifest 记录异常

非目标（第一版不做）：

- 自动时间戳对齐（monotonic vs wall-clock）
- 自动去噪（排除宿主机其他进程）

### eBPF 方案 vs 旧方案对比

| | bash wrapper + podman exec（旧） | eBPF agentsight（新） |
|---|---|---|
| 粒度 | 10 秒采样 / 每次 Bash 调用 | syscall 级别（纳秒精度） |
| 开销 | `find` 数秒 + `podman exec` ~100ms | 内核聚合，<1% CPU |
| 遗漏 | 短暂进程/端口/文件操作 | 无——内核 tracepoint 不会漏 |
| 侵入性 | 修改 bash_wrapper + 容器内安装工具 | 零侵入——宿主机内核追踪 |
| 输出 | 7+ 文件，格式各异 | 单一 JSONL，统一格式 |
| 安装 | 无需 root | 需 root/CAP_BPF（宿主机） |

### 预计资源和时间

- 每个任务平均 10 分钟，10 个任务 ≈ 2 小时（串行）
- 模型运行可选：
  - Sonnet（云端 API，推荐）：当前环境最稳定
  - Haiku（云端 API）：无需本地 GPU
  - 本地模型（`qwen3`/自定义）：需先启动本地推理服务（例如 `llama-server` 或 LiteLLM 代理）
- agentsight 追踪开销 <1%，不影响 agent 行为
- 磁盘：ebpf_trace.jsonl 约 1-10MB/任务（内核聚合大幅减少数据量）
- 可并行跑 2-3 个（agentsight 可同时追踪多个容器的进程）

---

## 第一部分：数据分析——量化探索模式

> 以下分析同时使用**旧数据**（144 个任务，统计频率/比例）和**新数据**（10-15 个任务，详细快照）。

**总目标**：证明真实 agent 频繁进行多路径探索，且每条路径产生大量**多维状态副作用**（文件系统变更、内存积累、进程残留）需要回滚。

### 实验 1.1：探索频率分析

**目标**：量化 agent 在真实任务中尝试多少条不同的解决策略，以及回滚操作的频率。

**数据来源**：144 个 SWE-bench traces（33 Haiku + 111 GLM），位于 `experiments/all_images_haiku/` 和 `experiments/all_images_local/`

**方法**：

1. **识别不同的解决策略**（不仅仅是重试）：
   - 已有发现：85-97% 的任务包含重试循环（连续 3 次以上 Bash 调用）
   - 扩展分析：在重试组之间，检测 agent 是否改变了策略（判定标准：编辑了不同的文件、应用了不同的 patch、尝试了不同的实现路径）
   - 具体做法：提取每个重试组之间的 Edit/Write 操作，比较修改的文件集合和 patch 内容的相似度；若文件集合或 patch 差异超过阈值，则判定为新的探索策略

2. **统计显式回滚操作**：
   - 扫描 trace 中的以下模式：
     - `git checkout -- <file>`、`git stash`、`git restore` 等 git 回滚命令
     - 手动重新编辑以恢复原始内容（Edit 操作的 new_content 与文件的原始内容匹配）
     - `rm -rf` 删除之前创建的文件/目录
   - 这些操作直接 motivate R2（自动回滚需求）

3. **分析并行子 agent**：
   - Haiku 数据中有 17 个 Task（子 agent）调用
   - 检查这些 Task 是否时间上重叠（并发执行）
   - 如果并发，检查它们是否共享文件系统状态

**预期产出**：
- 表格：每个任务的独立探索路径数量、回滚操作次数、每条路径修改的文件数
- 关键数字：例如 "X% 的任务尝试了 ≥2 种不同的解决策略"，"平均每个任务有 Y 次显式回滚操作"

**Motivate 目标**：Section 2.1（agent 模式）、R1（隔离并行执行）、R2（原子提交）

**分析脚本位置**：基于 `analysis/analyze_new_insights.py` 中的重试分析扩展

---

### 实验 1.2：每次探索的文件系统变更范围

**目标**：量化每次探索尝试产生多少文件系统变更，其中多少是 git 无法追踪的。

**数据来源**：
- **旧数据**（144 个任务）：从 trace 中通过命令语义分析推断文件变更
- **新数据**（10-15 个重跑任务）：`ebpf_trace.jsonl` 中的 SUMMARY 事件提供精确的 syscall 级追踪

**方法**：

1. **文件变更统计**（新数据 eBPF 追踪）：
   - 从 `ebpf_trace.jsonl` 中提取 SUMMARY 事件：
     - `DIR_CREATE`：创建的目录数量及路径分布（detail 字段 = 父目录前缀）
     - `FILE_DELETE`：删除的文件数量（`rm -rf` 场景下 count 很高）
     - `FILE_RENAME`：重命名数量（`pip install` 的原子安装模式）
     - `WRITE`：写入的文件数量和总字节数（total_bytes 字段）
   - 按探索尝试（重试组）分组统计

2. **文件变更统计**（旧数据命令分析）：
   - 从 trace 中提取 Edit/Write 工具调用 → 源代码编辑
   - 从 Bash 命令中识别文件变更操作（pip install、npm install、make、pytest 等）

3. **区分 git 可追踪 vs 不可追踪的变更**：
   - **git 可追踪**：源代码文件的编辑（.py, .js, .ts 等）
   - **git 不可追踪**（eBPF 追踪到但 git 看不到的）：
     - `pip install` → `site-packages/` 下大量 DIR_CREATE + FILE_RENAME（eBPF detail 字段可直接看到路径前缀）
     - `npm install` → `node_modules/` 下的变更
     - `make build` → `build/`、`dist/`、`__pycache__/`
     - `pytest` → `.pytest_cache/`、`htmlcov/`
   - 从 eBPF SUMMARY 事件的 detail 字段按路径前缀分类统计

4. **量化"git 盲区"**：
   - 计算：所有文件变更中，有多少比例是 git 不会追踪的
   - eBPF 数据提供精确数字（vs 旧数据的估算）
   - 这直接 motivate R4

4. **量化文件系统 CoW 收益**：
   - 从 eBPF WRITE 事件的 total_bytes 汇总每次探索的实际写入量
   - 对比工作空间总大小（SWE-bench 平均 4.1GB）
   - CoW 收益 = 1 - (实际写入量 / 工作空间总大小)
   - 预期：探索路径实际写入 <100MB，CoW 共享 >97% 的文件系统状态

**预期产出**：
- 饼图/柱状图：文件变更类型分布（源代码编辑 vs 包安装 vs 构建产物 vs 测试缓存）
- 关键数字：例如 "N% 的文件系统变更不在 git 追踪范围内"
- eBPF 精确数据：如 "`pip install flask` 产生 47 个 DIR_CREATE + 203 个 FILE_RENAME + 4.5MB WRITE"
- **CoW 收益**：文件系统层面，探索路径间共享 X% 的数据，CoW 节省 Y GB 磁盘/内存

**Motivate 目标**：R4（完整文件系统覆盖）、R5（CoW 高效性）

---

### 实验 1.3：重试循环中的内存积累与资源泄漏

**目标**：量化 agent 在多次探索尝试中产生的内存积累和进程残留，证明需要 branch-level 的资源回收。

**数据来源**：
- **旧数据**：144 个任务的 `resources.json`（1 秒粒度容器级 CPU/内存）+ `tool_calls.json`
- **新数据**：重跑任务的 eBPF 追踪（EXEC/EXIT 事件 + PROC_FORK）+ `/proc/pid/statm` per-process 内存

**方法**：

1. **内存积累分析**（旧数据）：
   - 已有发现：重试循环导致内存逐次积累，最极端案例 502MB 不释放
   - 扩展分析：
     - 对每个任务，识别所有重试组（连续 Bash 调用）
     - 测量每个重试组**结束后**的内存基线（相比重试组开始前）
     - 计算累积的内存"泄漏"：每次重试后基线升高了多少
     - 这些泄漏的内存就是 branch context 在 abort 时应该回收的

2. **per-process 内存分解**（新数据，弥补旧数据的关键缺陷）：
   - 旧数据只有容器级总内存，不知道哪个进程是"大户"
   - 新方案：在 agentsight 的 EXEC/EXIT 事件处理中，读 `/proc/<pid>/statm` 记录每个进程的 RSS
   - 或用 eBPF 追踪 `mm_struct` 的 RSS 变化（更精确）
   - 分析：pytest 进程 vs pip 进程 vs python 解释器的内存占比
   - **预期发现**：pytest 是内存大户（加载测试数据+fixtures），每次重试都重新加载

3. **进程残留分析**（新旧数据结合）：
   - 旧数据：从 Bash 命令中识别后台进程启动（`nohup`、`&`、daemon）
   - 新数据：eBPF EXEC/EXIT 事件精确追踪——有哪些进程在 agent 放弃后仍未退出
   - eBPF SIGNAL_SEND 事件：agent 是否在重试时手动 kill 之前的进程
   - 检查是否有 agent 因前一轮残留进程（如 test server 占用端口）导致错误

4. **内存突发与探索路径的对齐**：
   - 将内存突发按"探索尝试"分组（而非按单个工具调用）
   - 计算每个探索尝试的峰值内存——这是 branch context 需要隔离的内存量

**预期产出**：
- 图表：重试循环中内存基线的逐步升高（"阶梯状"内存曲线），标注每次重试的增量
- **per-process 内存分解**：pytest 占 X%，pip 占 Y%，框架基线占 Z%
- 数字：平均每个任务因重试累积 XMB 未释放内存
- 关键结论：**没有 branch-level 的资源回收，失败的探索路径会持续占用内存，限制并行探索的可行性**

**Motivate 目标**：R2（abort 时释放资源）、R1（并行探索的资源隔离）

**分析脚本位置**：基于 `analysis/analyze_new_insights.py` 中的重试分析 + `analysis/analyze_swebench_data.py` 中的内存分析扩展

---

### 实验 1.4：探索树深度分析

**目标**：证明 agent 的探索具有层次结构，需要嵌套分支支持。

**数据来源**：Haiku traces（含 Task 子 agent 调用）

**方法**：

1. **重建探索树**：
   - 从 trace 中提取 Task（子 agent）调用
   - 分析子 agent 内部是否也有重试/分支行为
   - 测量最大探索深度

2. **调研现有 agent 框架的探索策略**：
   - **Claude Code**：per-file snapshots，不支持并行分支，shell 命令产生的变更无法快照
   - **SWE-agent**：sequential retry，无并行探索
   - **OpenHands**：支持多种 agent 策略，但探索仍是串行的
   - **Tree-of-Thoughts / Graph-of-Thoughts**：设计上需要嵌套搜索，但缺乏 OS 级支持
   - 引用这些框架的文档/代码，说明嵌套探索是被需要但尚未被支持的

3. **统计 Claude Code 的 Task 嵌套深度**：
   - Haiku 数据中 Task 调用的嵌套关系
   - 是否有 Task 内再调用 Task 的情况

**预期产出**：
- 探索树深度分布
- 框架对比表：各框架的探索策略及其局限

**Motivate 目标**：R3（层次嵌套）

---

## 第二部分：基准测试现有机制——证明它们不够用

**总目标**：用具体的性能数据和功能测试，实证验证 Table 1 和 Table 2 的声明。

### 实验 2.1：分支创建与提交开销对比

**目标**：量化各种隔离机制的创建和提交延迟，证明 BranchFS 的 O(1) 创建优势。

**待测机制**：

| # | 机制 | 类别 | 备注 |
|---|------|------|------|
| 1 | `git stash` + `git stash pop` | 版本控制 | 仅追踪文件 |
| 2 | `git checkout -b` + merge | 版本控制 | 仅追踪文件 |
| 3 | `git worktree add` | 版本控制 | 独立工作目录，但仅追踪文件 |
| 4 | `cp -r` + `rsync` 回写 | 完全复制 | 完整覆盖但 O(n) 开销 |
| 5 | `podman run` / `docker run`（容器克隆） | 容器化 | 重量级，需镜像 |
| 6 | OverlayFS mount | 联合文件系统 | 需 root 权限 |
| 7 | Btrfs snapshot（用 loopback 设备测试） | 文件系统级 | 依赖特定文件系统 |
| 8 | BranchFS（FUSE 实现） | 本文方案 | 无需 root，O(1) 创建 |

**测试工作负载**：变化工作空间大小

| 级别 | 大小 | 说明 |
|------|------|------|
| Small | 10MB | 最小项目（几个源文件） |
| Medium | 100MB | 典型代码仓库 |
| Large | 1GB | 仓库 + node_modules |
| XL | 4GB | SWE-bench 镜像工作空间（平均 4.1GB） |

**测量指标**：

1. **分支创建延迟**（μs）：从发起创建到分支可用的时间
2. **提交/合并延迟**（μs）：将分支变更应用回父级的时间（在分支中做少量修改后测量）
3. **每个分支的内存开销**（KB）：分支本身的元数据内存占用
4. **每个分支的磁盘空间开销**（KB）：CoW 前（仅元数据）和 CoW 后（有修改）的磁盘占用
5. **并发分支扩展性**：同时创建 1/10/100/1000 个分支的总时间

**实验设计**：
- 每个配置重复 100 次取中位数和 P99
- 创建分支后修改 1 个文件（1KB），然后测量 commit 时间
- 同时测量修改多个文件（10/100/1000 个）后的 commit 时间

**预期产出**：
- 柱状图：各机制在不同工作空间大小下的创建延迟
- 表格：BranchFS 创建 < 350μs 且与基础文件系统大小无关；cp -r / container clone 线性增长
- 关键结论：BranchFS 比 cp -r 快 N 个数量级，比 container clone 快 M 倍

**Motivate 目标**：R5（轻量级）、Table 1

---

### 实验 2.2：进程隔离对比

**目标**：用具体测试用例验证 Table 2 中各进程管理机制的隔离缺陷。

**测试场景**：

#### 场景 1：进程逃逸测试

```c
// child.c - 一个会逃逸进程组的子进程
#include <unistd.h>
int main() {
    setsid();           // 创建新 session，逃离原进程组
    // 或 setpgid(0, 0);  // 创建新进程组
    while(1) sleep(1);  // 持续运行
    return 0;
}
```

- 在进程组/session 中启动此进程
- 尝试 `kill(-pgid, SIGKILL)` 终止整个组
- 检查逃逸进程是否仍存活

**预期结果**：

| 机制 | 逃逸进程是否存活 |
|------|-----------------|
| 进程组 (pgrp) | ✓ 存活（已逃逸） |
| Session | ✓ 存活（已用 setsid 逃逸） |
| cgroup | ✗ 被终止（cgroup.kill 杀死所有成员） |
| PID namespace | ✗ 被终止（namespace 销毁杀死所有进程） |
| branch() | ✗ 被终止（内核强制） |

#### 场景 2：跨分支信号干扰测试

```python
# 两个并发"探索路径"
# Path A 的进程尝试 kill Path B 的进程
import os, signal
os.kill(path_b_pid, signal.SIGKILL)  # 能否成功？
```

| 机制 | 跨组信号是否可发送 |
|------|------------------|
| 进程组 | ✓ 可以（同 UID 即可） |
| Session | ✓ 可以（同 UID 即可） |
| cgroup | ✓ 可以（cgroup 不阻止跨组信号） |
| PID namespace | ✗ 不可以（不同 namespace 看不到对方） |
| branch() | ✗ 不可以（内核强制兄弟隔离） |

#### 场景 3：孤儿进程清理测试

```python
# 父进程 fork 子进程后退出
import os
pid = os.fork()
if pid == 0:
    # 子进程：继续运行
    while True: time.sleep(1)
else:
    # 父进程：立即退出
    os._exit(0)
```

- 检查父进程退出后，子进程是否仍存活
- 尝试清理：各机制能否可靠终止所有后代进程

**实验实施**：
- 用 C/Python 编写上述测试程序
- 在每种机制下运行，记录结果
- 特别注意 cgroup 的权限问题（需要测试有/无 root 两种情况）

**预期产出**：填充完整的 Table 2，附带实测 pass/fail 结果

**Motivate 目标**：R6（进程协调）、Table 2

---

### 实验 2.3：真实 Agent Trace 重放

**目标**：用真实 SWE-bench 任务的 trace 展示 branch context 能带来的加速。

**实验设计**：

1. **选取任务**：从已有数据中选择重试循环最多的任务（如 GLM 最大连续重试 56 次的任务）

2. **串行重放（现状）**：
   - 按 trace 原始顺序回放所有操作
   - 测量花在"回滚"上的时间（git checkout、重新编辑等操作的累计时间）
   - 测量总端到端时间

3. **并行重放（假设有 branch context）**：
   - 在每个重试点创建 branch
   - 并行执行多条探索路径
   - 第一个成功的路径 commit，其余 abort
   - 测量理论加速比

4. **分析**：
   - 回滚时间占总执行时间的比例
   - 并行探索的理论加速比（假设 N 条路径并行，加速比 ≈ N / 串行尝试次数）
   - 被浪费的计算（失败路径执行的工具调用总时间）

**预期产出**：
- 具体数字：例如 "任务 X 串行执行 10 分钟，其中 2 分钟花在回滚上；若并行探索可缩短至 6 分钟"
- 图表：不同任务的潜在加速比分布

**Motivate 目标**：整体 motivation、R1-R6

**注意**：此实验需要 BranchFS 集成，优先级较低（P3），但即使不实际运行，也可以基于 trace 数据进行理论分析。

---

## 第三部分：内存与进程状态隔离——超越文件系统

**总目标**：证明 branch context 需要隔离的不仅仅是文件系统，还包括进程内存、共享状态和临时资源。文件系统隔离是必要的但不充分的。

### 实验 3.1：内存状态污染演示

**目标**：展示并发探索路径之间通过共享内存/tmpfs/环境变量产生的状态干扰。

**场景设计**：

#### 场景 A：共享 /tmp 导致的干扰

```python
# Path A: 将测试数据写入 /tmp
with open("/tmp/test_config.json", "w") as f:
    json.dump({"strategy": "A", "param": 1}, f)
subprocess.run(["pytest", "test_suite.py"])  # 测试读取 /tmp/test_config.json

# Path B: 同时也写入 /tmp（相同文件名）
with open("/tmp/test_config.json", "w") as f:
    json.dump({"strategy": "B", "param": 2}, f)
subprocess.run(["pytest", "test_suite.py"])  # 覆盖了 Path A 的配置！
```

- 无隔离：两条路径共享 /tmp，配置文件互相覆盖
- 有 branch context：每条路径有独立的 /tmp 视图（通过 mount namespace 或 CoW）

#### 场景 B：共享内存段干扰

```c
// Path A: 创建共享内存用于进程间通信
int fd = shm_open("/agent_state", O_CREAT|O_RDWR, 0666);
ftruncate(fd, 4096);
void *ptr = mmap(NULL, 4096, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
// 写入 Path A 的状态...

// Path B: 打开同一个共享内存段（因为名字相同）
int fd = shm_open("/agent_state", O_RDWR, 0666);  // 读到了 Path A 的数据！
```

#### 场景 C：环境变量和端口冲突

```bash
# Path A: 启动测试服务器在 8080 端口
export PORT=8080
python manage.py runserver 0.0.0.0:$PORT &

# Path B: 也尝试在 8080 端口启动（失败！）
export PORT=8080
python manage.py runserver 0.0.0.0:$PORT &  # Address already in use
```

**预期产出**：
- 3 个具体的状态污染场景 + 日志证据
- 对比表：各隔离机制能否防止每种污染

| 污染类型 | git stash | cp -r | OverlayFS | PID ns | branch context |
|----------|-----------|-------|-----------|--------|---------------|
| /tmp 文件冲突 | ✗ | ✗ | ✗ | ✓ | ✓ |
| 共享内存干扰 | ✗ | ✗ | ✗ | ✓ | ✓ |
| 端口/socket 冲突 | ✗ | ✗ | ✗ | ✓ | ✓ |
| 环境变量泄漏 | ✗ | ✗ | ✗ | ✓ | ✓ |
| 文件系统变更 | 部分 | ✓ | ✓ | ✗ | ✓ |
| **全部覆盖** | ✗ | ✗ | ✗ | ✗ | ✓ |

**关键结论**：OverlayFS 只隔离文件系统，PID namespace 只隔离进程，唯有 branch context 统一解决所有维度。

**Motivate 目标**：R1、R4（完整状态覆盖，不仅是文件系统）

---

### 实验 3.2：内存回收延迟——失败探索路径的资源成本

**目标**：测量当探索路径失败/abort 时，各种机制释放进程内存的速度和完整性。

**实验设计**：

1. **创建一个内存密集型探索路径**：
   ```python
   # 模拟 agent 加载大型测试数据集
   import numpy as np
   data = np.random.randn(100_000_000)  # ~800MB
   # 运行测试...失败了
   # 现在需要 abort 这条探索路径
   ```

2. **测量各机制的 abort + 内存释放时间**：

   | 机制 | abort 操作 | 内存释放方式 |
   |------|-----------|-------------|
   | 手动 kill | `kill -9 <pid>` | 依赖 OS 回收，可能遗漏子进程 |
   | kill 进程组 | `kill -9 -<pgid>` | 进程可逃逸 setsid → 内存泄漏 |
   | cgroup kill | `echo 1 > cgroup.kill` | 可靠，但需 root/delegation |
   | PID namespace 销毁 | 终止 init 进程 | 可靠，但有 init 开销 |
   | branch() abort | 内核终止所有进程 | 可靠 + 立即释放 |

3. **测量指标**：
   - abort 到内存完全释放的延迟（ms）
   - 是否有残留进程仍占用内存
   - 多次 abort 后的累积内存泄漏

**预期产出**：
- 表格：各机制的 abort 延迟和内存释放完整性
- 关键发现：进程组 kill 在有子进程逃逸时留下 X MB 泄漏

**Motivate 目标**：R2（abort 时的资源回收）、R6（进程协调）

---

### 实验 3.3：从 Trace 数据量化多维状态副作用

**目标**：量化 agent 产生的非文件系统状态副作用的频率和规模。

**数据来源**：
- **旧数据**（144 个任务）：从 Bash 命令文本推断状态副作用（粗粒度）
- **新数据**（10-15 个重跑任务）：`ebpf_trace.jsonl` 提供精确的多维状态追踪

**方法**：

1. **从 eBPF 追踪数据直接量化多维状态副作用**（新数据）：

   | eBPF SUMMARY 事件 | 状态维度 | 分析方式 |
   |-------------------|---------|---------|
   | `NET_BIND` + `NET_LISTEN` | 网络端口占用 | 统计 agent 绑定了哪些端口，是否有端口冲突风险 |
   | `NET_CONNECT` | 外部连接 | 统计 API 调用频率（如 pypi.org:443、anthropic API） |
   | `PGRP_CHANGE` + `SESSION_CREATE` | 进程组逃逸 | 直接验证 agent 的子进程是否调用了 setsid/setpgid |
   | `SIGNAL_SEND` | 进程间信号 | agent 是否在重试时 kill 之前的进程 |
   | `PROC_FORK` | 子进程创建 | 量化每次探索尝试创建的进程数 |
   | `MMAP_SHARED` | 共享内存 | 是否有跨进程共享状态 |
   | `DIR_CREATE` + `FILE_DELETE` + `FILE_RENAME` | 文件系统变更 | 精确的 git 盲区量化 |
   | `WRITE` | 写入规模 | total_bytes 量化每次探索的磁盘写入 |

2. **从 Bash 命令推断**（旧数据，作为补充）：

   | 命令模式 | 状态类型 | 频率统计 |
   |----------|---------|---------|
   | `python -m pytest`、`unittest` | 进程内存（加载测试数据） | 已知占 Bash 时间 44-73% |
   | `pip install`、`npm install` | 文件系统 + 进程内存（编译 C 扩展） | 已知占 ~10% |
   | `python manage.py runserver`、`flask run` | 网络端口 + 进程 | 待统计 |
   | `nohup`、`&`（后台进程） | 持久进程状态 | 待统计 |
   | `export`、`source` | 环境变量 | 待统计 |

3. **内存突发规模按探索尝试分组**：
   - 已有数据：P95 内存 spike 518MB（Haiku）/ 234MB（GLM）
   - 新分析：将这些 spike 按探索尝试分组，计算**每条探索路径的独立内存足迹**
   - eBPF 的 PROC_FORK 事件可精确定位每次探索启动了多少子进程

4. **识别"需要回滚但无法回滚"的状态**：
   - eBPF 数据可直接回答：agent 是否在重试时 kill 了之前的进程（SIGNAL_SEND 事件）
   - 是否有进程在 agent 放弃后仍然绑定端口（NET_BIND 事件 vs 进程 EXIT 事件的时序）
   - 是否有进程通过 setsid 逃逸了进程组（SESSION_CREATE 事件）

**预期产出**：
- 表格：各类非文件系统状态副作用的**精确频率**（eBPF 数据，非推断）
- 数字：X% 的任务产生了超出文件系统的状态副作用
- 数字：每个任务平均产生 Y 个进程组变更、Z 个端口绑定
- 数字：并行 N 路探索的内存需求估算
- **关键新数据**：进程组逃逸（setsid/setpgid）的实际发生频率——直接支撑 Table 2

**Motivate 目标**：R1、R4（完整状态覆盖）、R6（进程协调）

---

### 实验 3.4：并发探索的资源扩展性分析

**目标**：估算如果 agent 从串行探索改为并行探索，对系统资源（特别是内存）的影响。用 CoW 感知的模型取代简单的 K×peak 估算。

**方法**：

1. **基于已有数据 + 新实验数据建模**：
   - 从 144 个任务的资源数据中，提取每个任务的：
     - 峰值内存（P_mem）
     - 平均内存（A_mem）
     - 重试次数（N_retry）
     - 框架基线内存（~185MB）
   - 从实验 3.5（内存重叠分析）获取 CoW 共享率

2. **CoW 感知的并行探索资源需求模型**：
   ```
   简单模型（无 CoW）：并行 K 路 = K × P_mem
   容器模型：并行 K 路 = K × (P_mem + 容器开销)
   CoW 模型：并行 K 路 = shared_base + K × private_delta

   其中：
     shared_base = 共享内存（Python runtime + 库 + 基础数据）
     private_delta = 每条探索路径的独有内存（CoW 脏页）

   若 CoW 共享率 = 90%（来自实验 3.5）：
     private_delta = 0.1 × P_mem
     并行 3 路 = shared_base + 3 × 0.1 × P_mem
               ≈ 0.9 × P_mem + 0.3 × P_mem = 1.2 × P_mem

   vs 简单模型：3 × P_mem（2.5 倍的浪费）
   ```

3. **对比资源回收策略**：

   | 策略 | 并行 3 路峰值内存 | abort 后内存 | 说明 |
   |------|-----------------|-------------|------|
   | 无隔离 | 混乱——不可预测 | 不可控 | 状态污染 |
   | 独立容器 | 3 × (P_mem + 开销) | 需等待容器销毁 | 无共享，完全冗余 |
   | CoW branch context | shared + 3 × delta | 立即丢弃脏页 | 共享 90%+ 基线内存 |

4. **可支撑的并行探索数**：
   - 在 128GB 机器上，不同方案能支持多少并行探索路径
   - 容器方案：128GB / P_mem ≈ 128GB / 500MB ≈ 256 路（但每路完整复制）
   - CoW 方案：(128GB - shared) / delta ≈ 远更多路（共享 90%+ 基线）
   - **关键数字**：CoW 比容器方案支持 N 倍的并行探索

**预期产出**：
- 图表：不同并行度下的内存需求（简单复制 vs 容器 vs CoW branch）
- 表格：128GB 机器上各方案支持的最大并行探索数
- 关键结论：CoW 共享基线内存 + abort 立即释放脏页，可支撑 N 倍于容器方案的并行探索
- **与实验 3.5 联动**：CoW 共享率直接决定并行效率

**Motivate 目标**：R1（资源高效的并行探索）、R5（轻量级）

---

### 实验 3.5：探索路径间的内存重叠率（CoW 收益度量）

**目标**：量化两条并行探索路径之间内存页面的重叠比例。这是 CoW 设计的核心论据——重叠越高，CoW 节省越多。

**为什么这比文件系统分析更重要**：文件系统 CoW 的收益很显然（4GB workspace 改 10MB = 99.7% 共享），reviewer 不会被打动。但**内存 CoW 的收益不直观**——两条探索路径的 Python 进程到底共享多少内存？这需要实验数据。

**方法**：

1. **实验设计**：
   ```
   T0: 启动 Python 进程，加载项目 + 依赖（模拟 agent 初始状态）
   T1: fork() → 创建两个子进程（模拟 branch context 的 fork 语义）
   T2: 子进程 A 执行探索策略 1（编辑文件 + 运行 pytest）
       子进程 B 执行探索策略 2（编辑不同文件 + 运行 pytest）
   T3: 定期采样两个进程的内存页面状态
   ```

2. **测量方式**：
   - **`/proc/<pid>/smaps`**：读取每个 VMA 的 PSS（Proportional Set Size）、Shared_Clean、Shared_Dirty、Private_Clean、Private_Dirty
     ```
     overlap_ratio = (Shared_Clean + Shared_Dirty) / RSS
     cow_unique = Private_Dirty / RSS   ← fork 后被写脏的页面
     ```
   - **`/proc/<pid>/pagemap`**（需 root）：读取物理页帧号（PFN），直接比较两个进程的页面映射，计算完全相同的物理页面数量
   - **Minor page faults**：fork 后的 minor fault 数量 = CoW 触发次数 = 被写脏的页面数
     ```
     cow_write_ratio = minor_faults_after_fork / (RSS / PAGE_SIZE)
     ```

3. **测试工作负载**（来自真实 SWE-bench 任务）：
   - **轻量级**：编辑 1 个源文件 + 运行 pytest（大部分 SWE-bench 任务）
   - **中等**：pip install 一个包 + 运行 pytest
   - **重量级**：编译 C 扩展 + 加载大型测试数据 + 运行 pytest

4. **时序采样**：
   - fork 后每 1 秒采样一次 smaps，记录 overlap 随时间的变化
   - 预期：初始 overlap ~100%（刚 fork），逐渐下降到稳态

**预期产出**：
- **核心数字**：探索路径间 X% 的内存页面是共享的（预期 80-95%）
- 图表：overlap_ratio 随时间变化的曲线（fork 后快速下降然后稳定）
- 按工作负载类型的 CoW 收益对比：轻量级 95%+ 共享 vs 重量级 70-80% 共享
- 关键结论：**即使两条路径执行完全不同的策略，仍然共享 X% 的内存——CoW 是正确的设计选择**

**Motivate 目标**：核心设计决策（fork + CoW），R1（内存高效的并行探索）

---

### 实验 3.6：eBPF page fault 追踪——CoW 开销的精确度量

**目标**：用 eBPF 在内核层面追踪 page fault，精确量化 CoW 的实际开销。

**方法**：

1. **eBPF 追踪 page fault**：
   ```c
   // 追踪 handle_mm_fault / do_wp_page（CoW 的核心路径）
   SEC("kprobe/do_wp_page")   // 或 tp/exceptions/page_fault_user
   int trace_cow_fault(struct pt_regs *ctx) {
       u32 pid = bpf_get_current_pid_tgid() >> 32;
       // 按 pid 聚合 minor fault 计数
       // 记录 fault 发生的虚拟地址范围（text/data/heap/stack/mmap）
   }
   ```

2. **度量指标**：
   - 每个进程的 minor fault 计数（= CoW 页面复制次数）
   - fault 的内存区域分布：text（只读，不触发 CoW）vs heap（频繁触发）vs stack vs mmap
   - CoW fault 率随时间的变化（初始高，稳态低）
   - 总 CoW 开销 = minor_faults × page_copy_cost（~1-4μs per page）

3. **与 smaps 数据交叉验证**：
   - minor_faults 应约等于 smaps 中 Private_Dirty 的页面数
   - 两种方法互相印证

**预期产出**：
- 精确的 CoW fault 计数和时序分布
- 按内存区域的 fault 分布（heap 主导 vs text 为零）
- CoW 总开销估算：N 个 fault × Mμs = 总 Xms（预期很小）

**Motivate 目标**：证明 CoW 开销可接受，R5（轻量级）

**注意**：`process_new` 已支持 `--trace-cow`；但该 kprobe 依赖内核符号，建议保留“独立 eBPF 程序”作为备用方案。

---

### 实验 3.7：per-process 内存分解（process_new 已有基础版）

**目标**：在已有 `rss_kb/shared_kb/vm_hwm_kb` 基础上，继续细化到完整 per-process 内存分解（如 text/data/private）。

**方法**：

1. **在 `process_new` 的 EXEC/EXIT 事件处理中扩展内存采集字段**：
   ```c
   // 用户空间：在 handle_event 中，对 EXEC/EXIT 事件读 /proc/pid/statm
   case EVENT_TYPE_PROCESS:
       if (!e->exit_event) {
           // EXEC：记录新进程的初始内存
           read_proc_statm(e->pid, &rss, &shared, &text, &data);
       } else {
           // EXIT：记录进程退出前的峰值内存（从 /proc/pid/status 的 VmHWM）
           read_proc_status(e->pid, &vm_hwm);
       }
   ```

2. **输出格式**：
   ```jsonl
   {"timestamp":123,"event":"EXEC","pid":1234,"comm":"pytest","rss_kb":15360,"shared_kb":12800,"text_kb":2048,"data_kb":512}
   {"timestamp":456,"event":"EXIT","pid":1234,"comm":"pytest","vm_hwm_kb":524288,"duration_ms":5000}
   ```

3. **分析**：
   - 每种进程类型（pytest/pip/python/bash/gcc）的内存分布
   - 内存大户排名：哪些进程消耗了最多内存
   - 进程退出后内存是否被回收（通过 EXIT 时的 VmHWM 对比容器总内存变化）
   - **Shared 比例**：每个进程的 shared/rss 比值——高 shared 意味着 CoW 收益大

**预期产出**：
- per-process 内存分解图：pytest 占 X%，pip 占 Y%，框架占 Z%
- 内存大户 top-10 排名
- 每种进程的 shared/private 比例——直接预测 CoW 收益

**Motivate 目标**：R2（精确的内存回收需求量化）、支撑实验 3.5 的结论

**实现**：基于 `agentsight/bpf/process_new.c` 用户空间代码扩展，无需新增 eBPF 程序。

---

## 实施优先级

| 优先级 | 实验 | 工作量 | 论文影响 | 说明 |
|--------|------|--------|---------|------|
| **P0** | 1.1（探索频率分析） | 低（已有数据） | 高 — 量化核心声明 | 基于已有 trace 数据 |
| **P0** | 2.1（分支创建基准测试） | 中 | 高 — Table 1 核心数据 | 需编写 benchmark 脚本 |
| **P0** | **3.5（内存重叠率）** | 中 | **极高 — CoW 设计的核心论据** | fork + smaps/pagemap 采样 |
| **P0** | 1.3（内存积累 + per-process 分解） | 中 | 高 — motivate 内存隔离 | 旧数据 + 新 eBPF 数据 |
| **P0** | 3.3（多维状态副作用频率） | 低（已有数据） | 高 — 证明不只是文件系统 | eBPF 精确追踪 |
| **P1** | 2.2（进程隔离对比） | 中 | 高 — Table 2 核心数据 | C/Python 测试程序 |
| **P1** | **3.7（per-process 内存分解）** | 低 | 高 — 填补数据缺口 | 在 `process_new` 现有字段上继续增强 |
| **P1** | 3.4（并行资源扩展性） | 低 | 高 — CoW 感知模型 | 依赖 3.5 的共享率数据 |
| **P1** | 1.2（文件变更范围 + CoW 收益） | 低 | 中 — motivate R4 | 基于已有 + eBPF 数据 |
| **P1** | 3.1（内存/端口/IPC 污染演示） | 中 | 中 — 论点扩展 | 3 个具体场景 |
| **P2** | **3.6（eBPF page fault 追踪）** | 中 | 中 — CoW 精确开销 | 可集成到 agentsight |
| **P2** | 3.2（内存回收延迟） | 中 | 中 — 量化 abort 效率 | 内存密集测试 |
| **P2** | 1.4（探索树深度） | 中 | 中 — motivate R3 | Task 嵌套分析 |
| **P3** | 2.3（真实 trace 重放） | 高 | 高 — 但需 BranchFS 集成 | 可先做理论分析 |

> 调研类和文档分析类任务见 `docs/RESEARCH_branchcontext_survey.md`

---

## 关键文件与资源

| 资源 | 路径/URL |
|------|----------|
| 现有 trace 数据（Haiku） | `experiments/all_images_haiku/` |
| 现有 trace 数据（GLM） | `experiments/all_images_local/` |
| 分析脚本（可复用） | `analysis/analyze_swebench_data.py`, `analysis/analyze_new_insights.py` |
| AgentSight submodule | `agentsight/`（eBPF 追踪工具） |
| AgentSight 增强计划 | `agentsight/docs/design/PLAN_process_tracer_extension.md` |
| BranchFS 仓库 | https://github.com/multikernel/branchfs |
| 论文源码 | `paper-repo/main.tex` |

## 实验与 eBPF 追踪事件的对应

| 论文实验 | 需要的 eBPF 事件 | agentsight flag |
|----------|-----------------|----------------|
| 1.2（文件变更范围 + CoW 收益） | DIR_CREATE + FILE_DELETE + FILE_RENAME + WRITE | `--trace-fs` |
| 1.3（内存积累 + per-process） | EXEC/EXIT + rss_kb/shared_kb/vm_hwm_kb | 现有（`process_new`）+ 可选增强 |
| 2.2（进程隔离对比/Table 2） | PGRP_CHANGE + SESSION_CREATE + SIGNAL_SEND | `--trace-signals` |
| 3.1（内存/端口/IPC 污染） | NET_BIND + SIGNAL_SEND + MMAP_SHARED | `--trace-net --trace-signals --trace-mem` |
| 3.3（多维副作用频率） | 全部事件 | `--trace-all` |
| 3.5（内存重叠率） | — | 独立实验（fork + smaps） |
| 3.6（page fault 追踪） | kprobe/do_wp_page | `--trace-cow`（已实现，内核依赖）或独立程序 |
| 3.7（per-process 内存分解） | EXEC/EXIT + /proc/pid/statm | `process_new` 已有基础字段 + 用户空间增强 |

## 验证标准

1. **P0 实验**产出可直接引用在 Section 2 中的具体数字
2. **内存重叠实验（3.5）**必须产出 CoW 共享率——这是论文设计决策的核心数据支撑
3. **基准测试**结果用实测数据替代 Table 1 & 2 中的纯定性对比
4. **内存分析（3.5 + 3.7 + 1.3）**必须完整回答：agent 探索路径的内存有多少可以被 CoW 共享
5. 所有实验可复现，脚本和原始数据一并保存在 `experiments/branchfs_motivation/` 目录下

## 补充分析方向

调研类和纯文档分析类的内容已移至 `docs/RESEARCH_branchcontext_survey.md`，包括：
- 正确性影响量化
- 首次成功率 / 投机成功率（需从 trace 数据分析）
- 现有 Agent 框架隔离机制调研
- 跨工作空间修改分析
- 存储放大分析
- 不可逆外部副作用边界讨论

---

## 论文叙事建议

建议 Section 2 的 motivation 按以下逻辑组织：

1. **Agent 确实在做多路径探索**（实验 1.1 的数据）
2. **每条探索路径产生多维状态副作用**（实验 1.2 文件系统 + 1.3 内存 + 3.3 其他状态）
   - 不只是文件修改，还有内存积累、进程残留、临时资源
3. **现有机制各自覆盖部分维度，但没有统一方案**（实验 2.x + 3.1 的对比表）
   - git 只管文件的子集
   - OverlayFS 管文件系统但需 root
   - PID namespace 管进程但有 init 开销
   - 没有一个方案同时覆盖文件系统 + 内存 + 进程 + 临时资源
4. **CoW 是正确的设计选择**（实验 3.5 的内存重叠数据）
   - 探索路径间共享 X% 的内存页面
   - CoW 使并行探索的内存开销远低于完全复制
5. **并行探索对资源的影响是可管理的**（实验 3.4 的 CoW 感知模型）
   - CoW 共享基线内存，abort 立即丢弃脏页
   - 相比容器方案支持 N 倍的并行探索
