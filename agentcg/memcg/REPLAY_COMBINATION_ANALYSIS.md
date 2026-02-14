# Replay 组合分析报告

## 1. Trace 特征汇总

| Trace | 时长 | 内存均值 | 内存峰值 | 波动比 | 特点 |
|-------|------|---------|---------|--------|------|
| pre-commit | 327s | 306MB | 1907MB | 6.23x | 极高波动，多次突发 |
| dask | 98s | 198MB | 321MB | 1.62x | 中等波动，单次突发 |
| prefab-cloud | 87s | 191MB | 279MB | 1.47x | 低波动，多峰值 |
| sigmavirus24 | 103s | 211MB | 306MB | 1.45x | 低波动，阶梯增长 |
| joke2k | 123s | 190MB | 273MB | 1.44x | 低波动，周期性 |

## 2. 组合分析结果

### Combination 1: High-volatility HIGH

**配置**: HIGH=pre-commit, LOW=dask, joke2k, 内存限制=2048MB

| 策略 | HIGH 时间 | LOW 平均 | 优先级比 | 峰值内存 | OOM风险 |
|------|----------|----------|---------|---------|---------|
| NoIsolation | 367s | 124s | 0.34x | 1236MB | 0% |
| StaticLimit | 642s | 111s | 0.17x | 2048MB | 19% |
| BPF-Priority | 334s | 182s | 0.54x | 2294MB | 0% |

**BPF 效果**: 优先级隔离改善 +61%, OOM 风险从 19% 降至 0%

### Combination 2: Medium HIGH + High-volatility LOW

**配置**: HIGH=dask, LOW=pre-commit, joke2k, 内存限制=2048MB

| 策略 | HIGH 时间 | LOW 平均 | 优先级比 | 峰值内存 | OOM风险 |
|------|----------|----------|---------|---------|---------|
| NoIsolation | 110s | 252s | 2.29x | 1236MB | 0% |
| StaticLimit | 98s | 383s | 3.89x | 2048MB | 19% |
| BPF-Priority | 100s | 272s | 2.71x | 817MB | 0% |

**BPF 效果**: 优先级隔离改善 +18%, OOM 风险从 19% 降至 0%

### Combination 3: All medium volatility

**配置**: HIGH=dask, LOW=joke2k, sigmavirus24, 内存限制=1024MB

| 策略 | HIGH 时间 | LOW 平均 | 优先级比 | 峰值内存 | OOM风险 |
|------|----------|----------|---------|---------|---------|
| NoIsolation | 112s | 128s | 1.15x | 689MB | 0% |
| StaticLimit | 98s | 113s | 1.15x | 1024MB | 0% |
| BPF-Priority | 100s | 132s | 1.32x | 722MB | 0% |

**BPF 效果**: 优先级隔离改善 +15%, OOM 风险从 0% 降至 0%

### Combination 4: Tight memory limit

**配置**: HIGH=pre-commit, LOW=dask, joke2k, 内存限制=1024MB

| 策略 | HIGH 时间 | LOW 平均 | 优先级比 | 峰值内存 | OOM风险 |
|------|----------|----------|---------|---------|---------|
| NoIsolation | 463s | 157s | 0.34x | 1236MB | 10% |
| StaticLimit | 730s | 111s | 0.15x | 1024MB | 25% |
| BPF-Priority | 334s | 182s | 0.54x | 2294MB | 5% |

**BPF 效果**: 优先级隔离改善 +61%, OOM 风险从 25% 降至 5%

### Combination 5: Very tight limit

**配置**: HIGH=pre-commit, LOW=dask, 内存限制=512MB

| 策略 | HIGH 时间 | LOW 平均 | 优先级比 | 峰值内存 | OOM风险 |
|------|----------|----------|---------|---------|---------|
| NoIsolation | 978s | 294s | 0.30x | 1021MB | 50% |
| StaticLimit | 783s | 128s | 0.16x | 512MB | 16% |
| BPF-Priority | 334s | 169s | 0.51x | 2105MB | 5% |

**BPF 效果**: 优先级隔离改善 +68%, OOM 风险从 16% 降至 5%

## 3. 关键发现

| 发现 | 说明 |
|------|------|
| HIGH 波动性影响大 | HIGH 波动越大，BPF 保护效果越明显 |
| 内存限制越紧效果越好 | 竞争激烈时 BPF 优势更突出 |
| 静态限制 OOM 风险高 | 峰值超过配额时容易 OOM |
| BPF 允许 HIGH 突发 | LOW 被限流，HIGH 可以使用更多内存 |

## 4. 推荐实验配置

### 4.1 最佳效果展示配置

```bash
# HIGH: pre-commit (6.23x 波动)
# LOW: dask + faker
# 内存限制: 1GB (紧张)
# 预期优先级改善: 60%+
```

### 4.2 真实场景模拟配置

```bash
# HIGH: dask (1.62x 波动)
# LOW: faker + sigmavirus24
# 内存限制: 1GB
# 预期优先级改善: 15-20%
```

## 5. 与实际实验对比

| 指标 | 模型预测 | 实际实验 (合成负载) | 差异分析 |
|------|---------|-------------------|---------|
| 优先级比改善 | 15-60% | 28% | 合成负载处于中间水平 |
| BPF 触发次数 | ~270 | 272 | 非常接近 |
| OOM 事件 | 0-5% | 0% | 符合预期 |
