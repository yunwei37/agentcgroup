#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# test_weight_fairness.sh - Test CPU weight fairness with flatcg
#
# Creates 3 cgroups with weights 100:200:300 and verifies
# CPU allocation matches the weight ratios.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_DIR="$(dirname "${SCRIPT_DIR}")"
source "${SCRIPT_DIR}/../scripts/cgroup_helper.sh"
source "${SCRIPT_DIR}/../scripts/collect_stats.sh"

# Configuration
DURATION=10  # seconds
WEIGHTS=(100 200 300)
CGROUPS=("cg_w100" "cg_w200" "cg_w300")
FLATCG_BIN="${EVAL_DIR}/../scx_flatcg"

# Results
RESULTS_DIR="${EVAL_DIR}/results"
RESULT_FILE="${RESULTS_DIR}/weight_fairness_$(date +%Y%m%d_%H%M%S).log"

cleanup() {
    log_info "Cleaning up..."
    # Kill workloads
    for pid in "${PIDS[@]}"; do
        kill -9 "${pid}" 2>/dev/null || true
    done
    # Stop scheduler
    if [[ -n "${SCHED_PID}" ]]; then
        kill -9 "${SCHED_PID}" 2>/dev/null || true
    fi
    cleanup_cgroups
}

trap cleanup EXIT

main() {
    check_root
    mkdir -p "${RESULTS_DIR}"

    echo "=== Weight Fairness Test ===" | tee "${RESULT_FILE}"
    echo "Date: $(date)" | tee -a "${RESULT_FILE}"
    echo "Duration: ${DURATION}s" | tee -a "${RESULT_FILE}"
    echo "" | tee -a "${RESULT_FILE}"

    # Build workload if needed
    if [[ ! -f "${EVAL_DIR}/workload/cpu_burn" ]]; then
        log_info "Building workloads..."
        make -C "${EVAL_DIR}/workload"
    fi

    # Check if flatcg binary exists
    if [[ ! -f "${FLATCG_BIN}" ]]; then
        log_error "scx_flatcg not found at ${FLATCG_BIN}"
        log_error "Please run 'make' in the scx_flatcg directory first"
        exit 1
    fi

    # Initialize cgroups
    init_test_cgroups

    # Create test cgroups
    for i in "${!CGROUPS[@]}"; do
        create_cgroup "${CGROUPS[$i]}" "${WEIGHTS[$i]}"
    done

    echo "Cgroup structure:" | tee -a "${RESULT_FILE}"
    print_cgroup_tree | tee -a "${RESULT_FILE}"
    echo "" | tee -a "${RESULT_FILE}"

    # Start scx_flatcg scheduler
    log_info "Starting scx_flatcg scheduler..."
    "${FLATCG_BIN}" -i 1 > "${RESULTS_DIR}/flatcg_output.log" 2>&1 &
    SCHED_PID=$!
    sleep 2

    # Check if scheduler is running
    if ! kill -0 "${SCHED_PID}" 2>/dev/null; then
        log_error "scx_flatcg failed to start"
        cat "${RESULTS_DIR}/flatcg_output.log"
        exit 1
    fi
    log_info "scx_flatcg started (PID: ${SCHED_PID})"

    # Start workloads in each cgroup
    # Run multiple processes per cgroup to create CPU contention
    local PROCS_PER_CGROUP=$(( $(nproc) * 2 ))
    declare -a PIDS
    for i in "${!CGROUPS[@]}"; do
        local cg="${CGROUPS[$i]}"
        log_info "Starting ${PROCS_PER_CGROUP} cpu_burn processes in ${cg}..."
        for j in $(seq 1 ${PROCS_PER_CGROUP}); do
            "${EVAL_DIR}/workload/cpu_burn" 0 &
            local pid=$!
            echo ${pid} > "${TEST_CGROUP_BASE}/${cg}/cgroup.procs"
            PIDS+=("${pid}")
        done
    done

    # Wait for processes to stabilize
    sleep 2

    # Record initial CPU usage
    declare -A start_usage
    for cg in "${CGROUPS[@]}"; do
        start_usage["${cg}"]=$(get_cpu_usage "${cg}")
    done

    log_info "Running for ${DURATION} seconds..."
    sleep "${DURATION}"

    # Record final CPU usage
    declare -A end_usage
    declare -A delta_usage
    local total_usage=0

    for cg in "${CGROUPS[@]}"; do
        end_usage["${cg}"]=$(get_cpu_usage "${cg}")
        delta_usage["${cg}"]=$((${end_usage["${cg}"]} - ${start_usage["${cg}"]}))
        total_usage=$((total_usage + ${delta_usage["${cg}"]}))
    done

    # Calculate and display results
    echo "" | tee -a "${RESULT_FILE}"
    echo "=== Results ===" | tee -a "${RESULT_FILE}"
    printf "%-15s %10s %15s %12s %12s %10s\n" \
        "Cgroup" "Weight" "CPU Time (us)" "Expected %" "Actual %" "Deviation" | tee -a "${RESULT_FILE}"
    printf "%-15s %10s %15s %12s %12s %10s\n" \
        "------" "------" "-------------" "----------" "--------" "---------" | tee -a "${RESULT_FILE}"

    local total_weight=0
    for w in "${WEIGHTS[@]}"; do
        total_weight=$((total_weight + w))
    done

    local max_deviation=0
    local pass=true

    for i in "${!CGROUPS[@]}"; do
        local cg="${CGROUPS[$i]}"
        local w="${WEIGHTS[$i]}"
        local expected=$(awk "BEGIN {printf \"%.2f\", ${w} / ${total_weight} * 100}")
        local actual="0.00"
        if [[ ${total_usage} -gt 0 ]]; then
            actual=$(awk "BEGIN {printf \"%.2f\", ${delta_usage["${cg}"]} / ${total_usage} * 100}")
        fi
        local deviation=$(awk "BEGIN {printf \"%.2f\", ${actual} - ${expected}}")
        local abs_deviation=$(awk "BEGIN {d=${deviation}; if(d<0) d=-d; printf \"%.2f\", d}")

        printf "%-15s %10d %15d %11s%% %11s%% %9s%%\n" \
            "${cg}" "${w}" "${delta_usage["${cg}"]}" "${expected}" "${actual}" "${deviation}" | tee -a "${RESULT_FILE}"

        if (( $(awk "BEGIN {print (${abs_deviation} > ${max_deviation})}") )); then
            max_deviation=${abs_deviation}
        fi
        if (( $(awk "BEGIN {print (${abs_deviation} > 10)}") )); then
            pass=false
        fi
    done

    echo "" | tee -a "${RESULT_FILE}"
    echo "Maximum deviation: ${max_deviation}%" | tee -a "${RESULT_FILE}"
    echo "" | tee -a "${RESULT_FILE}"

    if ${pass}; then
        echo -e "${GREEN}TEST PASSED${NC}: All deviations within 10%" | tee -a "${RESULT_FILE}"
        exit 0
    else
        echo -e "${RED}TEST FAILED${NC}: Deviation exceeded 10%" | tee -a "${RESULT_FILE}"
        exit 1
    fi
}

main "$@"
