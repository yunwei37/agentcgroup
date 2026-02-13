# Experiment Scripts

Orchestration scripts for SWE-bench trace collection, replay, and resource monitoring.

## Scripts

| Script | Description |
|--------|-------------|
| `run_swebench.py` | Run a single SWE-bench task with resource monitoring (memory/CPU sampled every second) |
| `batch_test_swebench.py` | Batch execution across multiple tasks (6 categories x 3 difficulties), with resume support |
| `run_all_swebench_images.py` | Run all SWE-bench Docker images from the SWE-rebench dataset |
| `replay_trace.py` | Replay collected traces in containers, preserving original timing (supports concurrent replay) |
| `run_trace_in_container.py` | Execute a trace inside a SWE-rebench Docker container |
| `plot_resources.py` | Plot resource usage (memory/CPU) from monitoring data |
| `parse_claude_trace.py` | Parse Claude Code trace files, extract tool calls and timing info |
| `convert_sweagent_trace.py` | Convert SWE-agent trajectories to unified Trace IR format |
| `verify_sample_tasks.py` | Verify that sample tasks are valid and runnable |
| `run_experiment.sh` | Shell wrapper for running full experiments |
| `run_haiku_experiment.sh` | Shell wrapper for Haiku model experiments |
| `batch_replay.sh` | Batch trace replay script |

## Usage Examples

```bash
# Run a single SWE-bench task
python scripts/run_swebench.py swerebench/sweb.eval.x86_64.encode_1776_starlette-1147

# Batch run all 36 sample tasks
python scripts/batch_test_swebench.py

# Replay a trace at 10x speed
python scripts/replay_trace.py experiments/all_images_haiku/dask__dask-11628/attempt_1 --speed 10.0

# Plot resource usage
python scripts/plot_resources.py experiments/batch_swebench_18tasks/SQL_Data_Easy/attempt_1/resources.json
```
