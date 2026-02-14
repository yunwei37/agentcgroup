# SPDX-License-Identifier: GPL-2.0
# Makefile for eval workloads

CC ?= gcc
CFLAGS = -O2 -Wall -g

TARGETS = cpu_burn latency_test

all: $(TARGETS)

cpu_burn: cpu_burn.c
	$(CC) $(CFLAGS) -o $@ $<

latency_test: latency_test.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f $(TARGETS)

.PHONY: all clean
