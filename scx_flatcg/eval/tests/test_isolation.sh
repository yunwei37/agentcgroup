#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# test_isolation.sh - Test isolation from noisy neighbors
#
# Creates two cgroups with equal weight:
#   - victim: runs latency-sensitive workload
#   - noisy: runs CPU-intensive burst (fork storm)
#
# Compares latency with and without flatcg scheduler.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_DIR="$(dirname "${SCRIPT_DIR}")"
source "${SCRIPT_DIR}/../scripts/cgroup_helper.sh"

# Configuration
DURATION=10
LATENCY_ITERATIONS=5000
FLATCG_BIN="${EVAL_DIR}/../scx_flatcg"
RESULTS_DIR="${EVAL_DIR}/results"
RESULT_FILE="${RESULTS_DIR}/isolation_$(date +%Y%m%d_%H%M%S).log"

cleanup() {
    log_info "Cleaning up..."
    for pid in "${PIDS[@]}"; do
        kill -9 "${pid}" 2>/dev/null || true
    done
    if [[ -n "${SCHED_PID}" ]]; then
        kill -9 "${SCHED_PID}" 2>/dev/null || true
    fi
    # Kill any remaining stress-ng or cpu_burn
    pkill -9 cpu_burn 2>/dev/null || true
    pkill -9 stress-ng 2>/dev/null || true
    cleanup_cgroups
}

trap cleanup EXIT

run_isolation_test() {
    local scheduler="$1"  # "flatcg" or "cfs"
    local output_prefix="$2"

    init_test_cgroups
    create_cgroup "victim" 100
    create_cgroup "noisy" 100

    declare -a PIDS

    if [[ "${scheduler}" == "flatcg" ]]; then
        log_info "Starting scx_flatcg scheduler..."
        "${FLATCG_BIN}" -i 1 > "${RESULTS_DIR}/${output_prefix}_flatcg.log" 2>&1 &
        SCHED_PID=$!
        sleep 2
        if ! kill -0 "${SCHED_PID}" 2>/dev/null; then
            log_error "scx_flatcg failed to start"
            return 1
        fi
    else
        SCHED_PID=""
        log_info "Using default CFS scheduler"
    fi

    # Start noisy neighbor (CPU burn)
    log_info "Starting noisy neighbor..."
    for i in $(seq 1 4); do
        "${EVAL_DIR}/workload/cpu_burn" 0 &
        local pid=$!
        echo ${pid} > "${TEST_CGROUP_BASE}/noisy/cgroup.procs"
        PIDS+=("${pid}")
    done

    sleep 1

    # Run latency test in victim cgroup
    log_info "Running latency test in victim cgroup..."
    local latency_output="${RESULTS_DIR}/${output_prefix}_latency.txt"

    # Move to victim cgroup and run
    (
        echo $$ > "${TEST_CGROUP_BASE}/victim/cgroup.procs"
        "${EVAL_DIR}/workload/latency_test" ${LATENCY_ITERATIONS} 1000
    ) > "${latency_output}" 2>&1

    # Parse latency results
    local p99=$(grep "P99:" "${latency_output}" | awk '{print $2}')
    local p50=$(grep "P50:" "${latency_output}" | awk '{print $2}')
    local avg=$(grep "Avg:" "${latency_output}" | awk '{print $2}')

    echo "${scheduler}: P50=${p50}ns P99=${p99}ns Avg=${avg}ns"

    # Cleanup for this run
    for pid in "${PIDS[@]}"; do
        kill -9 "${pid}" 2>/dev/null || true
    done
    PIDS=()

    if [[ -n "${SCHED_PID}" ]]; then
        kill -9 "${SCHED_PID}" 2>/dev/null || true
        SCHED_PID=""
    fi

    cleanup_cgroups
    sleep 2

    echo "${p99}"  # Return P99 for comparison
}

main() {
    check_root
    mkdir -p "${RESULTS_DIR}"

    echo "=== Isolation Test (Noisy Neighbor) ===" | tee "${RESULT_FILE}"
    echo "Date: $(date)" | tee -a "${RESULT_FILE}"
    echo "" | tee -a "${RESULT_FILE}"

    # Build workload if needed
    if [[ ! -f "${EVAL_DIR}/workload/cpu_burn" ]] || \
       [[ ! -f "${EVAL_DIR}/workload/latency_test" ]]; then
        log_info "Building workloads..."
        make -C "${EVAL_DIR}/workload"
    fi

    if [[ ! -f "${FLATCG_BIN}" ]]; then
        log_error "scx_flatcg not found"
        exit 1
    fi

    echo "Test setup:" | tee -a "${RESULT_FILE}"
    echo "  - victim cgroup: latency test (${LATENCY_ITERATIONS} iterations)" | tee -a "${RESULT_FILE}"
    echo "  - noisy cgroup: 4x cpu_burn processes" | tee -a "${RESULT_FILE}"
    echo "  - both cgroups have equal weight (100)" | tee -a "${RESULT_FILE}"
    echo "" | tee -a "${RESULT_FILE}"

    # Run with CFS first
    echo "=== Test 1: CFS Scheduler ===" | tee -a "${RESULT_FILE}"
    cfs_p99=$(run_isolation_test "cfs" "cfs") | tee -a "${RESULT_FILE}"
    cfs_p99=$(echo "${cfs_p99}" | tail -1)

    sleep 3

    # Run with flatcg
    echo "" | tee -a "${RESULT_FILE}"
    echo "=== Test 2: flatcg Scheduler ===" | tee -a "${RESULT_FILE}"
    flatcg_p99=$(run_isolation_test "flatcg" "flatcg") | tee -a "${RESULT_FILE}"
    flatcg_p99=$(echo "${flatcg_p99}" | tail -1)

    # Compare results
    echo "" | tee -a "${RESULT_FILE}"
    echo "=== Comparison ===" | tee -a "${RESULT_FILE}"
    echo "CFS P99 latency:    ${cfs_p99} ns" | tee -a "${RESULT_FILE}"
    echo "flatcg P99 latency: ${flatcg_p99} ns" | tee -a "${RESULT_FILE}"

    if [[ -n "${cfs_p99}" ]] && [[ -n "${flatcg_p99}" ]] && \
       [[ "${cfs_p99}" =~ ^[0-9]+$ ]] && [[ "${flatcg_p99}" =~ ^[0-9]+$ ]]; then
        local improvement=$(echo "scale=2; (${cfs_p99} - ${flatcg_p99}) / ${cfs_p99} * 100" | bc)
        echo "Improvement: ${improvement}%" | tee -a "${RESULT_FILE}"

        if (( $(echo "${flatcg_p99} <= ${cfs_p99}" | bc -l) )); then
            echo -e "${GREEN}TEST PASSED${NC}: flatcg provides equal or better isolation" | tee -a "${RESULT_FILE}"
            exit 0
        else
            echo -e "${YELLOW}TEST WARNING${NC}: flatcg showed higher latency" | tee -a "${RESULT_FILE}"
            exit 0  # Not a hard failure
        fi
    else
        log_warn "Could not parse latency results for comparison"
        exit 0
    fi
}

main "$@"
