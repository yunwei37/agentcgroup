# SPDX-License-Identifier: GPL-2.0
# Makefile for memcg_priority BPF loader

# Program name
PROG_NAME = memcg_priority

# Compiler and tools
CLANG ?= clang
CC ?= gcc

# Project paths
PROJECT_ROOT := $(shell git rev-parse --show-toplevel)
KERNEL_ROOT := $(PROJECT_ROOT)/memcg/linux
BPFTOOL_ROOT := $(PROJECT_ROOT)/third_party/bpftool
BPFTOOL := $(BPFTOOL_ROOT)/src/bpftool

# Use libbpf from kernel source (has bpf_map__attach_struct_ops_opts)
LIBBPF_DIR := $(KERNEL_ROOT)/tools/lib/bpf
LIBBPF_INCLUDE := $(LIBBPF_DIR)
LIBBPF_LIB := $(LIBBPF_DIR)/libbpf.a

# BPF helper headers (for bpf_helpers.h, bpf_tracing.h)
BPF_INCLUDE := $(KERNEL_ROOT)/tools/testing/selftests/bpf/tools/include

# System includes for BPF
SYS_INCLUDES = \
    -I$(BPF_INCLUDE) \
    -I$(LIBBPF_INCLUDE) \
    -I. \
    -idirafter /usr/lib/llvm-19/lib/clang/19/include \
    -idirafter /usr/local/include \
    -idirafter /usr/include/x86_64-linux-gnu \
    -idirafter /usr/include

# BPF compilation flags
BPF_CFLAGS = -g -O2 -Wall -Wno-compare-distinct-pointer-types \
    -D__TARGET_ARCH_x86 -mcpu=v3 -mlittle-endian \
    $(SYS_INCLUDES)

# User space compilation flags
USER_CFLAGS = -O2 -g -Wall \
    -I$(LIBBPF_INCLUDE) \
    -I.

# Link against kernel libbpf.a
USER_LDFLAGS = $(LIBBPF_LIB) -lelf -lz -lzstd

# Build targets
BPF_SRC = $(PROG_NAME).bpf.c
BPF_OBJ = $(PROG_NAME).bpf.o
SKEL_H = $(PROG_NAME).bpf.skel.h
TARGET = $(PROG_NAME)

.PHONY: all clean help bpftool vmlinux libbpf

all: $(TARGET)

# Build bpftool if needed
$(BPFTOOL):
	@echo "  BUILD   bpftool"
	$(MAKE) -C $(BPFTOOL_ROOT)/src

# Build libbpf if needed
$(LIBBPF_LIB):
	@echo "  BUILD   libbpf"
	$(MAKE) -C $(LIBBPF_DIR)

libbpf: $(LIBBPF_LIB)

# Generate vmlinux.h from running kernel
vmlinux.h: $(BPFTOOL)
	@echo "  VMLINUX vmlinux.h"
	$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@

# Compile BPF program
$(BPF_OBJ): $(BPF_SRC) $(PROG_NAME).h vmlinux.h
	@echo "  BPF     $@"
	$(CLANG) $(BPF_CFLAGS) -target bpf -c $< -o $@

# Generate BPF skeleton
$(SKEL_H): $(BPF_OBJ) $(BPFTOOL)
	@echo "  SKEL    $@"
	$(BPFTOOL) gen skeleton $< name $(PROG_NAME) > $@

# Compile user space program
$(TARGET): $(SKEL_H) $(PROG_NAME).c $(PROG_NAME).h $(LIBBPF_LIB)
	@echo "  CC      $@"
	$(CC) $(USER_CFLAGS) $(PROG_NAME).c -o $@ $(USER_LDFLAGS)

bpftool: $(BPFTOOL)

vmlinux: vmlinux.h

clean:
	@echo "Cleaning build artifacts..."
	rm -f $(BPF_OBJ) $(SKEL_H) $(TARGET)

clean-all: clean
	rm -f vmlinux.h

help:
	@echo "memcg_priority BPF loader build"
	@echo ""
	@echo "Targets:"
	@echo "  make          - Build memcg_priority"
	@echo "  make libbpf   - Build libbpf from kernel source"
	@echo "  make vmlinux  - Generate vmlinux.h from running kernel"
	@echo "  make bpftool  - Build bpftool only"
	@echo "  make clean    - Clean build artifacts"
	@echo "  make clean-all - Clean everything including vmlinux.h"
	@echo ""
	@echo "Usage after build:"
	@echo "  sudo ./memcg_priority --high /path/to/high_cgroup \\"
	@echo "                        --low /path/to/low_cgroup1 \\"
	@echo "                        --low /path/to/low_cgroup2 \\"
	@echo "                        --delay-ms 2000 --below-low"
