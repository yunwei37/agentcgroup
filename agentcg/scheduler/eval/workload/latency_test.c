// SPDX-License-Identifier: GPL-2.0
/*
 * latency_test.c - Measure scheduling latency
 *
 * Periodically sleeps and measures the actual wakeup latency.
 * Outputs latency statistics (min, max, avg, p50, p95, p99).
 *
 * Usage: latency_test [iterations] [sleep_us]
 *   iterations: number of sleep/wake cycles (default: 1000)
 *   sleep_us: sleep duration in microseconds (default: 1000 = 1ms)
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <signal.h>
#include <unistd.h>
#include <string.h>

#define MAX_SAMPLES 100000

static volatile int running = 1;
static long latencies[MAX_SAMPLES];
static int num_samples = 0;

static void sighandler(int sig)
{
    running = 0;
}

static long timespec_diff_ns(struct timespec *start, struct timespec *end)
{
    return (end->tv_sec - start->tv_sec) * 1000000000L +
           (end->tv_nsec - start->tv_nsec);
}

static int compare_long(const void *a, const void *b)
{
    long la = *(const long *)a;
    long lb = *(const long *)b;
    return (la > lb) - (la < lb);
}

static void print_stats(void)
{
    if (num_samples == 0) {
        printf("No samples collected\n");
        return;
    }

    /* Sort for percentile calculation */
    qsort(latencies, num_samples, sizeof(long), compare_long);

    long min = latencies[0];
    long max = latencies[num_samples - 1];
    long sum = 0;
    for (int i = 0; i < num_samples; i++)
        sum += latencies[i];
    long avg = sum / num_samples;

    int p50_idx = num_samples * 50 / 100;
    int p95_idx = num_samples * 95 / 100;
    int p99_idx = num_samples * 99 / 100;

    printf("\n=== Latency Statistics (%d samples) ===\n", num_samples);
    printf("Min:  %8ld ns (%6.2f us)\n", min, min / 1000.0);
    printf("Max:  %8ld ns (%6.2f us)\n", max, max / 1000.0);
    printf("Avg:  %8ld ns (%6.2f us)\n", avg, avg / 1000.0);
    printf("P50:  %8ld ns (%6.2f us)\n", latencies[p50_idx], latencies[p50_idx] / 1000.0);
    printf("P95:  %8ld ns (%6.2f us)\n", latencies[p95_idx], latencies[p95_idx] / 1000.0);
    printf("P99:  %8ld ns (%6.2f us)\n", latencies[p99_idx], latencies[p99_idx] / 1000.0);
}

int main(int argc, char **argv)
{
    int iterations = 1000;
    long sleep_us = 1000;  /* 1ms default */
    struct timespec sleep_ts, before, after;

    if (argc > 1)
        iterations = atoi(argv[1]);
    if (argc > 2)
        sleep_us = atol(argv[2]);

    if (iterations > MAX_SAMPLES)
        iterations = MAX_SAMPLES;

    signal(SIGINT, sighandler);
    signal(SIGTERM, sighandler);

    sleep_ts.tv_sec = sleep_us / 1000000;
    sleep_ts.tv_nsec = (sleep_us % 1000000) * 1000;

    printf("latency_test: %d iterations, %ld us sleep\n", iterations, sleep_us);

    for (int i = 0; i < iterations && running; i++) {
        clock_gettime(CLOCK_MONOTONIC, &before);
        nanosleep(&sleep_ts, NULL);
        clock_gettime(CLOCK_MONOTONIC, &after);

        long elapsed = timespec_diff_ns(&before, &after);
        long expected = sleep_us * 1000;
        long latency = elapsed - expected;

        /* Only count positive latency (wakeup delay) */
        if (latency > 0 && num_samples < MAX_SAMPLES) {
            latencies[num_samples++] = latency;
        }
    }

    print_stats();
    return 0;
}
