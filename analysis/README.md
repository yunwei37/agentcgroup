# Data Analysis

Analysis scripts that generate paper figures and numerical results from experiment data.

## Scripts

| Script | Description |
|--------|-------------|
| `characterization.py` | Main runner: generates all characterization figures and data for the paper |
| `analyze_traces.py` | Core trace analysis: resource profiling, tool call patterns |
| `analyze_haiku_vs_qwen.py` | Cross-model comparison (Haiku vs local model) |
| `analyze_rq_validation.py` | Research question validation with statistical tests |
| `analyze_extended_insights.py` | Extended analysis: memory spikes, concurrency patterns |
| `analyze_new_insights.py` | Additional insights beyond core characterization |
| `analyze_tool_time_ratio.py` | Tool call time ratio analysis |
| `analyze_swebench_data.py` | SWE-bench dataset-level analysis |
| `compute_active_time.py` | Compute active vs idle time from traces |
| `filter_valid_tasks.py` | Filter tasks with valid resource data |

## Generated Outputs

| Directory | Contents |
|-----------|----------|
| `haiku_figures/` | Characterization figures from Haiku (cloud API) traces |
| `qwen3_figures/` | Characterization figures from local model traces |
| `comparison_figures/` | Cross-model comparison figures |

## Regenerating Figures

```bash
# Full run (all figures)
python analysis/characterization.py

# Fast mode (skip extended analysis)
python analysis/characterization.py --skip-extended --skip-rq

# Single dataset
python analysis/characterization.py --haiku-only

# Cross-model comparison only
python analysis/analyze_haiku_vs_qwen.py
```
