# AgentCgroup Experiment Design (Trace-Driven)

This document describes trace-driven experimental methodology for evaluating AgentCgroup. All experiments use pre-collected traces for reproducibility and to eliminate LLM randomness.

## 1. Why Trace-Driven?

For OS/resource-control evaluation, trace-driven replay is preferred:

| Approach | Pros | Cons |
|----------|------|------|
| **Online (with LLM)** | Realistic agent behavior | Non-deterministic, expensive, slow |
| **Trace-driven replay** | Deterministic, fast, reproducible, no LLM cost | Need pre-collected traces |

**Our approach**: Replay pre-collected tool-call traces to generate reproducible resource pressure patterns.

## 2. Available Trace Sources

### 2.1 Code/CLI Traces (SWE-bench domain)

**OpenHands Trajectories** (Recommended)
- 67k+ agent trajectories with structured tool calls
- Contains: bash commands, file edits, outputs
- Average 64 turns, max 100 turns per trace

```bash
# Download traces
pip install huggingface_hub
python -c "
from huggingface_hub import snapshot_download
snapshot_download(
  repo_id='nebius/SWE-rebench-openhands-trajectories',
  repo_type='dataset',
  local_dir='data/openhands_trajs',
)
"
```

**Trace format** (each trajectory):
```json
{
  "messages": [
    {"role": "assistant", "tool_calls": [
      {"function": {"name": "bash", "arguments": "{\"command\": \"pip install pytest\"}"}}
    ]},
    {"role": "tool", "content": "Successfully installed..."}
  ]
}
```

**SWE-agent Trajectories** (Alternative)
- 80k trajectories
- Solved: avg 31 steps, Unsolved: avg 58 steps

```bash
# Download
python -c "
from huggingface_hub import snapshot_download
snapshot_download(
  repo_id='nebius/SWE-agent-trajectories',
  repo_type='dataset',
  local_dir='data/sweagent_trajs',
)
"
```

### 2.2 Browser Traces (WebArena domain)

**VisualWebArena Human Trajectories**
- 233 tasks with Playwright trace recordings
- Native Playwright trace.zip format

```bash
# Clone and get traces
git clone https://github.com/web-arena-x/visualwebarena.git
# Human traces in trace/ directory as .zip files

# View a trace
playwright show-trace path/to/trace.zip
```

**WebArena Human Demonstrations**
- ~170 tasks with recorded trajectories
- Available via WebArena resource page

**Go-Browse-WA Dataset**
- ~9.5K successful + ~17K failed trajectories on WebArena
- Contains: accessibility tree, HTML, screenshots per step

### 2.3 Trace Statistics

| Source | Traces | Avg Steps | Format |
|--------|--------|-----------|--------|
| OpenHands trajectories | 67k+ | 64 turns | JSON (tool_calls) |
| SWE-agent trajectories | 80k | 31-58 steps | JSON |
| VisualWebArena human | 233 | varies | Playwright zip |
| Go-Browse-WA | 26k+ | varies | JSON + screenshots |

## 3. Trace Replay Architecture

### 3.1 Unified Trace IR

Convert all trace sources to a unified intermediate representation:

```json
{
  "trace_id": "openhands_12345",
  "source": "openhands|sweagent|webarena|visualwebarena",
  "steps": [
    {
      "step_id": 0,
      "tool": "bash",
      "command": "pip install pytest",
      "cwd": "/workspace/repo",
      "timeout_ms": 60000
    },
    {
      "step_id": 1,
      "tool": "bash",
      "command": "python -m pytest tests/ -v",
      "cwd": "/workspace/repo",
      "timeout_ms": 300000
    },
    {
      "step_id": 2,
      "tool": "browser",
      "action": "click",
      "selector": "#submit-button"
    }
  ]
}
```

### 3.2 Trace Converter Scripts

**OpenHands to IR**:
```python
import json

def convert_openhands_trace(trace_path):
    with open(trace_path) as f:
        data = json.load(f)

    steps = []
    for msg in data.get("messages", []):
        if msg.get("role") == "assistant" and "tool_calls" in msg:
            for tc in msg["tool_calls"]:
                func = tc["function"]
                args = json.loads(func["arguments"])
                steps.append({
                    "step_id": len(steps),
                    "tool": func["name"],
                    "command": args.get("command", ""),
                    "timeout_ms": 60000
                })

    return {"trace_id": trace_path, "source": "openhands", "steps": steps}
```

**Playwright trace to IR**:
```python
# Extract actions from Playwright trace zip
# Each action becomes a step with tool="browser"
```

### 3.3 Replay Runner

```python
class TraceReplayRunner:
    def __init__(self, cgroup_controller):
        self.controller = cgroup_controller

    def replay(self, trace_ir, workload_cgroup):
        results = []
        for step in trace_ir["steps"]:
            # Create tool-call cgroup as child
            tool_cgroup = self.controller.create_child(
                workload_cgroup, f"step_{step['step_id']}"
            )

            # Execute tool call in cgroup
            start = time.time()
            if step["tool"] == "bash":
                result = self.run_bash(step["command"], tool_cgroup)
            elif step["tool"] == "browser":
                result = self.run_browser_action(step, tool_cgroup)

            # Collect metrics
            results.append({
                "step_id": step["step_id"],
                "latency_ms": (time.time() - start) * 1000,
                "memory_events": self.read_memory_events(tool_cgroup),
                "cpu_time_ms": self.read_cpu_time(tool_cgroup)
            })

            # Cleanup tool-call cgroup
            self.controller.destroy(tool_cgroup)

        return results
```

## 4. Experiment Design (All Trace-Driven)

### Experiment 1: Domain Mismatch

**Trace source**: OpenHands trajectories (select 50 diverse traces)

**Selection criteria**:
- Mix of short (10-20 steps) and long (50+ steps) traces
- Heterogeneous tool calls (install, build, test, grep, edit)

**Replay configurations**:

| Config | Cgroup Structure | Policy |
|--------|-----------------|--------|
| Static-Env | Single cgroup for entire trace | Fixed limits |
| Static-ToolCall | Per-step child cgroups | Fixed per-step limits |
| AgentCgroup | Per-step child cgroups | Dynamic eBPF policy |

**Metrics collected per step**:
- Wall-clock latency
- CPU time (user + sys)
- Max RSS
- memory.high breach count
- memory.max breach count
- OOM kills

### Experiment 2: Timescale Mismatch

**Trace source**: Filter for "bursty" traces
- Select steps with known high resource usage (pytest, make, npm install)
- Or synthesize burst sequence from real traces

**Replay configurations**:

| Config | Controller | Reaction Time |
|--------|-----------|---------------|
| User-space | Poll PSI every 50ms, write cgroup files | 50-100ms |
| In-kernel | eBPF hooks (sched_ext, memcg_bpf_ops) | <1ms |

**Metrics**:
- Time from memory.high breach to throttle applied
- Interference on co-located workload (measure tail latency)

### Experiment 3: Multi-Tenant Isolation

**Trace sources**:
- Tenant A: VisualWebArena Playwright trace (browser-heavy)
- Tenant B: OpenHands trace (bash-heavy, compile/test)
- Tenant C: Synthetic noisy neighbor (fork bomb / malloc stress)

**Replay**: Run all three concurrently, measure cross-tenant interference.

**Metrics**:
- Per-tenant step latency distribution
- Tenant A (browser) page load time degradation
- Tenant B (bash) test execution time variance

### Experiment 4: Trace Replay Overhead

**Goal**: Measure overhead of trace replay infrastructure itself.

**Method**:
1. Run same trace with no resource control (baseline)
2. Run with static cgroup (minimal overhead)
3. Run with AgentCgroup (measure added overhead)

## 5. Environment Setup for Trace Replay

### 5.1 For SWE-bench Traces

Traces reference specific repos. Use SWE-rebench Docker images:

```bash
# Pre-built images available
docker pull ghcr.io/nebius/swe-rebench:<instance_id>

# Or build from SWE-rebench configs
git clone https://github.com/nebius/SWE-rebench.git
```

**Replay approach**:
1. Start container from pre-built image
2. Replay bash commands from trace inside container
3. Container runs in designated cgroup

### 5.2 For Browser Traces

```bash
# Install Playwright
pip install playwright
playwright install chromium

# For Playwright trace replay
# Extract actions from trace.zip and execute via Playwright API
```

## 6. Trace Selection Guidelines

### For Representative Workload

Select traces that cover:

| Category | Characteristics | Example Commands |
|----------|-----------------|------------------|
| CPU-heavy | Compilation, tests | `make -j4`, `pytest` |
| Memory-heavy | Large data processing | `npm install`, data loading |
| IO-heavy | File operations | `git clone`, `find`, `grep -r` |
| Bursty | Short intense spikes | Quick pytest, browser click |
| Long-running | Sustained load | Full test suite |

### Recommended Trace Subsets

**Quick smoke test**: 5 traces, ~20 steps each
**Development iteration**: 20 traces, mixed complexity
**Paper main results**: 50-100 traces, stratified sample
**Full evaluation**: All available traces (optional)

## 7. Output Format

### Per-Step Metrics (metrics.jsonl)

```json
{"trace_id": "t1", "step_id": 0, "tool": "bash", "cmd": "pip install", "latency_ms": 5234, "cpu_ms": 4100, "max_rss_mb": 256, "mem_high_events": 0}
{"trace_id": "t1", "step_id": 1, "tool": "bash", "cmd": "pytest", "latency_ms": 12456, "cpu_ms": 11200, "max_rss_mb": 512, "mem_high_events": 3}
```

### Aggregate Summary (summary.json)

```json
{
  "config": "agentcgroup",
  "traces": 50,
  "total_steps": 2340,
  "latency_p50_ms": 1234,
  "latency_p95_ms": 8765,
  "latency_p99_ms": 15432,
  "mem_high_events_total": 45,
  "oom_kills": 0
}
```

## 8. Makefile Targets

```makefile
# Download traces
make download-traces-openhands
make download-traces-sweagent
make download-traces-webarena

# Convert to IR
make convert-traces

# Run experiments (all trace-driven)
make exp1-domain-replay      # 50 traces x 3 configs
make exp2-timescale-replay   # Bursty subset x 2 configs
make exp3-multitenant-replay # 3 concurrent tenants
make exp4-overhead           # Overhead measurement

# Generate figures
make figures
```

## 9. Key Differences from Online Execution

| Aspect | Online (with LLM) | Trace Replay |
|--------|------------------|--------------|
| Determinism | Non-deterministic | Fully deterministic |
| Cost | LLM API costs | Free |
| Speed | Slow (API latency) | Fast (local execution) |
| Reproducibility | Hard | Easy |
| Realism | More realistic decisions | Fixed decision sequence |
| Suitable for | Agent capability eval | System/resource eval |

**For AgentCgroup (OS resource control paper)**: Trace replay is the right choice.

## 10. References

### Trace Sources
- OpenHands trajectories: https://huggingface.co/datasets/nebius/SWE-rebench-openhands-trajectories
- SWE-agent trajectories: https://huggingface.co/datasets/nebius/SWE-agent-trajectories
- VisualWebArena: https://github.com/web-arena-x/visualwebarena
- Go-Browse dataset: https://arxiv.org/abs/2506.03533

### Environments
- SWE-rebench Docker: https://huggingface.co/datasets/nebius/SWE-rebench
- BrowserGym: https://github.com/ServiceNow/BrowserGym
- WebArena: https://github.com/web-arena-x/webarena

### Kernel Features
- sched_ext: https://docs.kernel.org/scheduler/sched-ext.html
- memcg_bpf_ops: https://lwn.net/Articles/1055698/
