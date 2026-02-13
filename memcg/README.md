# Memory Controller (memcg)

eBPF-based memory cgroup controller using `memcg_bpf_ops` (struct_ops) for dynamic, priority-aware memory isolation of AI agent sessions.

## Components

- `patches.mbox` — Kernel patches for memcg_bpf_ops support
- `multi_tenant_test/` — Multi-tenant isolation experiments and BPF loader
  - `bpf_loader/` — eBPF program and userspace loader for memcg priority control
  - `run_isolation_comparison.sh` — Compare no-isolation, static, and BPF strategies
  - `run_overhead_experiment.sh` — Measure BPF overhead (allocation latency, CPU)
  - `trace_replay.py` — Memory trace replay for controlled experiments
  - `memory_stress.py` — Memory stress workload generator

## Building

```bash
cd memcg/multi_tenant_test/bpf_loader
make
```

Requires: Linux kernel with memcg_bpf_ops support, clang, libbpf-dev, libelf-dev.

## Running Experiments

```bash
cd memcg/multi_tenant_test

# Compare isolation strategies (no isolation vs static vs BPF)
sudo ./run_isolation_comparison.sh [--total-mb 1024] [--speed 10] [--runs 3]

# Measure BPF overhead
sudo ./run_overhead_experiment.sh
```

## Documentation

- [AGENT_EXPERIMENT_DESIGN.md](AGENT_EXPERIMENT_DESIGN.md) — Experiment design (Chinese)
- [EXPERIMENT_REPORT.md](EXPERIMENT_REPORT.md) — Detailed experiment results
- [REPLAY_COMBINATION_ANALYSIS.md](REPLAY_COMBINATION_ANALYSIS.md) — Replay strategy analysis
- [multi_tenant_test/EXPERIMENT_PLAN.md](multi_tenant_test/EXPERIMENT_PLAN.md) — Multi-tenant experiment plan
- [multi_tenant_test/RESULTS_SUMMARY.md](multi_tenant_test/RESULTS_SUMMARY.md) — Results summary
