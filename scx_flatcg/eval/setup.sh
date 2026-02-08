#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# setup.sh - Setup environment for scx_flatcg evaluation

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/scripts/cgroup_helper.sh"

echo "=== scx_flatcg Evaluation Setup ==="
echo ""

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    log_warn "Some checks require root. Run with sudo for full setup."
fi

# Check cgroup v2
echo "Checking cgroup v2..."
if mount | grep -q "cgroup2 on /sys/fs/cgroup"; then
    log_info "cgroup v2 is mounted"
else
    log_error "cgroup v2 not mounted. Please enable cgroup v2."
    exit 1
fi

# Check required tools
echo ""
echo "Checking required tools..."
MISSING_TOOLS=()

for tool in gcc make bc; do
    if command -v ${tool} &> /dev/null; then
        log_info "${tool} is available"
    else
        log_error "${tool} is missing"
        MISSING_TOOLS+=("${tool}")
    fi
done

if [[ ${#MISSING_TOOLS[@]} -gt 0 ]]; then
    log_error "Please install missing tools: ${MISSING_TOOLS[*]}"
    exit 1
fi

# Build workloads
echo ""
echo "Building workloads..."
make -C "${SCRIPT_DIR}/workload"

# Check scx_flatcg binary
echo ""
echo "Checking scx_flatcg binary..."
if [[ -f "${SCRIPT_DIR}/../scx_flatcg" ]]; then
    log_info "scx_flatcg binary found"
else
    log_warn "scx_flatcg not found. Building..."
    make -C "${SCRIPT_DIR}/.."
fi

# Make test scripts executable
echo ""
echo "Setting up test scripts..."
chmod +x "${SCRIPT_DIR}/scripts/"*.sh
chmod +x "${SCRIPT_DIR}/tests/"*.sh

# Create results directory
mkdir -p "${SCRIPT_DIR}/results"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Run tests with:"
echo "  sudo ./tests/test_weight_fairness.sh"
echo "  sudo ./tests/test_hierarchy.sh"
echo "  sudo ./tests/test_isolation.sh"
