# agentcg - eBPF Agent Cgroup Management with Per-Tool-Call Resource Domains

Resource isolation and monitoring for AI agent workloads, with per-tool-call
granularity and bidirectional resource negotiation between agent and OS.

## Key Idea

AI agents execute tool calls (compilers, test runners, git) inside sandboxed
containers. Each tool call has vastly different resource needs — `git status`
uses 13.5 MB while `pytest` can spike to 518 MB. Traditional container-level
resource controls cannot track these tool-call-level dynamics.

AgentCgroup creates **ephemeral child cgroups** for each tool call via a
transparent bash wrapper, enabling per-tool-call resource constraints while
maintaining overall session budgets. eBPF enforcement (sched_ext + memcg_bpf_ops)
provides microsecond-level reaction at kernel enforcement points.

## Architecture

```
agentcgroupd (Python daemon)
  ├── Creates cgroup hierarchy
  │   ├── session_high/                    (high-priority agent session)
  │   │   ├── cgroup.subtree_control: +memory +cpu
  │   │   ├── tool_<pid>_<ts>/            (per-tool-call ephemeral cgroup)
  │   │   ├── tool_<pid>_<ts>/            (another tool call)
  │   │   └── ...
  │   └── session_low/                     (low-priority / throttled)
  ├── Starts scx_flatcg       → CPU scheduling via cgroup weights
  ├── Starts memcg_priority    → Memory isolation via BPF struct_ops
  └── Starts process monitor   → Detects new processes, logs tool call events

bash_wrapper.sh (transparent per-tool-call bridge)
  ├── Intercepts every "bash -c ..." invocation
  ├── Creates ephemeral child cgroup under session_high/
  ├── Parses AGENT_RESOURCE_HINT env var (upward: Agent → System)
  ├── Sets memory.high based on hint
  ├── Executes the actual command
  ├── On OOM (exit 137): injects semantic feedback to stderr (downward: System → Agent)
  ├── Logs metrics to JSONL (duration, peak_mem, hint, exit_code)
  └── Cleans up ephemeral cgroup
```

## Components

| Component | Directory | Description |
|-----------|-----------|-------------|
| **bash_wrapper** | `bash_wrapper.sh` | Per-tool-call cgroup wrapper with bidirectional resource negotiation |
| **agentcgroupd** | `agentcgroupd.py` | Python daemon coordinating eBPF tools and cgroup lifecycle |
| **memcg_controller** | `memcg_controller.py` | Memory controller abstraction (BPF + cgroup v2 fallback) |
| **scx_flatcg** | `scheduler/` | eBPF CPU scheduler (sched_ext) |
| **memcg_priority** | `memcg/` | eBPF memory isolation (memcg_bpf_ops) |
| **process monitor** | `process/` | eBPF process lifecycle monitor |

## Bidirectional Resource Negotiation

Unlike traditional workloads, agents can **negotiate** with the OS about resources:

**Upward (Agent → System):** Agent declares resource needs via `AGENT_RESOURCE_HINT`:

```bash
AGENT_RESOURCE_HINT="memory:low"  bash -c "git status"       # lightweight
AGENT_RESOURCE_HINT="memory:high" bash -c "pytest tests/"     # heavy
AGENT_RESOURCE_HINT="memory:2g"   bash -c "python train.py"   # explicit
```

**Downward (System → Agent):** On OOM, wrapper injects feedback to stderr:

```
[Resource] Command killed (OOM, exit 137). Peak memory: 1800MB.
[Resource] Suggestions: run more targeted operations (e.g., specific test
  files instead of full test suite), reduce data size, or split into
  smaller steps.
```

Agents understand natural language and can make **semantic-level strategy
adjustments** — running a specific test instead of the full suite, for example.

## Quick Start

### Build eBPF Components

```bash
cd agentcg/
make           # builds scheduler, memcg, process monitor
```

### Run the Daemon

```bash
sudo python3 agentcgroupd.py [--cgroup-root PATH] [--no-scheduler] [--no-memcg]
```

### Use the Bash Wrapper

For container deployment (replaces bash in the container):

```bash
cp /usr/bin/bash /usr/bin/real-bash
cp bash_wrapper.sh /usr/bin/bash
chmod +x /usr/bin/bash
export AGENTCG_ROOT="/sys/fs/cgroup/agentcg/session_high"
```

For local testing (no root required):

```bash
export AGENTCG_ROOT="/tmp/agentcg_sim/session_high"
export AGENTCG_LOG="/tmp/agentcg_tools.jsonl"
bash bash_wrapper_local.sh -c "your command here"
```

### Run with SWE-bench

```bash
python3 ../scripts/run_swebench.py <image> --enable-wrapper
```

## Testing

```bash
# Python unit tests (43 tests)
cd agentcg/
python3 -m unittest test_agentcgroupd -v

# Bash wrapper tests (14 tests)
bash test_bash_wrapper.sh

# Live agent semantic validation (Claude Code haiku)
bash test_live_agent.sh
```

## Tool Call Log Format

The wrapper outputs JSONL to `$AGENTCG_LOG`:

```json
{
  "ts": 1771352900389526404,
  "pid": 1278268,
  "cgroup": "/sys/fs/cgroup/agentcg/session_high/tool_1278268_1771352900384005799",
  "cmd": "python3 -m unittest test_calculator -v",
  "exit": 0,
  "duration_ms": 47,
  "peak_mem": "3801088",
  "current_mem": "3801088",
  "hint": "memory:high",
  "mem_high": "max"
}
```

## Directory Structure

```
agentcg/
├── bash_wrapper.sh          # Per-tool-call cgroup wrapper (container deployment)
├── bash_wrapper_local.sh    # Local testing version (no root required)
├── agentcgroupd.py          # Python daemon (coordinates eBPF tools)
├── memcg_controller.py      # Memory controller abstraction (BPF + cgroup fallback)
├── test_agentcgroupd.py     # Python tests (43 tests)
├── test_bash_wrapper.sh     # Bash wrapper tests (14 tests)
├── test_live_agent.sh       # Live agent validation with Claude Code haiku
├── scheduler/               # scx_flatcg CPU scheduler
│   ├── scx_flatcg.bpf.c
│   ├── scx_flatcg.c
│   └── Makefile
├── memcg/                   # memcg_priority memory isolation
│   ├── memcg_priority.bpf.c
│   ├── memcg_priority.c
│   ├── multi_tenant_test/   # Multi-tenant isolation experiments
│   └── Makefile
├── process/                 # Process lifecycle monitor
│   ├── process.bpf.c
│   ├── process.c
│   └── Makefile
├── Makefile                 # Top-level build
└── README.md
```

## Experiment Results

See [docs/exp_log2_per_tool_call.md](../docs/exp_log2_per_tool_call.md) for the
full experiment log. Key findings:

- **Wrapper overhead**: < 5ms per tool call (cgroup create + PID write + rmdir)
- **Agent hint accuracy**: 5/5 = 100% — Claude Haiku correctly classified all
  operations (memory:low for cat/git, memory:high for pytest/unittest)
- **Semantic property checks**: 8/8 passed (unique cgroups, cleanup, tracking)
- **Test coverage**: 57 tests (43 Python + 14 Bash), all passing

## Prerequisites

- Linux kernel with cgroup v2 (required for all features)
- sched_ext support (for CPU scheduler)
- memcg_bpf_ops patches (for in-kernel memory enforcement)
- clang/llvm, libelf-dev, zlib1g-dev (for building eBPF components)
- Python 3.10+ (for daemon and tests)

## Design Documents

- [Plan (中文)](../docs/PLAN_per_tool_call_cgroup.md)
- [Plan (English)](../docs/PLAN_per_tool_call_cgroup_en.md)
- [Design v3](../docs/design/design_v3.md)
