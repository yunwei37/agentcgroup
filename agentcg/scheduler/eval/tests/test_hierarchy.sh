#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# test_hierarchy.sh - Test hierarchical weight flattening
#
# Creates a nested cgroup hierarchy:
#   root
#   ├── A (weight=100)
#   │   ├── B (weight=100)
#   │   └── C (weight=100)
#   └── D (weight=200)
#
# Expected CPU shares:
#   B: 1/6 (100/200 in A, then A gets 100/300) = 16.67%
#   C: 1/6 = 16.67%
#   D: 2/3 = 66.67%

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_DIR="$(dirname "${SCRIPT_DIR}")"
source "${SCRIPT_DIR}/../scripts/cgroup_helper.sh"

# Configuration
DURATION=10
FLATCG_BIN="${EVAL_DIR}/../scx_flatcg"
RESULTS_DIR="${EVAL_DIR}/results"
RESULT_FILE="${RESULTS_DIR}/hierarchy_$(date +%Y%m%d_%H%M%S).log"

cleanup() {
    log_info "Cleaning up..."
    for pid in "${PIDS[@]}"; do
        kill -9 "${pid}" 2>/dev/null || true
    done
    if [[ -n "${SCHED_PID}" ]]; then
        kill -9 "${SCHED_PID}" 2>/dev/null || true
    fi
    cleanup_cgroups
}

trap cleanup EXIT

main() {
    check_root
    mkdir -p "${RESULTS_DIR}"

    echo "=== Hierarchy Flattening Test ===" | tee "${RESULT_FILE}"
    echo "Date: $(date)" | tee -a "${RESULT_FILE}"
    echo "" | tee -a "${RESULT_FILE}"

    # Build workload if needed
    if [[ ! -f "${EVAL_DIR}/workload/cpu_burn" ]]; then
        log_info "Building workloads..."
        make -C "${EVAL_DIR}/workload"
    fi

    if [[ ! -f "${FLATCG_BIN}" ]]; then
        log_error "scx_flatcg not found"
        exit 1
    fi

    # Initialize cgroups
    init_test_cgroups

    # Create hierarchy
    #   A (100)
    #   ├── B (100)
    #   └── C (100)
    #   D (200)

    create_cgroup "A" 100
    # Enable subtree control for A
    echo "+cpu" > "${TEST_CGROUP_BASE}/A/cgroup.subtree_control" 2>/dev/null || true
    create_cgroup "A/B" 100
    create_cgroup "A/C" 100
    create_cgroup "D" 200

    echo "Cgroup structure:" | tee -a "${RESULT_FILE}"
    print_cgroup_tree | tee -a "${RESULT_FILE}"
    echo "" | tee -a "${RESULT_FILE}"

    echo "Expected CPU shares:" | tee -a "${RESULT_FILE}"
    echo "  B: 16.67% (1/6)" | tee -a "${RESULT_FILE}"
    echo "  C: 16.67% (1/6)" | tee -a "${RESULT_FILE}"
    echo "  D: 66.67% (2/3)" | tee -a "${RESULT_FILE}"
    echo "" | tee -a "${RESULT_FILE}"

    # Start scheduler
    log_info "Starting scx_flatcg scheduler..."
    "${FLATCG_BIN}" -i 1 > "${RESULTS_DIR}/flatcg_hierarchy.log" 2>&1 &
    SCHED_PID=$!
    sleep 2

    if ! kill -0 "${SCHED_PID}" 2>/dev/null; then
        log_error "scx_flatcg failed to start"
        exit 1
    fi

    # Start workloads (only in leaf cgroups B, C, D)
    declare -a PIDS
    LEAF_CGROUPS=("A/B" "A/C" "D")

    for cg in "${LEAF_CGROUPS[@]}"; do
        log_info "Starting cpu_burn in ${cg}..."
        "${EVAL_DIR}/workload/cpu_burn" 0 &
        local pid=$!
        echo ${pid} > "${TEST_CGROUP_BASE}/${cg}/cgroup.procs"
        PIDS+=("${pid}")
    done

    sleep 2

    # Record initial CPU usage
    declare -A start_usage
    for cg in "${LEAF_CGROUPS[@]}"; do
        start_usage["${cg}"]=$(get_cpu_usage "${cg}")
    done

    log_info "Running for ${DURATION} seconds..."
    sleep "${DURATION}"

    # Record final CPU usage
    declare -A end_usage
    declare -A delta_usage
    local total_usage=0

    for cg in "${LEAF_CGROUPS[@]}"; do
        end_usage["${cg}"]=$(get_cpu_usage "${cg}")
        delta_usage["${cg}"]=$((${end_usage["${cg}"]} - ${start_usage["${cg}"]}))
        total_usage=$((total_usage + ${delta_usage["${cg}"]}))
    done

    # Expected proportions
    declare -A expected
    expected["A/B"]=16.67
    expected["A/C"]=16.67
    expected["D"]=66.67

    # Calculate and display results
    echo "" | tee -a "${RESULT_FILE}"
    echo "=== Results ===" | tee -a "${RESULT_FILE}"
    printf "%-15s %15s %12s %12s %10s\n" \
        "Cgroup" "CPU Time (us)" "Expected %" "Actual %" "Deviation" | tee -a "${RESULT_FILE}"
    printf "%-15s %15s %12s %12s %10s\n" \
        "------" "-------------" "----------" "--------" "---------" | tee -a "${RESULT_FILE}"

    local max_deviation=0
    local pass=true

    for cg in "${LEAF_CGROUPS[@]}"; do
        local exp="${expected["${cg}"]}"
        local actual="0.00"
        if [[ ${total_usage} -gt 0 ]]; then
            actual=$(awk "BEGIN {printf \"%.2f\", ${delta_usage["${cg}"]} / ${total_usage} * 100}")
        fi
        local deviation=$(awk "BEGIN {printf \"%.2f\", ${actual} - ${exp}}")
        local abs_deviation=$(awk "BEGIN {d=${deviation}; if(d<0) d=-d; printf \"%.2f\", d}")

        printf "%-15s %15d %11s%% %11s%% %9s%%\n" \
            "${cg}" "${delta_usage["${cg}"]}" "${exp}" "${actual}" "${deviation}" | tee -a "${RESULT_FILE}"

        if (( $(awk "BEGIN {print (${abs_deviation} > ${max_deviation})}") )); then
            max_deviation=${abs_deviation}
        fi
        # Allow 15% deviation for hierarchy test (more complex)
        if (( $(awk "BEGIN {print (${abs_deviation} > 15)}") )); then
            pass=false
        fi
    done

    echo "" | tee -a "${RESULT_FILE}"
    echo "Maximum deviation: ${max_deviation}%" | tee -a "${RESULT_FILE}"
    echo "" | tee -a "${RESULT_FILE}"

    if ${pass}; then
        echo -e "${GREEN}TEST PASSED${NC}: Hierarchy flattening works correctly" | tee -a "${RESULT_FILE}"
        exit 0
    else
        echo -e "${RED}TEST FAILED${NC}: Deviation exceeded 15%" | tee -a "${RESULT_FILE}"
        exit 1
    fi
}

main "$@"
