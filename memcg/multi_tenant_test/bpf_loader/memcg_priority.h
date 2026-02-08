/* SPDX-License-Identifier: GPL-2.0 */
/*
 * memcg_priority.h - Shared definitions for memcg priority BPF
 */

#ifndef MEMCG_PRIORITY_H
#define MEMCG_PRIORITY_H

/* Configuration structure shared between BPF and userspace */
struct memcg_priority_config {
	__u64 high_cgroup_id;      /* Cgroup ID of the HIGH priority session */
	__u64 threshold;           /* Page fault threshold to trigger protection */
	__u32 over_high_ms;        /* Delay in ms for LOW priority when over memory.high */
	__u8 use_below_low;        /* Whether to use below_low callback */
	__u8 use_below_min;        /* Whether to use below_min callback */
};

#endif /* MEMCG_PRIORITY_H */
