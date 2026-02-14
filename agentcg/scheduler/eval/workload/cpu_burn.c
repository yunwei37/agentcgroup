// SPDX-License-Identifier: GPL-2.0
/*
 * cpu_burn.c - CPU intensive workload for testing scheduler fairness
 *
 * Usage: cpu_burn [duration_seconds]
 *   If duration is 0 or not specified, runs forever.
 */

#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <signal.h>
#include <unistd.h>

static volatile int running = 1;

static void sighandler(int sig)
{
    running = 0;
}

int main(int argc, char **argv)
{
    int duration = 0;
    time_t start, now;
    unsigned long long iterations = 0;

    if (argc > 1)
        duration = atoi(argv[1]);

    signal(SIGINT, sighandler);
    signal(SIGTERM, sighandler);

    start = time(NULL);

    /* Simple CPU burn loop */
    while (running) {
        /* Do some work to prevent optimization */
        for (int i = 0; i < 1000000; i++) {
            iterations++;
            __asm__ volatile("" ::: "memory");
        }

        if (duration > 0) {
            now = time(NULL);
            if (now - start >= duration)
                break;
        }
    }

    printf("cpu_burn: completed %llu iterations\n", iterations);
    return 0;
}
