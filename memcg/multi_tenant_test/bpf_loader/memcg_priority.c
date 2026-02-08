// SPDX-License-Identifier: GPL-2.0
/*
 * memcg_priority.c - Userspace loader for memcg priority BPF
 *
 * This program loads and attaches memcg BPF struct_ops to provide
 * priority-based memory management for multi-tenant workloads.
 *
 * Usage:
 *   memcg_priority --high /path/to/high_cgroup \
 *                  --low /path/to/low_cgroup1 \
 *                  --low /path/to/low_cgroup2 \
 *                  [--delay-ms 2000] \
 *                  [--threshold 1]
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <getopt.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <linux/limits.h>
#include <linux/types.h>
#include <bpf/libbpf.h>
#include <bpf/bpf.h>

/* Configuration structure - must match BPF program */
struct memcg_priority_config {
	__u64 high_cgroup_id;
	__u64 threshold;
	__u32 over_high_ms;
	__u8 use_below_low;
	__u8 use_below_min;
};

#include "memcg_priority.bpf.skel.h"

#define MAX_LOW_CGROUPS 16
#define DEFAULT_DELAY_MS 2000
#define DEFAULT_THRESHOLD 1

static volatile int running = 1;

static void sig_handler(int sig)
{
	running = 0;
}

static int libbpf_print_fn(enum libbpf_print_level level, const char *format, va_list args)
{
	if (level == LIBBPF_DEBUG)
		return 0;
	return vfprintf(stderr, format, args);
}

/*
 * Get cgroup ID from path
 */
static __u64 get_cgroup_id(const char *path)
{
	struct stat st;

	if (stat(path, &st) < 0) {
		fprintf(stderr, "Failed to stat %s: %s\n", path, strerror(errno));
		return 0;
	}

	/* The cgroup ID is the inode number */
	return st.st_ino;
}

/*
 * Open cgroup directory and return file descriptor
 */
static int open_cgroup(const char *path)
{
	int fd;

	fd = open(path, O_RDONLY | O_DIRECTORY);
	if (fd < 0) {
		fprintf(stderr, "Failed to open cgroup %s: %s\n", path, strerror(errno));
		return -1;
	}

	return fd;
}

static void print_usage(const char *prog)
{
	fprintf(stderr, "Usage: %s [OPTIONS]\n", prog);
	fprintf(stderr, "\n");
	fprintf(stderr, "Options:\n");
	fprintf(stderr, "  --high PATH       Path to HIGH priority cgroup (required)\n");
	fprintf(stderr, "  --low PATH        Path to LOW priority cgroup (can specify multiple)\n");
	fprintf(stderr, "  --delay-ms MS     Delay in ms for LOW cgroups (default: %d)\n", DEFAULT_DELAY_MS);
	fprintf(stderr, "  --threshold N     Page fault threshold (default: %d)\n", DEFAULT_THRESHOLD);
	fprintf(stderr, "  --below-low       Use below_low callback for HIGH cgroup\n");
	fprintf(stderr, "  --below-min       Use below_min callback for HIGH cgroup\n");
	fprintf(stderr, "  --verbose         Enable verbose output\n");
	fprintf(stderr, "  --help            Show this help\n");
	fprintf(stderr, "\n");
	fprintf(stderr, "Example:\n");
	fprintf(stderr, "  sudo %s --high /sys/fs/cgroup/memcg_bpf_test/high_session \\\n", prog);
	fprintf(stderr, "                --low /sys/fs/cgroup/memcg_bpf_test/low_session_1 \\\n");
	fprintf(stderr, "                --low /sys/fs/cgroup/memcg_bpf_test/low_session_2 \\\n");
	fprintf(stderr, "                --delay-ms 2000 --below-low\n");
}

int main(int argc, char **argv)
{
	struct memcg_priority *skel = NULL;
	struct bpf_link *high_link = NULL;
	struct bpf_link *low_links[MAX_LOW_CGROUPS] = {};
	struct bpf_link *tp_link = NULL;
	struct bpf_map *map;
	int high_fd = -1;
	int low_fds[MAX_LOW_CGROUPS];
	int low_count = 0;
	const char *high_path = NULL;
	const char *low_paths[MAX_LOW_CGROUPS] = {};
	__u32 delay_ms = DEFAULT_DELAY_MS;
	__u64 threshold = DEFAULT_THRESHOLD;
	int use_below_low = 0;
	int use_below_min = 0;
	int verbose = 0;
	int err = 0;
	int opt;
	int i;

	DECLARE_LIBBPF_OPTS(bpf_struct_ops_opts, opts);

	static struct option long_options[] = {
		{"high", required_argument, 0, 'H'},
		{"low", required_argument, 0, 'L'},
		{"delay-ms", required_argument, 0, 'd'},
		{"threshold", required_argument, 0, 't'},
		{"below-low", no_argument, 0, 'l'},
		{"below-min", no_argument, 0, 'm'},
		{"verbose", no_argument, 0, 'v'},
		{"help", no_argument, 0, 'h'},
		{0, 0, 0, 0}
	};

	for (i = 0; i < MAX_LOW_CGROUPS; i++)
		low_fds[i] = -1;

	while ((opt = getopt_long(argc, argv, "H:L:d:t:lmvh", long_options, NULL)) != -1) {
		switch (opt) {
		case 'H':
			high_path = optarg;
			break;
		case 'L':
			if (low_count >= MAX_LOW_CGROUPS) {
				fprintf(stderr, "Too many LOW cgroups (max %d)\n", MAX_LOW_CGROUPS);
				return 1;
			}
			low_paths[low_count++] = optarg;
			break;
		case 'd':
			delay_ms = atoi(optarg);
			break;
		case 't':
			threshold = strtoull(optarg, NULL, 10);
			break;
		case 'l':
			use_below_low = 1;
			break;
		case 'm':
			use_below_min = 1;
			break;
		case 'v':
			verbose = 1;
			break;
		case 'h':
		default:
			print_usage(argv[0]);
			return opt == 'h' ? 0 : 1;
		}
	}

	if (!high_path) {
		fprintf(stderr, "Error: --high is required\n");
		print_usage(argv[0]);
		return 1;
	}

	if (low_count == 0) {
		fprintf(stderr, "Warning: No LOW cgroups specified\n");
	}

	/* Set up signal handlers */
	signal(SIGINT, sig_handler);
	signal(SIGTERM, sig_handler);

	/* Set up libbpf */
	libbpf_set_print(libbpf_print_fn);

	/* Open cgroups */
	high_fd = open_cgroup(high_path);
	if (high_fd < 0) {
		err = 1;
		goto cleanup;
	}

	for (i = 0; i < low_count; i++) {
		low_fds[i] = open_cgroup(low_paths[i]);
		if (low_fds[i] < 0) {
			err = 1;
			goto cleanup;
		}
	}

	/* Load BPF skeleton */
	skel = memcg_priority__open();
	if (!skel) {
		fprintf(stderr, "Failed to open BPF skeleton\n");
		err = 1;
		goto cleanup;
	}

	/* Configure BPF program */
	skel->bss->local_config.high_cgroup_id = get_cgroup_id(high_path);
	skel->bss->local_config.threshold = threshold;
	skel->bss->local_config.over_high_ms = delay_ms;
	skel->bss->local_config.use_below_low = use_below_low;
	skel->bss->local_config.use_below_min = use_below_min;

	if (skel->bss->local_config.high_cgroup_id == 0) {
		fprintf(stderr, "Failed to get cgroup ID for %s\n", high_path);
		err = 1;
		goto cleanup;
	}

	printf("Configuration:\n");
	printf("  HIGH cgroup: %s (id=%llu)\n", high_path,
	       (unsigned long long)skel->bss->local_config.high_cgroup_id);
	printf("  Threshold: %llu page faults\n",
	       (unsigned long long)skel->bss->local_config.threshold);
	printf("  Delay: %u ms\n", skel->bss->local_config.over_high_ms);
	printf("  Use below_low: %s\n", use_below_low ? "yes" : "no");
	printf("  Use below_min: %s\n", use_below_min ? "yes" : "no");
	printf("  LOW cgroups: %d\n", low_count);
	for (i = 0; i < low_count; i++)
		printf("    - %s\n", low_paths[i]);

	/* Load BPF program */
	err = memcg_priority__load(skel);
	if (err) {
		fprintf(stderr, "Failed to load BPF skeleton: %d\n", err);
		goto cleanup;
	}

	/* Attach tracepoint for page fault counting */
	tp_link = bpf_program__attach(skel->progs.handle_count_memcg_events);
	if (!tp_link) {
		fprintf(stderr, "Failed to attach tracepoint: %s\n", strerror(errno));
		err = 1;
		goto cleanup;
	}
	printf("Attached tracepoint: memcg/count_memcg_events\n");

	/* Attach HIGH cgroup struct_ops */
	map = bpf_object__find_map_by_name(skel->obj, "high_mcg_ops");
	if (!map) {
		fprintf(stderr, "Failed to find high_mcg_ops map\n");
		err = 1;
		goto cleanup;
	}

	opts.relative_fd = high_fd;
	high_link = bpf_map__attach_struct_ops_opts(map, &opts);
	if (!high_link) {
		fprintf(stderr, "Failed to attach high_mcg_ops to %s: %s\n",
			high_path, strerror(errno));
		err = 1;
		goto cleanup;
	}
	printf("Attached high_mcg_ops to %s\n", high_path);

	/* Attach LOW cgroup struct_ops */
	map = bpf_object__find_map_by_name(skel->obj, "low_mcg_ops");
	if (!map) {
		fprintf(stderr, "Failed to find low_mcg_ops map\n");
		err = 1;
		goto cleanup;
	}

	for (i = 0; i < low_count; i++) {
		opts.relative_fd = low_fds[i];
		low_links[i] = bpf_map__attach_struct_ops_opts(map, &opts);
		if (!low_links[i]) {
			fprintf(stderr, "Failed to attach low_mcg_ops to %s: %s\n",
				low_paths[i], strerror(errno));
			err = 1;
			goto cleanup;
		}
		printf("Attached low_mcg_ops to %s\n", low_paths[i]);
	}

	printf("\nBPF program loaded and attached. Press Ctrl+C to exit.\n");
	printf("\n");

	/* Main loop - print stats periodically */
	while (running) {
		__u64 stats[4] = {};
		__u32 key;

		for (key = 0; key < 4; key++) {
			bpf_map_lookup_elem(bpf_map__fd(skel->maps.stats), &key, &stats[key]);
		}

		if (verbose) {
			printf("\rStats: high_delay_calls=%llu active=%llu below_low_calls=%llu active=%llu",
			       (unsigned long long)stats[0],
			       (unsigned long long)stats[1],
			       (unsigned long long)stats[2],
			       (unsigned long long)stats[3]);
			fflush(stdout);
		}

		sleep(1);
	}

	printf("\n\nFinal stats:\n");
	{
		__u64 stats[4] = {};
		__u32 key;

		for (key = 0; key < 4; key++) {
			bpf_map_lookup_elem(bpf_map__fd(skel->maps.stats), &key, &stats[key]);
		}

		printf("  get_high_delay_ms calls: %llu (active: %llu)\n",
		       (unsigned long long)stats[0], (unsigned long long)stats[1]);
		printf("  below_low calls: %llu (active: %llu)\n",
		       (unsigned long long)stats[2], (unsigned long long)stats[3]);
	}

cleanup:
	printf("\nCleaning up...\n");

	bpf_link__destroy(tp_link);
	bpf_link__destroy(high_link);
	for (i = 0; i < low_count; i++)
		bpf_link__destroy(low_links[i]);

	memcg_priority__destroy(skel);

	if (high_fd >= 0)
		close(high_fd);
	for (i = 0; i < low_count; i++) {
		if (low_fds[i] >= 0)
			close(low_fds[i]);
	}

	return err;
}
