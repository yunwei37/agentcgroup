# AgentCgroup: Understanding and Controlling OS Resources of AI Agents

**[\[Paper (arXiv)\]](https://arxiv.org/abs/2602.09345)**

AI agents are increasingly deployed in multi-tenant cloud environments, where they execute diverse tool calls within sandboxed containers, each with distinct resource demands and rapid fluctuations. This repository contains the implementation and experimental artifacts for **AgentCgroup**, an eBPF-based resource controller for fine-grained OS-level resource isolation of AI coding agents.

Our work includes:

- A **systematic characterization** of OS-level resource dynamics in sandboxed AI coding agents, analyzing 144 software engineering tasks from the SWE-rebench benchmark across two LLM models.
- **AgentCgroup**, an eBPF-based resource controller using sched_ext (CPU) and memcg_bpf_ops (memory) for in-kernel, tool-call-granularity resource enforcement.

## Repository Structure

```
agentcgroup/
├── agentcg/             # AgentCgroup controller: daemon, bash wrapper, eBPF components
│   ├── agentcgroupd.py  # Python daemon coordinating eBPF tools and cgroup lifecycle
│   ├── bash_wrapper.sh  # Per-tool-call cgroup wrapper (container deployment)
│   ├── scheduler/       # scx_flatcg CPU scheduler (sched_ext)
│   ├── memcg/           # memcg_priority memory isolation (memcg_bpf_ops)
│   └── process/         # Process lifecycle monitor
├── scx_flatcg/          # eBPF sched_ext CPU scheduler (standalone)
│   └── eval/            # Evaluation framework: tests, workloads, benchmarks
├── memcg/               # eBPF memory controller (memcg struct_ops) + experiments
│   └── multi_tenant_test/  # Multi-tenant isolation experiments & BPF loader
├── scripts/             # Experiment orchestration: SWE-bench trace collection & replay
├── analysis/            # Data analysis scripts and generated paper figures
├── experiments/         # Raw experiment data (SWE-bench traces, resource logs)
├── docs/                # Design documents, experiment methodology, setup guides
├── third_party/         # Git submodules: scx, bpftool
└── paper-repo/          # LaTeX paper source (separate submodule)
```

## Getting Started

### Prerequisites

- Linux kernel with sched_ext support (6.12+)
- cgroup v2 enabled
- Python 3.10+
- clang, gcc, make
- libbpf-dev, libelf-dev
- Docker (for container experiments)

### Clone

```bash
git clone --recurse-submodules https://github.com/yunwei37/agentcgroup.git
cd agentcgroup
```

### Python Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Building the CPU Scheduler (scx_flatcg)

```bash
cd scx_flatcg
make
```

### Building the Memory Controller (memcg BPF loader)

```bash
cd memcg/multi_tenant_test/bpf_loader
make
```

### Building the AgentCgroup Controller

```bash
cd agentcg
make           # builds scheduler, memcg, and process monitor
```

### Running the AgentCgroup Daemon

```bash
cd agentcg
sudo python3 agentcgroupd.py [--cgroup-root PATH] [--no-scheduler] [--no-memcg]
```

See [agentcg/README.md](agentcg/README.md) for full usage details including the bash wrapper and per-tool-call resource negotiation.

## Reproducing Paper Experiments

See [docs/REPRODUCING.md](docs/REPRODUCING.md) for step-by-step instructions to reproduce the results in the paper, including:

1. **Characterization results** (Section 4): Resource profiling and analysis across 144 tasks
2. **Resource control experiments** (Section 5): Multi-tenant CPU and memory isolation
3. **Overhead measurements** (Section 6): Controller overhead microbenchmarks

## Citation

If you find this work useful, please cite our paper:

```bibtex
@article{zheng2026agentcgroup,
  title     = {AgentCgroup: Understanding and Controlling OS Resources of AI Agents},
  author    = {Zheng, Yusheng and Fan, Jiakun and Fu, Quanzhi and Yang, Yiwei and Zhang, Wei and Quinn, Andi},
  journal   = {arXiv preprint arXiv:2602.09345},
  year      = {2026},
  url       = {https://arxiv.org/abs/2602.09345},
}
```

## License

This project is licensed under the [GPL-2.0 License](LICENSE).
