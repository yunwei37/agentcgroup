# Experiment Data

Raw experiment data from SWE-bench trace collection and replay experiments. This data supports the characterization analysis in the paper.

## Directory Layout

| Directory | Description |
|-----------|-------------|
| `all_images_haiku/` | Claude Haiku (cloud API) traces — 72 tasks |
| `all_images_local/` | Local model (GLM 4.7 flash) traces — 72 tasks |
| `batch_swebench_18tasks/` | Batch experiment across 18 task categories |
| `replays/` | Trace replay experiment results |
| `valid_common_tasks.json` | Task list common to both Haiku and local experiments |

## Data Format

Each task directory (e.g., `all_images_haiku/dask__dask-11628/attempt_1/`) contains:

| File | Description |
|------|-------------|
| `trace.jsonl` | Raw Claude Code trace (tool calls, LLM responses) |
| `resources.json` | Resource samples (memory/CPU) collected every second |
| `tool_calls.json` | Extracted tool call sequence with timing |
| `results.json` | Task outcome and metadata |
| `resource_plot.png` | Visualization of resource usage over time |
| `claude_output.txt` | Raw Claude Code stdout |
| `claude_stderr.txt` | Raw Claude Code stderr |
