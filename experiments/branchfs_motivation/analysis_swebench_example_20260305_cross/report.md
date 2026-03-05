# eBPF Cross Analysis Report

## Data Source Clarification
- `resource_plot.png` uses `resources.json + tool_calls.json` (container CPU/memory + tool timeline), not raw eBPF events.
- This report uses `ebpf_trace.jsonl` as the primary source and cross-checks with `tool_calls.json` and `resources.json`.

## Run Overview
| run_name                         | duration_s | event_total | summary_total | write_mb | tool_calls | cpu_avg% | mem_avg_mb |
| -------------------------------- | ---------- | ----------- | ------------- | -------- | ---------- | -------- | ---------- |
| swebench_example_20260304_233348 | 114.58     | 6736        | 13029         | 29.8     | 37         | 20.59    | 335.31     |

## Deep Dive: swebench_example_20260304_233348
- Total eBPF events: `6736`; SUMMARY aggregate count: `13029`; WRITE bytes: `29.804 MB`.
- Resource alignment: corr(events/s, CPU%)=`0.2359`, corr(events/s, MemMB)=`-0.3518`.

### Top Event/Summary Types
- Event types: FILE_OPEN=4290, SUMMARY=2138, EXEC=162, EXIT=144, CLOCK_SYNC=2
- Summary types: WRITE=5687, DIR_CREATE=3080, FILE_TRUNCATE=2011, FILE_DELETE=1163, PROC_FORK=378, FILE_RENAME=233, NET_CONNECT=158, CHDIR=120

### Tool-Window Cross Metrics
| tool | calls | duration_s | events_in_window | event_rate/s | write_mb/s |
| ---- | ----- | ---------- | ---------------- | ------------ | ---------- |
| Bash | 26    | 21.805     | 5642             | 258.748      | 0.4711     |
| Glob | 1     | 0.118      | 34               | 288.136      | 40.4371    |
| Edit | 4     | 0.077      | 15               | 194.805      | 0.0        |
| Read | 6     | 0.018      | 9                | 499.997      | 0.1502     |

### Process/Path Hotspots
- Top comm by SUMMARY count: python=6258, pip=1334, HTTP Client=1188, python3=953, Bun Pool 3=589, zsh=464, Bun Pool 2=431, Bun Pool 1=364
- Top path prefixes: /usr/lib=2298, /home/yunwei37=773, /lib/x86_64-linux-gnu=564, /etc/ld.so.cache=157, /usr/share=115, /testbed/starlette=107, /tmp/claude-1000=95, /usr/bin=94

## Figures
- `plots/01_event_type_counts.png`
- `plots/02_summary_type_counts.png`
- `plots/03_summary_type_bytes.png`
- `plots/04_timeline_events_tools.png`
- `plots/05_timeline_resources_vs_events.png`
- `plots/06_tool_cross_metrics.png`
- `plots/07_process_contribution.png`
- `plots/08_path_hotspots.png`

## Remaining Gaps
- WRITE path resolution still depends on fd/path mapping quality; unresolved fd writes remain.
- SUMMARY is periodic aggregate, not every syscall event; short bursts may be merged.
- Strict causal mapping from a single tool call to exact syscall sequence is approximate when calls overlap.
- No automatic semantic phase labels yet (setup/edit/test/fix) for higher-level interpretation.

