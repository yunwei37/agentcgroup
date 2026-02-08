// SPDX-License-Identifier: GPL-2.0
/*
 * memcg_priority.bpf.c - BPF program for memory cgroup priority management
 *
 * This program implements two struct_ops:
 * - high_mcg_ops: Attached to HIGH priority cgroup, uses below_low to protect
 * - low_mcg_ops: Attached to LOW priority cgroups, uses get_high_delay_ms to throttle
 */

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#define ONE_SECOND_NS	1000000000ULL

/* Configuration - set by userspace before attaching */
struct memcg_priority_config {
	__u64 high_cgroup_id;
	__u64 threshold;
	__u32 over_high_ms;
	__u8 use_below_low;
	__u8 use_below_min;
} local_config;

/* Aggregation data for page fault counting */
struct AggregationData {
	__u64 sum;
	__u64 window_start_ts;
};

/* Map to track page faults in the HIGH cgroup */
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct AggregationData);
} aggregation_map SEC(".maps");

/* Map to track when threshold was triggered */
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} trigger_ts_map SEC(".maps");

/* Stats for monitoring */
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 4);
	__type(key, __u32);
	__type(value, __u64);
} stats SEC(".maps");

enum {
	STAT_HIGH_DELAY_CALLS = 0,
	STAT_HIGH_DELAY_ACTIVE = 1,
	STAT_BELOW_LOW_CALLS = 2,
	STAT_BELOW_LOW_ACTIVE = 3,
};

/*
 * Tracepoint handler: count page faults for HIGH cgroup
 * When page faults exceed threshold, trigger protection mode
 */
SEC("tp/memcg/count_memcg_events")
int handle_count_memcg_events(struct trace_event_raw_memcg_rstat_events *ctx)
{
	__u32 key = 0;
	struct AggregationData *data;
	__u64 current_ts;

	/* Only count page faults from HIGH priority cgroup */
	/* PGFAULT = 23 in the kernel enum */
	if (ctx->id != local_config.high_cgroup_id || ctx->item != 23)
		return 0;

	data = bpf_map_lookup_elem(&aggregation_map, &key);
	if (!data)
		return 0;

	current_ts = bpf_ktime_get_ns();

	/* Reset window if more than 1 second has passed */
	if (current_ts - data->window_start_ts < ONE_SECOND_NS) {
		data->sum += ctx->val;
	} else {
		data->window_start_ts = current_ts;
		data->sum = ctx->val;
	}

	/* If threshold exceeded, record trigger timestamp */
	if (data->sum > local_config.threshold) {
		bpf_map_update_elem(&trigger_ts_map, &key, &current_ts, BPF_ANY);
		data->sum = 0;
		data->window_start_ts = current_ts;
	}

	return 0;
}

/*
 * Check if we're within the protection window
 * Returns true if HIGH cgroup is active and needs protection
 */
static __always_inline bool need_protection(void)
{
	__u32 key = 0;
	__u64 *trigger_ts;
	__u64 current_ts;

	trigger_ts = bpf_map_lookup_elem(&trigger_ts_map, &key);
	if (!trigger_ts || *trigger_ts == 0)
		return false;

	current_ts = bpf_ktime_get_ns();

	/* Protection window is 1 second */
	return (current_ts - *trigger_ts < ONE_SECOND_NS);
}

/*
 * below_low callback for HIGH priority cgroup
 * Returns true to indicate the cgroup should be treated as below memory.low
 * This protects it from reclaim pressure
 */
SEC("struct_ops/below_low")
bool below_low_impl(struct mem_cgroup *memcg)
{
	__u32 key = STAT_BELOW_LOW_CALLS;
	__u64 *cnt;

	cnt = bpf_map_lookup_elem(&stats, &key);
	if (cnt)
		__sync_fetch_and_add(cnt, 1);

	if (!local_config.use_below_low)
		return false;

	if (need_protection()) {
		key = STAT_BELOW_LOW_ACTIVE;
		cnt = bpf_map_lookup_elem(&stats, &key);
		if (cnt)
			__sync_fetch_and_add(cnt, 1);
		return true;
	}

	return false;
}

/*
 * below_min callback for HIGH priority cgroup
 * Returns true to indicate the cgroup should be treated as below memory.min
 */
SEC("struct_ops/below_min")
bool below_min_impl(struct mem_cgroup *memcg)
{
	if (!local_config.use_below_min)
		return false;

	return need_protection();
}

/*
 * get_high_delay_ms callback for LOW priority cgroups
 * Returns delay in milliseconds when cgroup is over memory.high
 * This throttles LOW priority processes when HIGH needs resources
 */
SEC("struct_ops/get_high_delay_ms")
unsigned int get_high_delay_ms_impl(struct mem_cgroup *memcg)
{
	__u32 key = STAT_HIGH_DELAY_CALLS;
	__u64 *cnt;

	cnt = bpf_map_lookup_elem(&stats, &key);
	if (cnt)
		__sync_fetch_and_add(cnt, 1);

	if (local_config.over_high_ms && need_protection()) {
		key = STAT_HIGH_DELAY_ACTIVE;
		cnt = bpf_map_lookup_elem(&stats, &key);
		if (cnt)
			__sync_fetch_and_add(cnt, 1);
		return local_config.over_high_ms;
	}

	return 0;
}

/*
 * HIGH priority struct_ops - attached to the protected cgroup
 * Uses below_low/below_min to avoid reclaim pressure
 */
SEC(".struct_ops.link")
struct memcg_bpf_ops high_mcg_ops = {
	.below_low = (void *)below_low_impl,
	.below_min = (void *)below_min_impl,
};

/*
 * LOW priority struct_ops - attached to throttled cgroups
 * Uses get_high_delay_ms to introduce delay when over memory.high
 */
SEC(".struct_ops.link")
struct memcg_bpf_ops low_mcg_ops = {
	.get_high_delay_ms = (void *)get_high_delay_ms_impl,
};

char LICENSE[] SEC("license") = "GPL";
