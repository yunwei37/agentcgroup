# agentcg - Integrated eBPF agent cgroup management
# Top-level Makefile that delegates to three sub-components

.PHONY: all scheduler memcg process clean clean-all test help

all: scheduler process
	@echo ""
	@echo "NOTE: memcg requires a kernel with memcg_bpf_ops. Build separately with: make memcg"
	@echo ""

scheduler:
	$(MAKE) -C scheduler

memcg:
	$(MAKE) -C memcg

process:
	$(MAKE) -C process

clean:
	$(MAKE) -C scheduler clean
	$(MAKE) -C memcg clean
	$(MAKE) -C process clean

test:
	python3 -m unittest test_agentcgroupd -v

clean-all:
	$(MAKE) -C scheduler clean-all
	$(MAKE) -C memcg clean-all
	$(MAKE) -C process clean-all

help:
	@echo "agentcg - Integrated eBPF agent cgroup management"
	@echo ""
	@echo "Components:"
	@echo "  scheduler  - scx_flatcg CPU scheduler (sched_ext)"
	@echo "  memcg      - memcg_priority memory isolation (memcg_bpf_ops)"
	@echo "  process    - Process lifecycle monitor"
	@echo ""
	@echo "Targets:"
	@echo "  make           - Build all three components"
	@echo "  make scheduler - Build CPU scheduler only"
	@echo "  make memcg     - Build memory isolation only"
	@echo "  make process   - Build process monitor only"
	@echo "  make test      - Run agentcgroupd unit tests"
	@echo "  make clean     - Clean all build artifacts"
	@echo "  make clean-all - Clean everything including vmlinux.h"
	@echo ""
	@echo "Usage:"
	@echo "  sudo python3 agentcgroupd.py [--cgroup-root PATH]"
