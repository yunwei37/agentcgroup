# Plan: MemcgController 抽象层 — BPF + Cgroup v2 回退

## 完成状态

| 步骤 | 状态 | 说明 |
|------|------|------|
| 创建 `memcg_controller.py` | ✅ 完成 | ABC + BpfMemcgController + CgroupMemcgController + factory |
| 集成到 `agentcgroupd.py` | ✅ 完成 | 替换直接子进程启动，使用 MemcgController 接口 |
| 添加 MemcgController 测试 | ✅ 完成 | 11 个新测试覆盖两个后端 + 自动检测 |

### 已完成的文件变更

| 文件 | 变更 |
|------|------|
| `agentcg/memcg_controller.py` | **新文件** — 308 行，包含 MemcgConfig、MemcgController ABC、BpfMemcgController、CgroupMemcgController、create_memcg_controller() |
| `agentcg/agentcgroupd.py` | **已修改** — import MemcgConfig/create_memcg_controller，Step 3 改用接口，event loop 调用 poll()，shutdown 调用 detach() |
| `agentcg/test_agentcgroupd.py` | **已修改** — 新增 TestCgroupMemcgController (6 tests)、TestBpfMemcgController (3 tests)、TestAutoDetection (2 tests) |

---

## 问题

当前 memcg_priority 依赖定制内核（`memcg_bpf_ops`），在普通内核上无法编译/运行。
需要一个回退方案：用标准 cgroup v2 控制文件实现类似效果，保持统一接口。

## BPF 做了什么（需要模拟的语义）

```
HIGH cgroup page faults 超过 threshold（1秒窗口内）
  → 进入 1 秒"保护窗口"
  → 保护窗口内:
      HIGH: below_low 返回 true → 内核跳过对 HIGH 的内存回收
      LOW:  get_high_delay_ms 返回 N ms → 内核延迟 LOW 的内存分配
  → 保护窗口结束 → 恢复正常
```

## Cgroup v2 回退如何模拟

| BPF 行为 | Cgroup v2 等价物 | 说明 |
|----------|-----------------|------|
| `below_low → true` | 设置 `memory.low = <大值>` | 内核保护 HIGH 不被回收 |
| `get_high_delay_ms → 50` | 降低 LOW 的 `memory.high` | 内核自动节流 LOW 分配 |
| page fault 计数 | **三种信号检测**（见下） | 检测内存压力 |
| 1秒保护窗口 | Python 定时器 + 状态机 | 周期性轮询 + 自动恢复 |

### 压力检测信号（三选一触发保护）

| 信号 | 来源 | 触发条件 | 说明 |
|------|------|----------|------|
| `memory.events` | HIGH cgroup 的 `memory.events` | `high` 计数增长 ≥ threshold | 需要先设置 `memory.high` |
| `memory.pressure` (PSI) | 父 cgroup 的 `memory.pressure` | `total` 微秒数增长 > 0 | **实测最有效**，标准内核均支持 |
| `memory.current` | 父 cgroup 使用率 | `current / max ≥ 85%` | 最简单的阈值检测 |

**实测验证**（300MB 限制，dask 真实 trace）：
- usage(94%) 和 psi(delta=125410us) 均成功触发保护
- HIGH avg 延迟 4.1ms vs LOW avg 7.6ms（1.85x 节流效果）
- HIGH max 延迟 61.5ms vs LOW max 155ms（2.5x 节流效果）

### 具体行为

- **正常状态**: HIGH `memory.low = 0`，LOW `memory.high = max`
- **检测到压力**: HIGH 的 `memory.events` 中 `high` 计数增长超过阈值
- **进入保护**: HIGH `memory.low = <total * 80%>`，LOW `memory.high = <total * 50%>`
- **保护过期（1秒后）**: 恢复到正常状态

## 接口设计

```python
@dataclass
class MemcgConfig:
    high_cgroup: str           # HIGH cgroup 路径
    low_cgroups: list[str]     # LOW cgroup 路径列表
    delay_ms: int = 50         # BPF 用的延迟参数
    threshold: int = 1         # page fault 阈值
    use_below_low: bool = True

class MemcgController(ABC):
    @abstractmethod
    def attach(self, config: MemcgConfig) -> bool: ...

    @abstractmethod
    def detach(self) -> None: ...

    @abstractmethod
    def poll(self) -> None: ...   # 周期性调用，处理监控逻辑

    @abstractmethod
    def get_stats(self) -> dict: ...

    @property
    @abstractmethod
    def backend_name(self) -> str: ...
```

## 两个实现

### BpfMemcgController
- `attach()`: 用 SubprocessManager 启动 `memcg/memcg_priority` 二进制
- `detach()`: 停止子进程
- `poll()`: 检查子进程健康
- `get_stats()`: 返回 `{"backend": "bpf", "running": True/False}`

### CgroupMemcgController
- `attach()`: 写 `memory.low`/`memory.high` 初始值
- `detach()`: 恢复 `memory.low = 0`, `memory.high = max`
- `poll()`: 读 `memory.events`，检测压力，切换保护状态
- `get_stats()`: 返回 `{"backend": "cgroup", "protection_active": bool, "activations": int}`

### 自动选择
```python
def create_memcg_controller(script_dir: str) -> MemcgController:
    bpf_bin = os.path.join(script_dir, "memcg", "memcg_priority")
    if os.path.isfile(bpf_bin) and os.access(bpf_bin, os.X_OK):
        return BpfMemcgController(bpf_bin)
    return CgroupMemcgController()
```

## 集成到 AgentCGroupDaemon

```python
# 在 daemon.start() 中:
self.memcg = create_memcg_controller(self.script_dir)
self.memcg.attach(MemcgConfig(
    high_cgroup=os.path.join(self.cgroup_root, "session_high"),
    low_cgroups=[os.path.join(self.cgroup_root, "session_low")],
))

# 在 event loop 中:
self.memcg.poll()  # 每次循环调用

# 在 shutdown 中:
self.memcg.detach()
```

## 文件变更

| 文件 | 变更 |
|------|------|
| `agentcg/memcg_controller.py` | **新文件** — MemcgController ABC + 两个实现 |
| `agentcg/agentcgroupd.py` | 改用 MemcgController 接口替代直接启动子进程 |
| `agentcg/test_agentcgroupd.py` | 新增 MemcgController 测试 |

## 测试策略

全部用 tmpdir 模拟 cgroup 文件系统，不需要 root：

1. **CgroupMemcgController**:
   - `test_attach_writes_initial_values` — 验证 memory.low/high 初始写入
   - `test_poll_no_pressure` — memory.events 无变化 → 保持正常状态
   - `test_poll_detects_pressure` — memory.events.high 增长 → 进入保护
   - `test_protection_expires` — 保护窗口过期后恢复
   - `test_detach_restores_defaults` — detach 恢复默认值
   - `test_get_stats` — 返回正确统计

2. **BpfMemcgController**:
   - `test_attach_starts_process` — 验证子进程启动参数正确
   - `test_attach_binary_missing` — 二进制不存在时返回 False
   - `test_detach_stops_process` — 停止子进程

3. **Auto-detection**:
   - `test_selects_bpf_when_available` — 二进制存在 → BPF
   - `test_falls_back_to_cgroup` — 二进制不存在 → Cgroup
