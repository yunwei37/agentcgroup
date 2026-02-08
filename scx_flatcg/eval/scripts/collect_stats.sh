#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# collect_stats.sh - Collect CPU usage statistics from cgroups

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/cgroup_helper.sh"

# Collect CPU stats for multiple cgroups over a duration
# Usage: collect_cpu_stats <duration_seconds> <cgroup1> [cgroup2] ...
collect_cpu_stats() {
    local duration="$1"
    shift
    local cgroups=("$@")

    # Get initial values
    declare -A start_usage
    for cg in "${cgroups[@]}"; do
        start_usage["${cg}"]=$(get_cpu_usage "${cg}")
    done

    local start_time=$(date +%s.%N)
    sleep "${duration}"
    local end_time=$(date +%s.%N)

    local elapsed=$(echo "${end_time} - ${start_time}" | bc)

    # Get final values and calculate
    echo "=== CPU Usage Statistics (${elapsed}s) ==="
    echo ""
    printf "%-20s %15s %15s %10s\n" "Cgroup" "CPU Time (us)" "CPU Time (s)" "CPU %"
    printf "%-20s %15s %15s %10s\n" "------" "-------------" "------------" "-----"

    local total_usage=0
    declare -A usage_delta

    for cg in "${cgroups[@]}"; do
        local end_usage=$(get_cpu_usage "${cg}")
        local delta=$((end_usage - ${start_usage["${cg}"]}))
        usage_delta["${cg}"]=${delta}
        total_usage=$((total_usage + delta))

        local delta_sec=$(echo "scale=3; ${delta} / 1000000" | bc)
        local cpu_pct=$(echo "scale=2; ${delta} / (${elapsed} * 1000000) * 100" | bc)

        printf "%-20s %15d %15.3f %9.2f%%\n" "${cg}" "${delta}" "${delta_sec}" "${cpu_pct}"
    done

    echo ""
    echo "=== Weight Proportions ==="
    if [[ ${total_usage} -gt 0 ]]; then
        for cg in "${cgroups[@]}"; do
            local proportion=$(echo "scale=4; ${usage_delta["${cg}"]} / ${total_usage}" | bc)
            printf "%-20s: %.2f%%\n" "${cg}" $(echo "${proportion} * 100" | bc)
        done
    fi
}

# Compare actual vs expected CPU ratios
# Usage: verify_cpu_ratios <cgroup1:weight1> <cgroup2:weight2> ...
verify_cpu_ratios() {
    local total_weight=0
    declare -A weights
    declare -A usage

    for arg in "$@"; do
        local cg="${arg%:*}"
        local w="${arg#*:}"
        weights["${cg}"]=${w}
        total_weight=$((total_weight + w))
        usage["${cg}"]=$(get_cpu_usage "${cg}")
    done

    local total_usage=0
    for cg in "${!usage[@]}"; do
        total_usage=$((total_usage + ${usage["${cg}"]}))
    done

    echo ""
    echo "=== Ratio Verification ==="
    printf "%-20s %10s %12s %12s %10s\n" "Cgroup" "Weight" "Expected %" "Actual %" "Deviation"
    printf "%-20s %10s %12s %12s %10s\n" "------" "------" "----------" "--------" "---------"

    local max_deviation=0
    for cg in "${!weights[@]}"; do
        local expected=$(echo "scale=4; ${weights["${cg}"]} / ${total_weight} * 100" | bc)
        local actual=0
        if [[ ${total_usage} -gt 0 ]]; then
            actual=$(echo "scale=4; ${usage["${cg}"]} / ${total_usage} * 100" | bc)
        fi
        local deviation=$(echo "scale=2; ${actual} - ${expected}" | bc)
        local abs_deviation=${deviation#-}

        printf "%-20s %10d %11.2f%% %11.2f%% %9.2f%%\n" \
            "${cg}" "${weights["${cg}"]}" "${expected}" "${actual}" "${deviation}"

        if (( $(echo "${abs_deviation} > ${max_deviation}" | bc -l) )); then
            max_deviation=${abs_deviation}
        fi
    done

    echo ""
    echo "Maximum deviation: ${max_deviation}%"

    # Return success if deviation is within 10%
    if (( $(echo "${max_deviation} < 10" | bc -l) )); then
        return 0
    else
        return 1
    fi
}

# If run directly, show usage
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "Usage: source this script or call functions directly"
    echo ""
    echo "Functions:"
    echo "  collect_cpu_stats <duration> <cgroup1> [cgroup2] ..."
    echo "  verify_cpu_ratios <cgroup1:weight1> <cgroup2:weight2> ..."
fi
