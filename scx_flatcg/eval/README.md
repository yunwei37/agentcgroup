# scx_flatcg Evaluation

This directory contains tests and benchmarks to validate the scx_flatcg scheduler.

## Quick Start

```bash
# Setup environment and build workloads
sudo ./setup.sh

# Run all tests
sudo ./tests/test_weight_fairness.sh
sudo ./tests/test_hierarchy.sh
sudo ./tests/test_isolation.sh
```

## Tests

### 1. Weight Fairness Test (`test_weight_fairness.sh`)

Validates that CPU allocation matches cgroup weights.

**Setup:**
- 3 cgroups with weights 100:200:300
- Each runs a CPU-intensive workload

**Expected Result:**
- CPU usage ratio should be approximately 1:2:3
- Deviation should be within 10%

### 2. Hierarchy Test (`test_hierarchy.sh`)

Validates hierarchical weight flattening.

**Setup:**
```
root
├── A (weight=100)
│   ├── B (weight=100)
│   └── C (weight=100)
└── D (weight=200)
```

**Expected Result:**
- B gets 1/6 of CPU (16.67%)
- C gets 1/6 of CPU (16.67%)
- D gets 2/3 of CPU (66.67%)

This tests the core flatcg algorithm: compound weights are calculated as:
- B: (100/200) × (100/300) = 1/6
- C: (100/200) × (100/300) = 1/6
- D: 200/300 = 2/3

### 3. Isolation Test (`test_isolation.sh`)

Validates protection from noisy neighbors.

**Setup:**
- victim cgroup: latency-sensitive workload
- noisy cgroup: 4 CPU-intensive processes

**Comparison:**
- Runs with CFS scheduler
- Runs with flatcg scheduler
- Compares P99 latency

## Workloads

### cpu_burn
Simple CPU-intensive workload that runs a tight loop.

```bash
./workload/cpu_burn [duration_seconds]
```

### latency_test
Measures scheduling latency by sleeping and measuring wakeup delay.

```bash
./workload/latency_test [iterations] [sleep_microseconds]
```

Outputs:
- Min, Max, Avg latency
- P50, P95, P99 percentiles

## Results

Test results are saved to `results/` directory with timestamps.

## Requirements

- Linux kernel with sched_ext support
- cgroup v2 enabled
- Root privileges
- gcc, make, bc

## Documentation

- [Bug Analysis](docs/bug_analysis.md) - 详细分析了 possible vs online CPU 不匹配导致的权重调度失效问题
- [Evaluation Results](docs/evaluation_results.md) - 完整的评估报告和测试结果

## Directory Structure

```
eval/
├── README.md           # This file
├── setup.sh            # Environment setup
├── docs/
│   ├── bug_analysis.md    # Bug 分析文档
│   └── evaluation_results.md  # 评估结果
├── workload/
│   ├── cpu_burn.c      # CPU-intensive workload
│   ├── latency_test.c  # Latency measurement
│   └── Makefile
├── scripts/
│   ├── cgroup_helper.sh   # Cgroup management functions
│   └── collect_stats.sh   # Statistics collection
├── tests/
│   ├── test_weight_fairness.sh
│   ├── test_hierarchy.sh
│   └── test_isolation.sh
└── results/            # Test output directory
```
