#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# cgroup_helper.sh - Helper functions for cgroup management

set -e

# Base path for test cgroups
CGROUP_ROOT="/sys/fs/cgroup"
TEST_CGROUP_BASE="${CGROUP_ROOT}/scx_flatcg_test"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "This script must be run as root"
        exit 1
    fi
}

# Check if cgroup v2 is mounted
check_cgroup_v2() {
    if ! mount | grep -q "cgroup2 on ${CGROUP_ROOT}"; then
        log_error "cgroup v2 not mounted at ${CGROUP_ROOT}"
        exit 1
    fi
}

# Initialize test cgroup hierarchy
init_test_cgroups() {
    check_root
    check_cgroup_v2

    if [[ ! -d "${TEST_CGROUP_BASE}" ]]; then
        mkdir -p "${TEST_CGROUP_BASE}"
        log_info "Created test cgroup base: ${TEST_CGROUP_BASE}"
    fi

    # Enable cpu controller
    if [[ -f "${CGROUP_ROOT}/cgroup.subtree_control" ]]; then
        echo "+cpu" > "${CGROUP_ROOT}/cgroup.subtree_control" 2>/dev/null || true
    fi
    if [[ -f "${TEST_CGROUP_BASE}/cgroup.subtree_control" ]]; then
        echo "+cpu" > "${TEST_CGROUP_BASE}/cgroup.subtree_control" 2>/dev/null || true
    fi
}

# Create a cgroup with specified weight
# Usage: create_cgroup <relative_path> <weight>
create_cgroup() {
    local path="$1"
    local weight="${2:-100}"
    local full_path="${TEST_CGROUP_BASE}/${path}"

    mkdir -p "${full_path}"

    # Enable cpu controller for subtree if needed
    local parent_dir=$(dirname "${full_path}")
    if [[ -f "${parent_dir}/cgroup.subtree_control" ]]; then
        echo "+cpu" > "${parent_dir}/cgroup.subtree_control" 2>/dev/null || true
    fi

    # Set weight
    if [[ -f "${full_path}/cpu.weight" ]]; then
        echo "${weight}" > "${full_path}/cpu.weight"
        log_info "Created cgroup ${path} with weight ${weight}"
    else
        log_warn "cpu.weight not available for ${path}"
    fi
}

# Run a command in a cgroup
# Usage: run_in_cgroup <relative_path> <command...>
run_in_cgroup() {
    local path="$1"
    shift
    local full_path="${TEST_CGROUP_BASE}/${path}"

    if [[ ! -d "${full_path}" ]]; then
        log_error "Cgroup ${path} does not exist"
        return 1
    fi

    # Add current shell to cgroup, then exec
    echo $$ > "${full_path}/cgroup.procs"
    exec "$@"
}

# Run a command in a cgroup (background, returns PID)
# Usage: run_in_cgroup_bg <relative_path> <command...>
run_in_cgroup_bg() {
    local path="$1"
    shift
    local full_path="${TEST_CGROUP_BASE}/${path}"

    if [[ ! -d "${full_path}" ]]; then
        log_error "Cgroup ${path} does not exist"
        return 1
    fi

    # Start process and move to cgroup
    "$@" &
    local pid=$!
    echo ${pid} > "${full_path}/cgroup.procs"
    echo ${pid}
}

# Get CPU usage from cgroup (in microseconds)
# Usage: get_cpu_usage <relative_path>
get_cpu_usage() {
    local path="$1"
    local full_path="${TEST_CGROUP_BASE}/${path}"

    if [[ -f "${full_path}/cpu.stat" ]]; then
        grep "^usage_usec" "${full_path}/cpu.stat" | awk '{print $2}'
    else
        echo "0"
    fi
}

# Get CPU usage delta over a time period
# Usage: get_cpu_usage_delta <relative_path> <duration_seconds>
get_cpu_usage_delta() {
    local path="$1"
    local duration="$2"

    local start=$(get_cpu_usage "${path}")
    sleep "${duration}"
    local end=$(get_cpu_usage "${path}")

    echo $((end - start))
}

# Cleanup test cgroups
cleanup_cgroups() {
    check_root

    if [[ ! -d "${TEST_CGROUP_BASE}" ]]; then
        return 0
    fi

    log_info "Cleaning up test cgroups..."

    # Kill all processes in test cgroups
    find "${TEST_CGROUP_BASE}" -name "cgroup.procs" -exec cat {} \; 2>/dev/null | \
        while read pid; do
            kill -9 "${pid}" 2>/dev/null || true
        done

    sleep 1

    # Remove cgroups (leaf first)
    find "${TEST_CGROUP_BASE}" -depth -type d | while read dir; do
        rmdir "${dir}" 2>/dev/null || true
    done

    log_info "Cleanup complete"
}

# Kill all processes in a cgroup
# Usage: kill_cgroup <relative_path>
kill_cgroup() {
    local path="$1"
    local full_path="${TEST_CGROUP_BASE}/${path}"

    if [[ -f "${full_path}/cgroup.procs" ]]; then
        cat "${full_path}/cgroup.procs" | while read pid; do
            kill -9 "${pid}" 2>/dev/null || true
        done
    fi
}

# Print cgroup tree
print_cgroup_tree() {
    if [[ -d "${TEST_CGROUP_BASE}" ]]; then
        find "${TEST_CGROUP_BASE}" -type d | while read dir; do
            local rel_path="${dir#${TEST_CGROUP_BASE}}"
            local weight=""
            if [[ -f "${dir}/cpu.weight" ]]; then
                weight="(weight=$(cat ${dir}/cpu.weight))"
            fi
            echo "${rel_path:-/} ${weight}"
        done
    fi
}
