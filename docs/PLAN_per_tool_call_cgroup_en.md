# Plan: Per-Tool-Call Cgroup + Bash Wrapper + Bidirectional Resource Negotiation

## 1. Problem Diagnosis: Gap Between Paper Claims and Implementation

### 1.1 What the Paper Claims

Section 5 of the paper (`main.tex:285`) states:

> "AgentCgroup organizes resources using a hierarchical cgroup v2 structure where each agent workload maps to a cgroup node with tool calls as child nodes, enabling per-tool-call resource constraints while maintaining overall workload budgets."

And further:

> "AgentCgroup executes control logic directly at kernel cgroup enforcement points via eBPF at the per-tool-call child cgroups described above"

### 1.2 What Is Actually Implemented

`agentcgroupd.py`'s `setup_cgroup_hierarchy()` (line 62-78) only creates two flat cgroups:

```
/sys/fs/cgroup/agentcg/
  session_high/    ← all processes are dumped here
  session_low/
```

`handle_event()` (line 97-121) does one thing for EXEC events: writes the PID into `session_high`. There are no per-tool-call child cgroups.

### 1.3 Core Gaps

| Paper Claim | Actual Implementation | Gap |
|-----------|---------|------|
| Tool calls as child nodes | Only session_high/session_low | No child cgroups |
| Per-tool-call resource constraints | All processes share one cgroup | No isolation granularity |
| Detecting tool-call boundaries | handle_event only assigns PIDs | No boundary detection |
| In-kernel enforcement at per-tool-call cgroups | BPF attached to session_high | Session-level only |

### 1.4 Evaluation Gaps

The current evaluation (`trace_replay.py`) only validates HIGH vs LOW priority isolation between sessions. It does not validate per-tool-call resource control within a single agent.

---

## 2. Design: Bash Wrapper + Per-Tool-Call Ephemeral Cgroup + Bidirectional Resource Negotiation

### 2.1 Core Idea

Each bash tool call gets its own child cgroup. A transparent bash wrapper intercepts tool calls without modifying the agent framework.

### 2.2 Cgroup Hierarchy

```
/sys/fs/cgroup/agentcg/
  session_high/                          ← agent session (existing)
    cgroup.subtree_control: +memory +cpu ← NEW: enable subtree control
    tool_<pid>_<ts>/                     ← NEW: per-tool-call ephemeral cgroup
      memory.peak                        ← peak memory readable
      memory.current                     ← real-time memory
      cgroup.procs                       ← tool process and its children
    tool_<pid>_<ts>/                     ← another tool call
  session_low/                           ← low-priority session (existing)
```

Key points:
- memcg_bpf_ops attached to `session_high` automatically applies to all child cgroups via cgroup v2 hierarchy inheritance
- No BPF code changes needed
- Child cgroup lifecycle strictly aligns with tool call lifecycle

### 2.3 Bash Wrapper Workflow

```
Claude Code emits: bash -c "pytest tests/"
  → wrapper is invoked (replaces original bash)
  → parse resource hint (if agent declared via env var)
  → create child cgroup: session_high/tool_<pid>_<timestamp>/
  → write own PID into child cgroup
  → execute /usr/bin/real-bash -c "pytest tests/"
  → command finishes
  → if exit code 137 (OOM): write resource feedback to stderr
  → log resource usage to JSONL file
  → cleanup child cgroup
```

### 2.4 Bidirectional Resource Negotiation

**This is a property that traditional workloads completely lack.** An agent is not a passive recipient of resource limits — it is an intelligent entity that can:

1. **System → Agent (downward feedback)**: When resources are insufficient, the wrapper tells the agent what happened and suggests what to do
2. **Agent → System (upward declaration)**: When issuing a tool call, the agent declares resource need hints via environment variables

#### 2.4.1 Downward Feedback: System → Agent

When a tool call encounters resource issues, the wrapper injects semantic feedback into stderr:

```
[Resource] Command killed (OOM, exit 137). Peak memory: 1800MB.
[Resource] The command exceeded the available memory budget.
[Resource] Suggestions: run more targeted operations (e.g., specific test
  files instead of full test suite), reduce data size, or split into
  smaller steps. You can also request more memory by setting
  AGENT_RESOURCE_HINT="memory:<size>g" before the command.
```

This is not simply "telling the application that resources are tight" (PSI notifications can do that). The key difference:
- PSI is a numeric signal that traditional applications cannot interpret semantically
- An agent can understand natural language suggestions and make **semantic-level behavioral adjustments** (run a specific test instead of the full suite)

#### 2.4.2 Upward Declaration: Agent → System

The agent can declare resource needs via environment variables in its tool calls:

```bash
AGENT_RESOURCE_HINT="memory:high" bash -c "pytest tests/"
AGENT_RESOURCE_HINT="memory:low"  bash -c "git status"
AGENT_RESOURCE_HINT="memory:2g"   bash -c "python train.py"
```

The wrapper parses these hints and sets the child cgroup's `memory.high` accordingly:

| Hint | memory.high Setting | Meaning |
|------|-----------------|------|
| `memory:low` | 256MB | Agent considers this a lightweight operation |
| `memory:medium` (default) | 1GB | Normal operation |
| `memory:high` | Unlimited | Agent expects high memory usage |
| `memory:<N>g` | N GB | Agent explicitly declares the amount needed |

**Why this works:**

The agent has semantic understanding of the commands it is about to execute. It knows:
- `git status` needs almost no memory → `memory:low`
- `pytest tests/` may need significant memory → `memory:high`
- `python -c "import pandas; df = pd.read_csv('large.csv')"` → `memory:2g`

These declarations don't need to be precise — they are hints. The system can use them for better resource allocation decisions. If the agent underestimates, the downward feedback mechanism informs it, and the agent can adjust its hint next time.

#### 2.4.3 Negotiation Loop

```
Round 1:
  Agent: AGENT_RESOURCE_HINT="memory:medium" bash -c "pytest tests/"
  System: [OOM, exit 137, peak 1.8GB] → stderr feedback

Round 2:
  Agent reads feedback, understands it needs more memory or smaller scope
  Choice A: AGENT_RESOURCE_HINT="memory:2g" bash -c "pytest tests/"      ← request more resources
  Choice B: bash -c "pytest tests/test_specific.py -x"                    ← reduce operation scope

Round 3:
  Completes successfully
```

This is an adaptive negotiation loop:
- The agent has semantic knowledge (what commands do)
- The system has resource knowledge (how much is available, who is competing)
- Both sides exchange information through a simple protocol to reach optimal execution

### 2.5 Core Argument: Semantic Elasticity

Agent workloads possess a property that traditional workloads completely lack: **semantic elasticity**.

Traditional resource management is unidirectional: the system imposes limits, the application passively endures them. PSI notifications can tell an application "resources are tight," but the application's response options are very limited — either reduce quality (lower video bitrate) or queue and wait. These responses are parameter adjustments **within the same execution strategy**.

Agents are different. They simultaneously possess:
1. **Semantic understanding**: Knows that "run pytest tests/" and "run pytest tests/test_specific.py -x" achieve similar purposes but differ by orders of magnitude in resource consumption
2. **Strategy generation**: Can re-plan execution strategies based on natural language feedback
3. **Demand forecasting**: Has prior knowledge about commands to be executed (knows git status is lightweight, pytest may be heavy)

Based on these three properties, resource management can transform from unidirectional control to **bidirectional negotiation**:

- **Agent → System**: The agent declares resource hints ("I'm about to run pytest, may need 2GB memory"), enabling the system to make more precise resource pre-allocation and scheduling decisions
- **System → Agent**: When resources are insufficient, the system not only reports "OOM occurred" but suggests alternative strategies in natural language ("please try running more targeted tests"), which the agent can understand and execute

This is not simply "making applications resource-aware" — traditional applications, when resource-aware, can only adjust parameters (thread count, buffer size). Agents can perform **semantic-level strategy reconstruction**: using a completely different approach to achieve the same goal.

---

## 3. Implementation Details

### 3.1 Files Modified

| File | Change Type | Description |
|------|---------|------|
| `agentcg/bash_wrapper.sh` | **New** | Bash wrapper: per-tool-call cgroup create/destroy + bidirectional negotiation |
| `agentcg/agentcgroupd.py` | Modified | `setup_cgroup_hierarchy` enables subtree_control; `handle_event` logs only; added `scan_tool_cgroups` |
| `agentcg/memcg_controller.py` | Modified | `poll()` scans child cgroups; added `_manage_tool_cgroups()` |
| `scripts/run_swebench.py` | Modified | Container wrapper installation (bind mount + PATH setup) |
| `agentcg/test_agentcgroupd.py` | Modified | Updated tests for new behavior; added per-tool-call tests |
| `agentcg/test_bash_wrapper.sh` | **New** | Bash wrapper unit tests (14 tests) |

### 3.2 Key Design Decisions

1. **Wrapper as bridge, not daemon modification**: The wrapper approach is transparent to the agent framework and requires no code changes to Claude Code or any other agent.

2. **Ephemeral cgroups with unique naming**: Each tool call gets `tool_<pid>_<nanosecond_timestamp>` ensuring no naming collisions even under concurrent execution.

3. **BPF inheritance via cgroup hierarchy**: memcg_bpf_ops attached to `session_high` automatically applies to all child cgroups. No BPF code changes needed.

4. **Graceful degradation**: If cgroup operations fail (e.g., not running in a cgroup environment), the wrapper silently falls back to passthrough mode.

---

## 4. Evaluation Plan

### Experiment 1: Wrapper Overhead Microbenchmark

**Goal**: Prove wrapper overhead is negligible.

**Method**:
- Run 1000x `bash -c "echo hello"`: with wrapper vs without
- Run 100x `bash -c "git status"`: with wrapper vs without
- Measure per-invocation overhead (cgroup mkdir + echo PID + rmdir)

**Expected**: < 5ms overhead per tool call

### Experiment 2: Per-Tool-Call Resource Visibility

**Goal**: Validate per-tool-call cgroups accurately measure each tool call's resources.

**Method**:
- Enable wrapper during real agent execution
- Select 5-10 tasks (covering test execution, git ops, package install)
- Collect each tool call's `memory.peak`, `memory.current` time series
- Compare against characterization-stage container-level trace data

### Experiment 3: Per-Tool-Call Limits Reduce Resource Waste

**Goal**: Validate per-tool-call limits are more efficient than container-level limits.

**Method**:
- Multiple agents concurrent, total memory constrained
- Compare: container-level `memory.high` vs per-tool-call child cgroup `memory.high`
- Use trace replay (consistent with existing evaluation infrastructure)

**Metrics**: OOM count, resource utilization ratio, high-priority P95 latency

### Experiment 4: Bidirectional Resource Negotiation Effectiveness

**Goal**: Validate upward hints + downward feedback improve agent behavior under resource constraints.

**Method**:
- Agent executes tasks under 1GB memory limit
- Compare conditions:
  - A) No feedback: agent only sees "Killed" on OOM
  - B) Downward only: wrapper outputs semantic suggestions on OOM
  - C) Bidirectional: agent declares hints + receives OOM feedback
- Select 10-15 tasks that trigger OOM

**Metrics**: Task completion rate, retry count, total execution time, peak memory utilization

### Experiment 5: Resource Hint Accuracy Analysis

**Goal**: Quantify whether agent resource hints have value.

**Method**:
- Analyze agent-declared hints vs actual resource usage
- Compute hint accuracy rate (underestimate / overestimate / match)
- Correlate hint accuracy with task success rate

---

## 5. Paper Modification Plan

### 5.1 Design Section Expansion

Current three paragraphs expand to full subsections:

```
5.1 Per-Tool-Call Resource Domains
  5.1.1 Bash Wrapper as Agent-OS Bridge
  5.1.2 Hierarchical Cgroup Structure
  5.1.3 Bidirectional Resource Negotiation

5.2 In-Kernel Enforcement (existing, minor adjustments)

5.3 Runtime-Adaptive Policies (existing, minor adjustments)
```

### 5.2 Evaluation Section Expansion

Add Experiments 1-5 results, particularly:
1. Wrapper overhead → proves feasibility
2. Per-tool-call resource visibility → core claim validation
3. Per-tool-call limits → better than container-level
4. Bidirectional negotiation → most novel result
5. Hint accuracy → understanding agent resource awareness

### 5.3 New Discussion: Semantic Elasticity

Add discussion on bidirectional resource negotiation as a fundamentally new property of agent workloads, contrasting with traditional unidirectional resource management.

---

## 6. Implementation Sequence

**Phase 1 (1 day)**: Core implementation
1. Write `bash_wrapper.sh` ✅
2. Modify `agentcgroupd.py` ✅
3. Modify `memcg_controller.py` ✅
4. Experiment 1: overhead microbenchmark

**Phase 2 (1 day)**: Integration and basic evaluation
5. Modify `run_swebench.py` ✅
6. Experiment 2: per-tool-call resource visibility (live agent, 5-10 tasks)
7. Experiment 3: per-tool-call limits vs container limits (trace replay)

**Phase 3 (1-2 days)**: Bidirectional negotiation evaluation
8. Experiment 4: bidirectional resource negotiation (live agent, 10-15 tasks)
9. Experiment 5: resource hint accuracy analysis

**Phase 4 (parallel)**: Paper modification
10. Design section expansion
11. Evaluation section expansion
12. New discussion: Semantic Elasticity

---

## 7. Risks and Mitigations

### Risk 1: cgroup subtree_control and "no internal processes" constraint

**Risk**: Enabling `subtree_control` on `session_high` means processes cannot reside directly in `session_high` (cgroup v2 constraint) — all must be in child cgroups.

**Mitigation**: The wrapper ensures all `bash -c` calls move into child cgroups. For non-bash processes (e.g., Node.js agent runtime), create a permanent `tool_framework/` child cgroup.

### Risk 2: Wrapper impact on non-tool-call bash invocations

**Risk**: Other bash calls in the container may not be tool calls.

**Mitigation**: The wrapper only activates for `bash -c "..."` pattern. Interactive bash and other invocation patterns pass through directly.

### Risk 3: Resource hints ignored by agent

**Risk**: Claude Code may not set `AGENT_RESOURCE_HINT` environment variables.

**Mitigation**: Hints are optional. Without hints, defaults are used (`max`). Downward feedback still works even without upward declarations. During evaluation, prompts can guide agents to use hints.

### Risk 4: Concurrent cgroup create/delete races

**Risk**: Multiple concurrent tool calls may race on cgroup operations.

**Mitigation**: Each wrapper uses a unique cgroup path (`tool_<pid>_<timestamp>`), preventing collisions. `rmdir` fails safely on non-empty cgroups (meaning child processes are still running).
