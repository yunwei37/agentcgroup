# SPDX-License-Identifier: GPL-2.0
# Standalone Makefile for scx_flatcg - out of tree build
# Uses local libbpf and bpftool from third_party

# Scheduler name
SCHED_NAME = scx_flatcg

# Compiler and tools
CLANG ?= clang
CC ?= gcc

# Project paths
PROJECT_ROOT := $(shell git rev-parse --show-toplevel)
SCX_ROOT := $(PROJECT_ROOT)/third_party/scx
BPFTOOL_ROOT := $(PROJECT_ROOT)/third_party/bpftool

# Local bpftool and libbpf paths
BPFTOOL := $(BPFTOOL_ROOT)/src/bpftool
LIBBPF_DIR := $(BPFTOOL_ROOT)/src/libbpf
LIBBPF_INCLUDE := $(LIBBPF_DIR)/include
LIBBPF_LIB := $(LIBBPF_DIR)/libbpf.a

# Include paths - matching the main scx build system
SCX_INCLUDES = \
    -I$(SCX_ROOT)/scheds/include \
    -I$(SCX_ROOT)/scheds/include/scx \
    -I$(SCX_ROOT)/scheds/include/bpf-compat \
    -I$(SCX_ROOT)/scheds/include/lib \
    -I$(SCX_ROOT)/scheds/vmlinux \
    -I$(SCX_ROOT)/scheds/vmlinux/arch/x86

# System includes
SYS_INCLUDES = \
    -I$(LIBBPF_INCLUDE) \
    -idirafter /usr/lib/llvm-19/lib/clang/19/include \
    -idirafter /usr/local/include \
    -idirafter /usr/include/x86_64-linux-gnu \
    -idirafter /usr/include

# BPF compilation flags
BPF_CFLAGS = -g -O2 -Wall -Wno-compare-distinct-pointer-types \
    -D__TARGET_ARCH_x86 -mcpu=v3 -mlittle-endian \
    $(SYS_INCLUDES) \
    $(SCX_INCLUDES)

# User space compilation flags
USER_CFLAGS = -O2 -g -Wall \
    -I$(LIBBPF_INCLUDE) \
    -I. \
    $(SCX_INCLUDES)

# Link against local libbpf.a
USER_LDFLAGS = $(LIBBPF_LIB) -lelf -lz -lzstd

# Build targets
BPF_SRC = $(SCHED_NAME).bpf.c
BPF_OBJ = $(SCHED_NAME).bpf.o
SKEL_H = $(SCHED_NAME).bpf.skel.h
TARGET = $(SCHED_NAME)

.PHONY: all clean help bpftool

all: $(TARGET)

# Build bpftool first if needed
$(BPFTOOL):
	@echo "  BUILD   bpftool"
	$(MAKE) -C $(BPFTOOL_ROOT)/src

# Compile BPF program
$(BPF_OBJ): $(BPF_SRC) $(SCHED_NAME).h
	@echo "  BPF     $@"
	$(CLANG) $(BPF_CFLAGS) -target bpf -c $< -o $@

# Generate BPF skeleton
$(SKEL_H): $(BPF_OBJ) $(BPFTOOL)
	@echo "  SKEL    $@"
	$(BPFTOOL) gen skeleton $< name $(SCHED_NAME) > $@

# Compile user space program
$(TARGET): $(SKEL_H) $(SCHED_NAME).c $(SCHED_NAME).h $(LIBBPF_LIB)
	@echo "  CC      $@"
	$(CC) $(USER_CFLAGS) $(SCHED_NAME).c -o $@ $(USER_LDFLAGS)

# Ensure libbpf is built (built together with bpftool)
$(LIBBPF_LIB): $(BPFTOOL)
	@test -f $@ || (echo "libbpf not found, rebuilding bpftool..." && $(MAKE) -C $(BPFTOOL_ROOT)/src)

bpftool: $(BPFTOOL)

clean:
	@echo "Cleaning build artifacts..."
	rm -f $(BPF_OBJ) $(SKEL_H) $(TARGET)

clean-all: clean
	@echo "Cleaning bpftool..."
	$(MAKE) -C $(BPFTOOL_ROOT)/src clean

help:
	@echo "scx_flatcg out-of-tree build"
	@echo ""
	@echo "Targets:"
	@echo "  make          - Build scx_flatcg"
	@echo "  make bpftool  - Build bpftool only"
	@echo "  make clean    - Clean scheduler artifacts"
	@echo "  make clean-all - Clean everything including bpftool"
	@echo ""
	@echo "Paths:"
	@echo "  SCX_ROOT=$(SCX_ROOT)"
	@echo "  BPFTOOL=$(BPFTOOL)"
	@echo "  LIBBPF_LIB=$(LIBBPF_LIB)"
