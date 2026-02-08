# AgentCgroup SWE-Bench Experiment Analysis Report (haiku)

Generated: 2026-02-07 18:08:19

Data source: `/home/yunwei37/workspace/agentcgroup/experiments/all_images_haiku`

Total tasks analyzed: 33


## Dataset Overview

| Metric | Value |
|--------|-------|
| Total tasks | 33 |
| Successful | 9 (27.3%) |
| Total execution time | 12322.0s (205.4 min) |

## RQ1: Resource Usage Dynamics (Time-scale Mismatch)

**Research Question**: How dynamic are resource changes during AI agent execution?

**Paper Claim**: User-space controllers react in 10-100ms, but resource changes happen at millisecond scale.


### Findings

- **Total burst events detected**: 231
- **Tasks with bursts**: 33 / 33

**CPU Change Rate Statistics (%/sec)**:
- Mean: 0.69
- Max: 143.88
- 95th percentile: 1.71

![Resource Time Series](rq1_resource_timeseries.png)

![Change Rate Distribution](rq1_change_rate_distribution.png)

## RQ2: Resource Usage by Category (Domain Mismatch)

**Research Question**: Do different task categories have significantly different resource needs?

**Paper Claim**: Static resource limits cannot adapt to different workloads.


### Memory Usage by Category

| Category | N | Avg Memory (MB) | Peak Memory (MB) |
|----------|---|-----------------|------------------|
| swebench | 33 | 206.0 | 2076.0 |

![Category Box Plots](rq2_category_boxplots.png)

## RQ3: Tool Call Patterns

**Research Question**: What is the relationship between tool calls and resource consumption?


### Top Tools by Execution Time

| Tool | Call Count | Total Time (s) | Avg Time (s) |
|------|------------|----------------|--------------|
| Task | 17 | 1707.99 | 100.47 |
| Bash | 410 | 1543.21 | 3.76 |
| WebSearch | 9 | 108.57 | 12.06 |
| Read | 222 | 75.19 | 0.34 |
| WebFetch | 9 | 67.75 | 7.53 |
| Grep | 159 | 31.98 | 0.20 |
| Glob | 45 | 26.29 | 0.58 |
| Edit | 109 | 5.59 | 0.05 |
| TodoWrite | 94 | 3.46 | 0.04 |
| Write | 26 | 1.27 | 0.05 |

**Tool Time Ratio**: Mean 25.9%, Median 22.1%

![Tool Analysis](rq3_tool_analysis.png)

## RQ4: Over-provisioning Analysis

**Research Question**: How much over-provisioning would static limits require?


### Over-provisioning Factors

| Metric | CPU Ratio | Memory Ratio |
|--------|-----------|--------------|
| Mean | 11.11x | 1.66x |
| Median | 10.26x | 1.37x |
| Max | 22.86x | 6.63x |
| 95th Percentile | 19.71x | 3.60x |

![Over-provisioning Analysis](rq4_overprovisioning.png)

## Key Conclusions

1. **Time-scale Mismatch**: Resource usage exhibits significant burstiness that exceeds 
   the reaction time of typical user-space controllers.
2. **Domain Mismatch**: Different task categories show distinct resource profiles, 
   making static limits suboptimal.
3. **Over-provisioning Waste**: Static provisioning at peak levels wastes significant resources,
   as average usage is typically much lower than peak.