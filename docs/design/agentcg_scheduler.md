# AgentCgroup Scheduler 设计文档

## 1. 概述

基于 `scx_flatcg` 改造的 agent-aware CPU 调度器，核心特性：

1. **mem_penalty**: 内存压力 → CPU 降权联动
2. **auto_boost**: 自动识别新进程/step，给予启动优先
3. **wakeup_boost**: I/O 唤醒优先
4. **fork_aware**: Fork 感知，防止 fork bomb
5. **min_slice**: 最小运行时间保护

**设计原则**：零侵入 —— 不需要修改 agent runtime，自动识别和优化。

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         User Space                               │
├─────────────────────────────────────────────────────────────────┤
│  agentcgroupd (daemon)                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │ memory      │  │ cgroup      │  │ metrics                 │  │
│  │ monitor     │  │ watcher     │  │ exporter                │  │
│  │             │  │             │  │                         │  │
│  │ 监控        │  │ 监控 cgroup │  │ Prometheus              │  │
│  │ memory.events│ │ 创建/销毁   │  │ /metrics                │  │
│  └──────┬──────┘  └──────┬──────┘  └─────────────────────────┘  │
│         │                │                                       │
│         │ update         │ update                                │
│         ▼                ▼                                       │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                    BPF Maps                                  ││
│  │  mem_penalty_map    cgroup_state_map    stats_map           ││
│  └─────────────────────────────────────────────────────────────┘│
├─────────────────────────────────────────────────────────────────┤
│                         Kernel Space                             │
├─────────────────────────────────────────────────────────────────┤
│  agentcg_scx.bpf.c (sched_ext scheduler)                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │ enqueue     │  │ dispatch    │  │ running/stopping        │  │
│  │             │  │             │  │                         │  │
│  │ - wakeup    │  │ - pick cgrp │  │ - 计量运行时间          │  │
│  │   boost     │  │ - pick task │  │ - 更新 vtime            │  │
│  │ - fork      │  │ - apply     │  │ - 扣减 bandwidth        │  │
│  │   detect    │  │   boost     │  │                         │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. 数据结构

### 3.1 BPF Maps

```c
// bpf/agentcg_maps.h

/* Cgroup 运行时状态 */
struct cgrp_rt_state {
    /* 调度相关 */
    __u64 cvtime;              // cgroup 虚拟时间 (继承 flatcg)
    __u64 last_run_at;         // 上次运行时间戳

    /* 内存联动 */
    __u64 mem_penalty;         // 内存压力惩罚 (0-4)
    __u64 mem_high_events;     // memory.events.high 计数

    /* 启动 boost */
    __u64 first_seen_at;       // 首次看到该 cgroup 的时间
    __u64 last_fork_at;        // 最近一次 fork 时间
    __u32 nr_forks;            // 时间窗口内 fork 次数
    __u32 boost_remaining_ns;  // 剩余 boost 时间

    /* 唤醒 boost */
    __u64 last_wakeup_at;      // 最近唤醒时间
    __u32 wakeup_count;        // 时间窗口内唤醒次数
    __u8  is_io_bound;         // 判定为 I/O 密集型

    /* 统计 */
    __u64 total_runtime_ns;    // 总运行时间
    __u64 total_wait_ns;       // 总等待时间
};

/* 任务级状态 (task local storage) */
struct task_state {
    __u64 started_at;          // 本次运行开始时间
    __u64 enqueued_at;         // 入队时间
    __u8  is_new;              // 是否是新创建的任务
    __u8  from_fork;           // 是否来自 fork
};

/* BPF Maps 定义 */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 16384);
    __type(key, __u64);                    // cgroup id
    __type(value, struct cgrp_rt_state);
} cgrp_state_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_TASK_STORAGE);
    __uint(map_flags, BPF_F_NO_PREALLOC);
    __type(key, int);
    __type(value, struct task_state);
} task_state_map SEC(".maps");

/* 用户态写入的配置 */
struct cgrp_config {
    __u32 weight;              // cpu.weight (1-10000)
    __u32 flags;               // AGENTCG_FLAG_*
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 16384);
    __type(key, __u64);                    // cgroup id
    __type(value, struct cgrp_config);
} cgrp_config_map SEC(".maps");
```

### 3.2 常量和配置

```c
// bpf/agentcg_const.h

/* 时间常量 (纳秒) */
#define NSEC_PER_MSEC           1000000ULL
#define NSEC_PER_SEC            1000000000ULL

/* Boost 参数 */
#define STARTUP_BOOST_WINDOW_NS (100 * NSEC_PER_MSEC)  // 新进程 100ms boost 窗口
#define STARTUP_BOOST_VTIME_NS  (20 * NSEC_PER_MSEC)   // vtime 减少 20ms
#define WAKEUP_BOOST_VTIME_NS   (5 * NSEC_PER_MSEC)    // 唤醒 boost 5ms

/* Fork 检测 */
#define FORK_WINDOW_NS          (1 * NSEC_PER_SEC)     // 1秒窗口
#define FORK_STORM_THRESHOLD    32                      // 超过视为 fork storm

/* I/O 判定 */
#define IO_BOUND_WAKEUP_RATE    10                      // 每秒 >10 次唤醒
#define IO_BOUND_WINDOW_NS      (1 * NSEC_PER_SEC)

/* 最小 slice */
#define MIN_SLICE_NS            (500 * 1000)            // 500us 最小运行

/* 内存惩罚 */
#define MEM_PENALTY_MAX         4
#define MEM_PENALTY_WEIGHT_DIV  2                       // penalty=1 时权重/2

/* Slice 基准 */
#define BASE_SLICE_NS           (4 * NSEC_PER_MSEC)     // 4ms 默认
#define BOOSTED_SLICE_NS        (2 * NSEC_PER_MSEC)     // 2ms boosted
```

---

## 4. 核心算法

### 4.1 自动识别新进程/Step

```c
/*
 * 自动识别策略：
 * 1. 新 cgroup 首次有任务 enqueue -> 新 step
 * 2. 已有 cgroup 长时间空闲后重新活跃 -> 新 step
 * 3. fork 创建的新进程 -> 继承 boost 但不叠加
 */

static __always_inline bool is_new_step(struct cgrp_rt_state *state, __u64 now)
{
    /* 情况1: 首次见到这个 cgroup */
    if (state->first_seen_at == 0) {
        state->first_seen_at = now;
        return true;
    }

    /* 情况2: 空闲超过阈值后重新活跃 */
    __u64 idle_duration = now - state->last_run_at;
    if (idle_duration > STARTUP_BOOST_WINDOW_NS * 2) {
        return true;
    }

    return false;
}

static __always_inline void apply_startup_boost(struct cgrp_rt_state *state, __u64 now)
{
    state->boost_remaining_ns = STARTUP_BOOST_WINDOW_NS;
    /* 不直接修改 cvtime，而是在 effective_vtime 计算时应用 */
}
```

### 4.2 Effective Vtime 计算（核心）

```c
/*
 * 有效虚拟时间 = 基础 cvtime - boost 偏移 + penalty 偏移
 *
 * 越小的 effective_vtime = 越优先调度
 */
static __always_inline __u64 effective_vtime(struct cgrp_rt_state *state,
                                              struct fcg_cgrp_ctx *cgc,
                                              __u64 now)
{
    __u64 vtime = state->cvtime;

    /* 1. Startup boost: 降低 vtime */
    if (state->boost_remaining_ns > 0) {
        __u64 boost = min(state->boost_remaining_ns, STARTUP_BOOST_VTIME_NS);
        if (vtime > boost)
            vtime -= boost;
        else
            vtime = 0;
    }

    /* 2. Wakeup boost: I/O 密集型额外降低 */
    if (state->is_io_bound) {
        __u64 since_wakeup = now - state->last_wakeup_at;
        if (since_wakeup < WAKEUP_BOOST_VTIME_NS * 2) {
            if (vtime > WAKEUP_BOOST_VTIME_NS)
                vtime -= WAKEUP_BOOST_VTIME_NS;
        }
    }

    /* 3. Memory penalty: 增加 vtime (降低优先级) */
    if (state->mem_penalty > 0) {
        /* penalty=1: +25%, penalty=2: +50%, ... */
        vtime += (vtime * state->mem_penalty) / 4;
    }

    return vtime;
}
```

### 4.3 Effective Weight 计算

```c
/*
 * 有效权重用于 vtime 增长计算
 * vtime_delta = runtime / effective_weight
 */
static __always_inline __u64 effective_weight(struct cgrp_rt_state *state,
                                               __u64 base_weight)
{
    __u64 weight = base_weight ?: 100;

    /* Memory penalty 降权 */
    if (state->mem_penalty > 0) {
        /* penalty=1: /2, penalty=2: /3, ... */
        weight = weight / (1 + state->mem_penalty);
        if (weight == 0) weight = 1;
    }

    /* Fork storm 检测: 严重降权 */
    if (state->nr_forks > FORK_STORM_THRESHOLD) {
        weight = weight / 4;
        if (weight == 0) weight = 1;
    }

    return weight;
}
```

### 4.4 Enqueue Hook

```c
void BPF_STRUCT_OPS(agentcg_enqueue, struct task_struct *p, u64 enq_flags)
{
    __u64 now = bpf_ktime_get_ns();
    __u64 cgid = get_cgroup_id(p);

    /* 获取或创建 cgroup 状态 */
    struct cgrp_rt_state *state = bpf_map_lookup_elem(&cgrp_state_map, &cgid);
    if (!state) {
        struct cgrp_rt_state new_state = { .first_seen_at = now };
        bpf_map_update_elem(&cgrp_state_map, &cgid, &new_state, BPF_ANY);
        state = bpf_map_lookup_elem(&cgrp_state_map, &cgid);
        if (!state) goto fallback;
    }

    /* 获取或创建任务状态 */
    struct task_state *tstate = bpf_task_storage_get(&task_state_map, p, 0,
                                                      BPF_LOCAL_STORAGE_GET_F_CREATE);
    if (!tstate) goto fallback;

    /* 检测唤醒 */
    if (enq_flags & SCX_ENQ_WAKEUP) {
        state->last_wakeup_at = now;
        state->wakeup_count++;

        /* 更新 I/O 密集型判定 */
        __u64 window = now - (state->last_wakeup_at - IO_BOUND_WINDOW_NS);
        if (window > 0 && state->wakeup_count > IO_BOUND_WAKEUP_RATE) {
            state->is_io_bound = 1;
        }
    }

    /* 检测新进程/step */
    if (is_new_step(state, now)) {
        apply_startup_boost(state, now);
        stat_inc(STAT_NEW_STEP);
    }

    /* 检测 fork */
    if (tstate->from_fork) {
        state->last_fork_at = now;
        state->nr_forks++;
        tstate->from_fork = 0;

        /* Fork storm 检测 */
        if (state->nr_forks > FORK_STORM_THRESHOLD) {
            stat_inc(STAT_FORK_STORM);
        }
    }

    tstate->enqueued_at = now;

    /* 继续原有 flatcg enqueue 逻辑 */
    // ... dispatch to cgroup DSQ ...
    return;

fallback:
    scx_bpf_dsq_insert(p, FALLBACK_DSQ, SCX_SLICE_DFL, enq_flags);
}
```

### 4.5 Dispatch Hook

```c
void BPF_STRUCT_OPS(agentcg_dispatch, s32 cpu, struct task_struct *prev)
{
    __u64 now = bpf_ktime_get_ns();

    /*
     * 两级调度：
     * 1. 选择 effective_vtime 最小的 cgroup
     * 2. 从该 cgroup 的 DSQ 取任务
     */

    /* 遍历 runnable cgroups, 选最小 effective_vtime */
    struct cgrp_rt_state *best_state = NULL;
    __u64 best_cgid = 0;
    __u64 best_vtime = ~0ULL;

    /*
     * 实际实现用 rbtree 或其他数据结构
     * 这里简化为 hash 遍历 (性能较差，仅示意)
     */
    // ... pick best cgroup ...

    if (best_state) {
        __u64 dsq = dsq_for_cgrp(best_cgid);

        /* 计算 slice */
        __u64 slice = BASE_SLICE_NS;
        if (best_state->boost_remaining_ns > 0) {
            slice = BOOSTED_SLICE_NS;  // boosted 任务用更短 slice
        }

        /* 消费 boost */
        if (best_state->boost_remaining_ns > slice) {
            best_state->boost_remaining_ns -= slice;
        } else {
            best_state->boost_remaining_ns = 0;
        }

        scx_bpf_consume(dsq);
    }

    /* Fallback */
    scx_bpf_consume(FALLBACK_DSQ);
}
```

### 4.6 Running/Stopping Hooks

```c
void BPF_STRUCT_OPS(agentcg_running, struct task_struct *p)
{
    __u64 now = bpf_ktime_get_ns();

    struct task_state *tstate = bpf_task_storage_get(&task_state_map, p, 0, 0);
    if (tstate) {
        tstate->started_at = now;
    }
}

void BPF_STRUCT_OPS(agentcg_stopping, struct task_struct *p, bool runnable)
{
    __u64 now = bpf_ktime_get_ns();
    __u64 cgid = get_cgroup_id(p);

    struct task_state *tstate = bpf_task_storage_get(&task_state_map, p, 0, 0);
    struct cgrp_rt_state *state = bpf_map_lookup_elem(&cgrp_state_map, &cgid);

    if (!tstate || !state) return;

    /* 计算实际运行时间 */
    __u64 runtime = now - tstate->started_at;

    /* 更新统计 */
    state->total_runtime_ns += runtime;
    state->last_run_at = now;

    /* 更新 cvtime: vtime += runtime / effective_weight */
    struct cgrp_config *cfg = bpf_map_lookup_elem(&cgrp_config_map, &cgid);
    __u64 weight = effective_weight(state, cfg ? cfg->weight : 100);
    state->cvtime += runtime / weight;

    /* 重置 fork 计数器 (每秒窗口) */
    if (now - state->last_fork_at > FORK_WINDOW_NS) {
        state->nr_forks = 0;
    }

    /* 重置唤醒计数器 */
    if (now - (state->last_wakeup_at - IO_BOUND_WINDOW_NS) > IO_BOUND_WINDOW_NS) {
        state->wakeup_count = 0;
        state->is_io_bound = 0;
    }
}
```

### 4.7 Fork Hook

```c
void BPF_STRUCT_OPS(agentcg_fork, struct task_struct *p)
{
    /* 标记为来自 fork 的新进程 */
    struct task_state *tstate = bpf_task_storage_get(&task_state_map, p, 0,
                                                      BPF_LOCAL_STORAGE_GET_F_CREATE);
    if (tstate) {
        tstate->is_new = 1;
        tstate->from_fork = 1;
    }
}
```

---

## 5. 用户态 Daemon

### 5.1 Memory Monitor

```c
// daemon/mem_monitor.c

/*
 * 监控 memory.events，更新 mem_penalty
 *
 * 策略：
 * - memory.events.high 增加 -> penalty++
 * - 一段时间无增加 -> penalty--
 */

struct mem_monitor {
    int map_fd;                    // BPF map fd
    char *cgroup_path;
    __u64 last_high_events;
    time_t last_update;
};

void update_mem_penalty(struct mem_monitor *mon) {
    /* 读取 memory.events */
    char path[PATH_MAX];
    snprintf(path, sizeof(path), "%s/memory.events", mon->cgroup_path);

    __u64 high_events = read_memory_events_high(path);
    __u64 cgid = get_cgroup_id_from_path(mon->cgroup_path);

    struct cgrp_rt_state state;
    if (bpf_map_lookup_elem(mon->map_fd, &cgid, &state) < 0)
        return;

    time_t now = time(NULL);

    if (high_events > mon->last_high_events) {
        /* 内存压力上升 */
        if (state.mem_penalty < MEM_PENALTY_MAX) {
            state.mem_penalty++;
        }
        mon->last_update = now;
    } else if (now - mon->last_update > PENALTY_DECAY_SECONDS) {
        /* 压力消退 */
        if (state.mem_penalty > 0) {
            state.mem_penalty--;
        }
        mon->last_update = now;
    }

    mon->last_high_events = high_events;
    bpf_map_update_elem(mon->map_fd, &cgid, &state, BPF_EXIST);
}
```

### 5.2 Cgroup Watcher

```c
// daemon/cgroup_watcher.c

/*
 * 监控 cgroup 创建/销毁
 * 使用 inotify 或 fanotify
 */

void on_cgroup_created(const char *path) {
    __u64 cgid = get_cgroup_id_from_path(path);

    /* 初始化状态 */
    struct cgrp_rt_state state = { 0 };
    bpf_map_update_elem(state_map_fd, &cgid, &state, BPF_ANY);

    /* 读取 cpu.weight 并设置配置 */
    __u32 weight = read_cpu_weight(path);
    struct cgrp_config cfg = { .weight = weight };
    bpf_map_update_elem(config_map_fd, &cgid, &cfg, BPF_ANY);

    log_info("New cgroup: %s (id=%llu, weight=%u)", path, cgid, weight);
}

void on_cgroup_destroyed(const char *path) {
    __u64 cgid = get_cgroup_id_from_path(path);

    /* 清理状态 */
    bpf_map_delete_elem(state_map_fd, &cgid);
    bpf_map_delete_elem(config_map_fd, &cgid);

    log_info("Cgroup removed: %s", path);
}
```

### 5.3 Metrics Exporter

```c
// daemon/metrics.c

/*
 * 暴露 Prometheus metrics
 */

void export_metrics(int stats_map_fd) {
    printf("# HELP agentcg_new_steps_total New step detections\n");
    printf("# TYPE agentcg_new_steps_total counter\n");
    printf("agentcg_new_steps_total %llu\n", read_stat(STAT_NEW_STEP));

    printf("# HELP agentcg_fork_storms_total Fork storm detections\n");
    printf("# TYPE agentcg_fork_storms_total counter\n");
    printf("agentcg_fork_storms_total %llu\n", read_stat(STAT_FORK_STORM));

    printf("# HELP agentcg_mem_penalty Current memory penalty by cgroup\n");
    printf("# TYPE agentcg_mem_penalty gauge\n");
    /* 遍历 cgroup 输出 penalty */
}
```

---

## 6. 配置文件

```yaml
# /etc/agentcg/config.yaml

daemon:
  log_level: info
  metrics_port: 9090

scheduler:
  # Boost 参数
  startup_boost_window_ms: 100
  startup_boost_vtime_ms: 20
  wakeup_boost_vtime_ms: 5

  # Slice 参数
  base_slice_ms: 4
  boosted_slice_ms: 2
  min_slice_us: 500

  # Fork storm
  fork_window_sec: 1
  fork_storm_threshold: 32

  # I/O 判定
  io_bound_wakeup_rate: 10

  # 内存联动
  mem_penalty_max: 4
  mem_penalty_decay_sec: 5

monitor:
  # 监控的 cgroup 根目录
  cgroup_root: /sys/fs/cgroup
  # 监控间隔
  poll_interval_ms: 10
```

---

## 7. 部署方式

### 7.1 安装

```bash
# 编译
cd agentcgroup
make

# 安装 BPF 程序和 daemon
sudo make install

# 启动 daemon
sudo systemctl start agentcgroupd
```

### 7.2 与 Docker/K8s 集成

**无需任何配置**，daemon 自动：
1. 监控 `/sys/fs/cgroup` 下的容器 cgroup
2. 读取 `cpu.weight` 配置
3. 监控 `memory.events`
4. BPF 自动识别新进程和优化调度

### 7.3 验证

```bash
# 查看 sched_ext 状态
cat /sys/kernel/sched_ext/state

# 查看 metrics
curl http://localhost:9090/metrics

# 查看日志
journalctl -u agentcgroupd -f
```

---

## 8. 性能影响分析

| 操作 | 开销 | 说明 |
|------|------|------|
| enqueue hook | ~100ns | BPF map lookup + 简单计算 |
| dispatch hook | ~200ns | cgroup 选择 + DSQ consume |
| stopping hook | ~150ns | vtime 更新 + 统计 |
| mem monitor (userspace) | ~1ms/10ms周期 | 文件读取 + map 更新 |

**总体开销**：< 1% CPU，对 agent 场景可忽略。

---

## 9. 与 flatcg 的差异总结

| 功能 | flatcg | agentcg |
|------|--------|---------|
| 层级权重 | ✅ | ✅ 继承 |
| cgroup vtime | ✅ | ✅ 继承 |
| 内存联动 | ❌ | ✅ mem_penalty |
| 自动识别新进程 | ❌ | ✅ first_seen + idle 检测 |
| Wakeup 优先 | ❌ | ✅ is_io_bound |
| Fork 感知 | ❌ | ✅ fork storm 检测 |
| 最小 slice | ❌ | ✅ MIN_SLICE_NS |

---

## 10. 实现路线图

```
Phase 1 (MVP):
  ✓ mem_penalty 联动
  ✓ 基础统计

Phase 2:
  □ 自动识别新进程 boost
  □ cgroup watcher

Phase 3:
  □ Wakeup boost
  □ Fork storm 检测
  □ Metrics exporter

Phase 4:
  □ 最小 slice 保护
  □ 配置热更新
  □ NRI 集成
```

---

## 11. 测试计划

### 11.1 单元测试

```bash
# 验证 boost 机制
./tests/test_startup_boost.sh

# 验证 mem_penalty
./tests/test_mem_penalty.sh

# 验证 fork storm 检测
./tests/test_fork_storm.sh
```

### 11.2 集成测试

```bash
# 多租户隔离测试
./eval/tests/test_isolation.sh

# 与 CFS 对比
./eval/tests/compare_cfs.sh
```

### 11.3 压力测试

```bash
# 高并发 agent 模拟
./eval/stress/multi_agent.sh --agents 100 --duration 300
```
