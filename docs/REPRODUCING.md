# Reproducing Paper Experiments

This document provides step-by-step instructions to reproduce the results in the AgentCgroup paper.

## Environment Requirements

- **Kernel**: Linux 6.12+ with sched_ext support and cgroup v2 enabled
- **Compilers**: clang (for eBPF), gcc, make
- **Libraries**: libbpf-dev, libelf-dev, zlib1g-dev
- **Python**: 3.10+ with packages listed in `requirements.txt`
- **Docker**: Required for container-based experiments (SWE-bench trace replay)
- **Root access**: Required for eBPF and cgroup experiments

```bash
# Install Python dependencies
pip install -r requirements.txt
```

## 1. Characterization Results (Paper Section 4)

The characterization analysis uses pre-collected traces in `experiments/`. All figures and numerical data can be regenerated from the raw data.

```bash
# Full characterization run (generates all figures)
python analysis/characterization.py

# Individual analysis options
python analysis/characterization.py --haiku-only   # Haiku dataset only
python analysis/characterization.py --local-only   # Local model only
python analysis/characterization.py --skip-extended --skip-rq  # fast mode

# Cross-model comparison
python analysis/analyze_haiku_vs_qwen.py
```

**Output**: Figures are written to `analysis/haiku_figures/`, `analysis/qwen3_figures/`, and `analysis/comparison_figures/`.

**Data sources**:
- `experiments/all_images_haiku/` — Claude Haiku API traces (72 tasks)
- `experiments/all_images_local/` — Local model traces (72 tasks)

## 2. CPU Scheduling Experiments (Paper Section 5)

### Build scx_flatcg

```bash
cd scx_flatcg
make
```

Requires: sched_ext-enabled kernel, `third_party/scx` and `third_party/bpftool` submodules initialized.

### Run Evaluation

```bash
cd scx_flatcg/eval

# Setup environment and build workloads
sudo ./setup.sh

# Weight fairness test (cgroup weights 100:200:300, expect ~1:2:3 CPU ratio)
sudo ./tests/test_weight_fairness.sh

# Hierarchical weight flattening test
sudo ./tests/test_hierarchy.sh

# Noisy-neighbor isolation test (compares CFS vs flatcg P99 latency)
sudo ./tests/test_isolation.sh
```

See [scx_flatcg/eval/README.md](../scx_flatcg/eval/README.md) for detailed test descriptions.

## 3. Memory Isolation Experiments (Paper Section 5)

### Build memcg BPF loader

```bash
cd memcg/multi_tenant_test/bpf_loader
make
```

### Run Isolation Comparison

Compares three strategies: no isolation, static memory.max, and BPF-based dynamic isolation.

```bash
cd memcg/multi_tenant_test
sudo ./run_isolation_comparison.sh [--total-mb 1024] [--speed 10] [--runs 3]
```

Results are written to `memcg/multi_tenant_test/isolation_results/`.

## 4. Overhead Measurements (Paper Section 6)

Measures BPF overhead in three scenarios: no BPF (baseline), BPF attached without pressure, and BPF active under memory pressure.

```bash
cd memcg/multi_tenant_test
sudo ./run_overhead_experiment.sh
```

Results are written to `memcg/multi_tenant_test/overhead_results/`.

## 5. Collecting New Traces (Optional)

To collect new SWE-bench traces with resource monitoring:

```bash
cd scripts

# Run a single SWE-bench task with resource monitoring
python run_swebench.py --help

# Batch execution across multiple tasks
python batch_test_swebench.py --help

# Replay a collected trace in a resource-limited container
python replay_trace.py --help
```

See [scripts/README.md](../scripts/README.md) for details on each script.
