# Characterization 数据与脚本说明

本文档说明 Section 3 (Characterization) 中使用的原始数据来源、分析脚本和生成的图表之间的对应关系。

## 1. 原始数据

所有实验数据位于 `experiments/` 目录下，每个任务的 `attempt_1/` 子目录包含以下文件：

| 文件 | 内容 |
|------|------|
| `results.json` | 执行结果、Claude 输出（stdout/stderr）、退出码、资源采样序列 |
| `resources.json` | 聚合后的资源统计（最小/最大/平均 CPU 和内存） |
| `tool_calls.json` | 每个工具调用的类型、开始时间、结束时间 |
| `trace.jsonl` | Claude Code 完整执行 trace（JSONL 格式） |
| `resource_plot.png` | 单任务 CPU/内存时序图 |

### 数据集

| 数据集 | 路径 | 模型 | 任务数 | 用途 |
|--------|------|------|--------|------|
| batch_swebench_18tasks | `experiments/batch_swebench_18tasks/` | Claude Code + Haiku | 18 (6 类别 × 3 难度) | 主要 characterization 数据 |
| all_images_local | `experiments/all_images_local/` | Claude Code + GLM 4.7 flash (本地) | 102+ | 扩展数据集、模型对比 |

18 个任务覆盖：CLI_Tools、DevOps_Build、ML_Scientific、Medical_Bio、SQL_Data、Web_Network，每类别 Easy/Medium/Hard 各一个。

## 2. 实验执行脚本

### 核心执行流程

```
run_experiment.sh / run_haiku_experiment.sh   （启动与监控）
  └─→ run_all_swebench_images.py              （批量任务调度）
      └─→ run_swebench.py                     （单任务执行 + 资源采集）
```

| 脚本 | 路径 | 功能 |
|------|------|------|
| `run_swebench.py` | `scripts/run_swebench.py` | 单任务执行器：拉取 Docker 镜像 → 启动 Podman 容器 → 运行 Claude Code → 每 1 秒通过 `podman stats --no-stream` 采样 CPU/内存 → 记录工具调用 trace → 输出 results.json, resources.json, tool_calls.json |
| `batch_test_swebench.py` | `scripts/batch_test_swebench.py` | 批量执行 36 个预定义任务（6 类别 × 3 难度 × 2 任务），支持重试和断点续传 |
| `run_all_swebench_images.py` | `scripts/run_all_swebench_images.py` | 大规模执行 SWE-rebench 所有可用 Docker 镜像（102+ 任务），支持 `--model haiku` 或 `--model qwen3` |
| `run_experiment.sh` | `scripts/run_experiment.sh` | Qwen/GLM 实验自动化：启动 llama-server（GLM-4.7-Flash-GGUF:Q4_K_M，端口 8080）+ 健康检查 + 自动重启 |
| `run_haiku_experiment.sh` | `scripts/run_haiku_experiment.sh` | Haiku 实验自动化：通过 Anthropic API 执行，健康检查 + 日志轮转 |
| `plot_resources.py` | `scripts/plot_resources.py` | 根据 resources.json 绘制单任务 CPU/内存时序图 |

### Trace 回放工具

| 脚本 | 路径 | 功能 |
|------|------|------|
| `replay_trace.py` | `scripts/replay_trace.py` | 在容器中回放 trace.jsonl 中的所有工具调用，同时采集资源使用 |
| `batch_replay.sh` | `scripts/batch_replay.sh` | 批量回放 18 个任务的 trace |
| `parse_claude_trace.py` | `scripts/parse_claude_trace.py` | 解析 Claude Code trace 文件，提取工具调用和时间戳 |

## 3. 分析脚本与生成图表

### 3.1 analyze_swebench_data.py

**路径**: `analysis/analyze_swebench_data.py`
**输入**: `experiments/batch_swebench_18tasks/` 或 `experiments/all_images_local/`
**输出**: `analysis/haiku_figures/` (28 PNG) 或 `analysis/qwen3_figures/` (28 PNG)

```bash
python analysis/analyze_swebench_data.py --all                    # Haiku 全部分析
python analysis/analyze_swebench_data.py --dataset qwen3 --all    # Qwen 全部分析
```

**生成图表与论文/文档对应关系：**

| 生成图表 | 论文 Figure | characterization.md 章节 | 分析内容 |
|----------|-------------|--------------------------|----------|
| `rq1_resource_timeseries.png` | Fig. timeseries | 3.3 时间动态性 | ML 任务内存 2.9GB/秒变化 |
| `rq1_change_rate_distribution.png` | Fig. changerate | 3.3 时间动态性 | 资源变化率分布：最大内存变化 3GB/s，CPU 变化 >50%/s |
| `rq1_timescale_mismatch.png` | — | 3.3 时间动态性 | 突发事件频率/幅度 |
| `rq2_category_boxplots.png` | Fig. categories | 3.3 异构性 | 峰值内存 197MB–4GB，CV=147% |
| `rq2_domain_mismatch.png` | — | 3.3 异构性 | 任务类别间资源差异 |
| `rq2_category_heatmap.png` | — | 3.3 异构性 | 资源使用热力图 |
| `rq3_tool_analysis.png` | — | 3.2 RQ1 | 工具调用模式分析 |
| `rq4_overprovisioning.png` | Fig. overprovisioning | 3.4 RQ3 | CPU 浪费 76%–93%，过度供给 4.1×–13.6× |

### 3.2 analyze_tool_time_ratio.py

**路径**: `analysis/analyze_tool_time_ratio.py`
**输入**: `experiments/all_images_local/` (默认) 或通过 `--data-dir` 指定
**输出**: 14 张 chart 图表

```bash
python analysis/analyze_tool_time_ratio.py
python analysis/analyze_tool_time_ratio.py --data-dir experiments/batch_swebench_18tasks --figures-dir analysis/haiku_figures
```

| 生成图表 | characterization.md 章节 | 分析内容 |
|----------|--------------------------|----------|
| `chart_01_repo_success_rate.png` | 3.2 RQ1 | 任务成功率 |
| `chart_02_time_distribution.png` | 3.2 RQ1 | 执行时间分布 |
| `chart_03_tool_ratio_distribution.png` | 3.2 阶段划分 | 工具执行时间占比：平均 28.2%，范围 0.1%–73.3% |
| `chart_04_tool_usage_breakdown.png` | 3.2 工具执行时间差异 | Bash 平均 2.64s，Task 平均 66.16s，Read/Edit <0.1s |
| `chart_05_tool_timeline.png` | 3.2 工具使用时间分布 | Read 集中前 30%，Bash 集中 40%–80% |
| `chart_06_bash_categories.png` | 3.2 工具类型分布 | 测试 44.1%，Python 26.7%，安装 10.9% |
| `chart_07_resource_boxplots.png` | 3.3 异构性 | 资源使用箱线图 |
| `chart_08_time_breakdown.png` | 3.2 RQ1 | 时间分解 |
| `chart_09_overhead_analysis.png` | 3.2 磁盘与启动开销 | 启动开销分析 |
| `chart_10_memory_trajectory.png` | 3.4 聚合内存轨迹 | 归一化内存轨迹：前半段稳定 ~200MB，后半段上升 |
| `chart_11_cpu_utilization.png` | 3.3 时间动态性 | CPU 利用率模式 |
| `chart_12_bash_time_by_category.png` | 3.2 工具语义决定资源消耗 | Medical_Bio 4GB vs Web_Network 291MB (13.7×) |
| `chart_13_memory_peak_timing.png` | 3.3 时间动态性 | 内存峰值出现时机（早期/中期/后期） |
| `chart_14_scatter_time_ratio.png` | 3.2 RQ1 | 工具时间占比散点图 |

### 3.3 analyze_haiku_vs_qwen.py

**路径**: `analysis/analyze_haiku_vs_qwen.py`
**输入**: `experiments/batch_swebench_18tasks/` (Haiku) + `experiments/all_images_local/` (Qwen)
**输出**: `analysis/comparison_figures/` (6 PNG)

```bash
python analysis/analyze_haiku_vs_qwen.py
```

| 生成图表 | 论文 Figure | characterization.md 章节 | 分析内容 |
|----------|-------------|--------------------------|----------|
| `01_success_rate_by_category.png` | — | 3.3 异构性 | Haiku 94.4% vs Qwen 44.4% |
| `02_execution_time_comparison.png` | — | 3.3 异构性 | Haiku 400s vs Qwen 607s |
| `03_peak_memory_comparison.png` | — | 3.3 异构性 | 峰值内存对比 |
| `04_cpu_utilization_comparison.png` | Fig. cpudiff | 3.3 异构性 | CPU 利用率 Haiku 30.6% vs Qwen 7.9% (3.9×) |
| `05_time_vs_memory_scatter.png` | — | 3.3 异构性 | 时间-内存散点图 |
| `06_overall_comparison.png` | — | 3.3 异构性 | 总体指标对比 |

### 3.4 analyze_extended_insights.py

**路径**: `analysis/analyze_extended_insights.py`
**输入**: 同上
**输出**: 可复用的分析函数（按需调用，不自动生成图表）

```bash
python analysis/analyze_extended_insights.py --haiku     # Haiku 数据集
python analysis/analyze_extended_insights.py --qwen      # Qwen 数据集
python analysis/analyze_extended_insights.py --compare    # 模型对比
```

提供的分析函数及其对应 characterization.md 章节：

| 函数 | 对应章节 | 分析内容 |
|------|----------|----------|
| `analyze_disk_and_startup_overhead()` | 3.2 磁盘与启动开销 | 镜像 2.9–17.7GB，权限修复平均 28.3s |
| `analyze_transient_bursts()` | 3.3 瞬态突发特征 | Medical_Bio_Hard 峰值 4060MB，平均 264MB，过度供给 15.4× |
| `analyze_cpu_memory_correlation()` | 3.3 CPU 与内存正相关性 | 相关系数 91%–95% |
| `analyze_retry_loop_patterns()` | 3.3 重试循环模式 | 20–51 个重试组，Bash 密度 61.8% |
| `analyze_tool_timeline_distribution()` | 3.2 工具使用时间分布 | 10 阶段工具调用分布 |
| `analyze_local_vs_api_inference()` | 3.3 本地推理 vs API 推理 | CPU>50% 采样点：Haiku 21.2% vs Qwen 0.5% |
| `analyze_concurrency_potential()` | 3.3 异构性 | 理论并发：Haiku 3 实例 vs Qwen 12 实例 |
| `analyze_memory_trajectory()` | 3.4 聚合内存轨迹 | 归一化内存使用趋势 |
| `analyze_tool_semantic_variance()` | 3.2 工具语义决定资源消耗 | 相同 Bash 调用资源差异 13.7× |

### 3.5 analyze_rq_validation.py

**路径**: `analysis/analyze_rq_validation.py`
**输入**: 任意实验数据目录
**输出**: RQ1–RQ4 验证图表 + 统计数据

```bash
python analysis/analyze_rq_validation.py --all
python analysis/analyze_rq_validation.py --data-dir experiments/batch_swebench_18tasks --figures-dir analysis/haiku_figures
```

功能与 `analyze_swebench_data.py` 中的 RQ 分析部分重叠，主要用于独立验证论文中的具体数据声明。

## 4. 论文图表引用一览

论文 `main.tex` 中引用的图表与生成脚本对应关系：

| LaTeX label | 图表文件 | 生成脚本 |
|-------------|----------|----------|
| `fig:timeseries` | `rq1_resource_timeseries.png` | `analyze_swebench_data.py --dynamics` |
| `fig:change_rate` | `rq1_change_rate_distribution.png` | `analyze_swebench_data.py --dynamics` |
| `fig:categories` | `rq2_category_boxplots.png` | `analyze_swebench_data.py --domain` |
| `fig:cpu_diff` | `04_cpu_utilization_comparison.png` | `analyze_haiku_vs_qwen.py` |
| `fig:overprovisioning` | `rq4_overprovisioning.png` | `analyze_swebench_data.py --efficiency` |
| `fig:tool_ratio` | `chart_03_tool_ratio_distribution.png` | `analyze_tool_time_ratio.py` |
| `fig:bash_categories` | `chart_06_bash_categories.png` | `analyze_tool_time_ratio.py` |
| `fig:tool_time` | `chart_04_tool_usage_breakdown.png` | `analyze_tool_time_ratio.py` |
| `fig:tool_timeline` | `chart_05_tool_timeline.png` | `analyze_tool_time_ratio.py` |
| `fig:peak_timing` | `chart_13_memory_peak_timing.png` | `analyze_tool_time_ratio.py` |
| `fig:memory_trajectory` | `chart_10_memory_trajectory.png` | `analyze_tool_time_ratio.py` |

## 5. 一键重现

```bash
# 1. 生成 Haiku 分析图表
python analysis/analyze_swebench_data.py --all
python analysis/analyze_tool_time_ratio.py --data-dir experiments/batch_swebench_18tasks --figures-dir analysis/haiku_figures

# 2. 生成 Qwen/GLM 分析图表
python analysis/analyze_swebench_data.py --dataset qwen3 --all
python analysis/analyze_tool_time_ratio.py

# 3. 生成模型对比图表
python analysis/analyze_haiku_vs_qwen.py

# 4. 生成扩展分析
python analysis/analyze_extended_insights.py --compare
```
