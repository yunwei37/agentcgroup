# Bottleneck Attribution Report

## Method
- Phase classification from tool-call semantics (`discovery/editing/testing/build_install/vcs_revert/runtime_probe/other`).
- eBPF events are aligned via `CLOCK_SYNC(start)` and attributed to phase windows.
- Bottleneck score = `0.5*time_share + 0.3*event_share + 0.2*write_share`.

## Aggregate Ranking (Across Runs)
| phase         | score_mean | score_cv | time_share_mean | event_share_mean | write_share_mean |
| ------------- | ---------- | -------- | --------------- | ---------------- | ---------------- |
| testing       | 0.697658   | 0.164012 | 0.882143        | 0.484608         | 0.556023         |
| discovery     | 0.189788   | 0.358567 | 0.032563        | 0.35021          | 0.342219         |
| build_install | 0.064904   | 1.732051 | 0.042919        | 0.077305         | 0.101264         |
| other         | 0.023055   | 1.732051 | 0.019726        | 0.043934         | 6.2e-05          |
| runtime_probe | 0.022027   | 1.608297 | 0.01913         | 0.041252         | 0.000431         |
| editing       | 0.002566   | 0.361821 | 0.003518        | 0.002692         | 0.0              |
| vcs_revert    | 0.0        | 0.0      | 0.0             | 0.0              | 0.0              |

## Main Finding
- Dominant bottleneck phase: `testing` (highest mean bottleneck score).
- If `testing` dominates, optimization target is test-loop efficiency and rerun policy.
- If `discovery` dominates, optimization target is context retrieval/selection quality.

## Per-Run Summary
| run                                                         | top_phase | top_score | tool_calls | tool_time_s | events_in_windows | write_mb_in_windows |
| ----------------------------------------------------------- | --------- | --------- | ---------- | ----------- | ----------------- | ------------------- |
| swebench_example_20260304_233348                            | testing   | 0.579073  | 37         | 22.017999   | 5695              | 15.042855           |
| sweb.eval.x86_64.encode_1776_starlette-1147_20260305_004649 | testing   | 0.755446  | 22         | 7.264       | 2704              | 2.606988            |
| sweb.eval.x86_64.encode_1776_starlette-1147_20260305_004821 | testing   | 0.599369  | 26         | 8.859       | 2959              | 17.281661           |
| sweb.eval.x86_64.encode_1776_starlette-1147_20260305_005002 | testing   | 0.856746  | 28         | 11.605      | 3394              | 4.167665            |

## Deep Dive: swebench_example_20260304_233348
- `testing`: score=0.579073, time_share=0.805432, event_share=0.415979, write_share=0.257816, top_summary=[('DIR_CREATE', 2861), ('FILE_TRUNCATE', 2006), ('WRITE', 1799)]
- `discovery`: score=0.153956, time_share=0.017349, event_share=0.259526, write_share=0.337119, top_summary=[('WRITE', 322), ('PROC_FORK', 127), ('MMAP_SHARED', 44)]
- `build_install`: score=0.259616, time_share=0.171678, event_share=0.309219, write_share=0.405057, top_summary=[('FILE_DELETE', 1005), ('WRITE', 925), ('DIR_CREATE', 189)]

## Caveats
- Phase classification is rule-based; future work should use model-side action labels.
- Overlapping tool calls are resolved by fixed phase priority; this can bias attribution.
- `SUMMARY` is aggregated at flush interval, not raw per-syscall stream.

## Figures
- `plots/01_phase_time_share.png`
- `plots/02_phase_event_share.png`
- `plots/03_phase_write_share.png`
- `plots/04_phase_bottleneck_score.png`
- `plots/05_run_phase_score_heatmap.png`

