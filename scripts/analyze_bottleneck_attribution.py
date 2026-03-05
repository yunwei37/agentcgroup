#!/usr/bin/env python3
"""
Bottleneck attribution analysis (paper-style) for run_swebench_new outputs.

Given one or more run dirs, classify tool-call windows into phases and attribute:
1) wall-clock tool active time
2) eBPF event pressure
3) eBPF write-byte pressure

Outputs:
  <output_dir>/
    bottleneck_report.md
    bottleneck_metrics.json
    plots/
      01_phase_time_share.png
      02_phase_event_share.png
      03_phase_write_share.png
      04_phase_bottleneck_score.png
      05_run_phase_score_heatmap.png
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


PHASES = [
    "discovery",
    "editing",
    "testing",
    "build_install",
    "vcs_revert",
    "runtime_probe",
    "other",
]


def parse_iso8601(ts: str) -> float:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).timestamp()


def safe_float(x: object, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def n(x: float, d: int = 4) -> float:
    if math.isnan(x) or math.isinf(x):
        return 0.0
    return round(x, d)


@dataclass
class ToolCall:
    tool: str
    start: float
    end: float
    duration: float
    command: str
    phase: str


@dataclass
class EbpfEvent:
    event: str
    summary_type: str
    count: int
    total_bytes: int
    comm: str
    wall_epoch: float


def classify_phase(tool: str, command: str) -> str:
    t = (tool or "").lower()
    c = (command or "").lower()
    if t in {"read", "glob", "grep", "ls"}:
        return "discovery"
    if t in {"edit", "write", "multiedit"}:
        return "editing"
    if t == "bash":
        if any(k in c for k in ["pytest", "unittest", "tox ", "nose", "python -m pytest"]):
            return "testing"
        if any(k in c for k in ["pip install", "pip3 install", "npm install", "yarn install", "poetry install", "apt-get", "conda install", "uv pip"]):
            return "build_install"
        if "git " in c:
            if any(k in c for k in ["git checkout", "git reset", "git stash", "git revert", "git clean"]):
                return "vcs_revert"
            return "discovery"
        if any(k in c for k in ["find ", "ls ", "grep ", "sed -n", "cat ", "head ", "tail "]):
            return "discovery"
        if any(k in c for k in ["python -c", "python ", "node ", "uvicorn", "gunicorn", "flask run"]):
            return "runtime_probe"
    return "other"


def load_tool_calls(path: Path) -> List[ToolCall]:
    if not path.exists():
        return []
    with open(path, "r") as f:
        raw = json.load(f)
    out: List[ToolCall] = []
    for item in raw:
        tool = item.get("tool", "UNKNOWN")
        start = parse_iso8601(item["timestamp"])
        end = parse_iso8601(item.get("end_timestamp", item["timestamp"]))
        if end < start:
            end = start
        inp = item.get("input", {}) or {}
        cmd = inp.get("command", "")
        if not cmd:
            cmd = inp.get("file_path", "") or inp.get("pattern", "") or ""
        phase = classify_phase(tool, cmd)
        out.append(ToolCall(tool=tool, start=start, end=end, duration=end - start, command=cmd, phase=phase))
    return out


def load_ebpf(path: Path) -> List[EbpfEvent]:
    if not path.exists():
        return []
    raw = []
    mono0 = None
    wall0 = None
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw.append(obj)
            if obj.get("event") == "CLOCK_SYNC" and obj.get("phase") == "start":
                mono0 = int(obj.get("mono_ns", obj.get("timestamp", 0)))
                wall0 = int(obj.get("wall_time_ns", 0))
    if mono0 is None or wall0 is None:
        if not raw:
            return []
        mono0 = int(raw[0].get("timestamp", 0))
        wall0 = int(datetime.now(tz=timezone.utc).timestamp() * 1e9)

    out = []
    for obj in raw:
        mono = int(obj.get("timestamp", 0))
        wall_epoch = (wall0 + (mono - mono0)) / 1e9
        ev = str(obj.get("event", "UNKNOWN"))
        st = str(obj.get("type", "")) if ev == "SUMMARY" else ""
        cnt = int(obj.get("count", 1))
        b = int(obj.get("total_bytes", 0))
        comm = str(obj.get("comm", ""))
        out.append(EbpfEvent(event=ev, summary_type=st, count=cnt, total_bytes=b, comm=comm, wall_epoch=wall_epoch))
    return out


def merge_intervals(intervals: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not intervals:
        return []
    ints = sorted(intervals, key=lambda x: (x[0], x[1]))
    merged = [ints[0]]
    for s, e in ints[1:]:
        ps, pe = merged[-1]
        if s <= pe:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def point_in_intervals(x: float, intervals: List[Tuple[float, float]]) -> bool:
    # intervals are sorted and merged
    lo = 0
    hi = len(intervals) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        s, e = intervals[mid]
        if x < s:
            hi = mid - 1
        elif x > e:
            lo = mid + 1
        else:
            return True
    return False


def compute_run_attribution(run_dir: Path) -> Dict[str, object]:
    tool_calls = load_tool_calls(run_dir / "swebench" / "tool_calls.json")
    events = load_ebpf(run_dir / "ebpf_trace.jsonl")

    phase_intervals: Dict[str, List[Tuple[float, float]]] = {p: [] for p in PHASES}
    phase_tool_time: Dict[str, float] = {p: 0.0 for p in PHASES}
    phase_tool_calls: Dict[str, int] = {p: 0 for p in PHASES}

    for tc in tool_calls:
        p = tc.phase if tc.phase in PHASES else "other"
        phase_tool_time[p] += max(tc.duration, 0.0)
        phase_tool_calls[p] += 1
        if tc.end > tc.start:
            phase_intervals[p].append((tc.start, tc.end))

    for p in PHASES:
        phase_intervals[p] = merge_intervals(phase_intervals[p])

    # exclusive assignment by priority of phases order above
    phase_event_n: Dict[str, int] = {p: 0 for p in PHASES}
    phase_summary_cnt: Dict[str, int] = {p: 0 for p in PHASES}
    phase_write_bytes: Dict[str, int] = {p: 0 for p in PHASES}
    phase_exec: Dict[str, int] = {p: 0 for p in PHASES}
    phase_comm_summary: Dict[str, Counter] = {p: Counter() for p in PHASES}
    phase_summary_type: Dict[str, Counter] = {p: Counter() for p in PHASES}

    for ev in events:
        assigned = None
        for p in PHASES:
            if point_in_intervals(ev.wall_epoch, phase_intervals[p]):
                assigned = p
                break
        if assigned is None:
            continue
        phase_event_n[assigned] += 1
        if ev.event == "EXEC":
            phase_exec[assigned] += 1
        if ev.event == "SUMMARY":
            phase_summary_cnt[assigned] += max(ev.count, 0)
            phase_write_bytes[assigned] += max(ev.total_bytes, 0) if ev.summary_type == "WRITE" else 0
            if ev.comm:
                phase_comm_summary[assigned][ev.comm] += max(ev.count, 0)
            if ev.summary_type:
                phase_summary_type[assigned][ev.summary_type] += max(ev.count, 0)

    total_tool_time = sum(phase_tool_time.values())
    total_events = sum(phase_event_n.values())
    total_write = sum(phase_write_bytes.values())

    phase_stats = {}
    for p in PHASES:
        time_share = (phase_tool_time[p] / total_tool_time) if total_tool_time > 0 else 0.0
        event_share = (phase_event_n[p] / total_events) if total_events > 0 else 0.0
        write_share = (phase_write_bytes[p] / total_write) if total_write > 0 else 0.0
        # weighted bottleneck score: time 0.5 + events 0.3 + write 0.2
        score = 0.5 * time_share + 0.3 * event_share + 0.2 * write_share
        phase_stats[p] = {
            "tool_calls": phase_tool_calls[p],
            "tool_time_s": n(phase_tool_time[p], 6),
            "time_share": n(time_share, 6),
            "event_n": phase_event_n[p],
            "event_share": n(event_share, 6),
            "summary_count": phase_summary_cnt[p],
            "write_mb": n(phase_write_bytes[p] / (1024 * 1024), 6),
            "write_share": n(write_share, 6),
            "exec_n": phase_exec[p],
            "bottleneck_score": n(score, 6),
            "top_comm_summary": phase_comm_summary[p].most_common(6),
            "top_summary_type": phase_summary_type[p].most_common(8),
        }

    return {
        "run_name": run_dir.name,
        "run_dir": str(run_dir.resolve()),
        "totals": {
            "tool_calls": len(tool_calls),
            "tool_time_s": n(total_tool_time, 6),
            "events_in_tool_windows": total_events,
            "write_mb_in_tool_windows": n(total_write / (1024 * 1024), 6),
        },
        "phase_stats": phase_stats,
    }


def aggregate_runs(run_metrics: List[Dict[str, object]]) -> Dict[str, object]:
    agg: Dict[str, Dict[str, List[float]]] = {
        p: {
            "time_share": [],
            "event_share": [],
            "write_share": [],
            "bottleneck_score": [],
            "tool_time_s": [],
            "event_n": [],
            "write_mb": [],
            "tool_calls": [],
        }
        for p in PHASES
    }
    for rm in run_metrics:
        ps = rm["phase_stats"]
        for p in PHASES:
            agg[p]["time_share"].append(float(ps[p]["time_share"]))
            agg[p]["event_share"].append(float(ps[p]["event_share"]))
            agg[p]["write_share"].append(float(ps[p]["write_share"]))
            agg[p]["bottleneck_score"].append(float(ps[p]["bottleneck_score"]))
            agg[p]["tool_time_s"].append(float(ps[p]["tool_time_s"]))
            agg[p]["event_n"].append(float(ps[p]["event_n"]))
            agg[p]["write_mb"].append(float(ps[p]["write_mb"]))
            agg[p]["tool_calls"].append(float(ps[p]["tool_calls"]))

    out = {}
    for p in PHASES:
        out[p] = {}
        for k, vals in agg[p].items():
            mu = mean(vals) if vals else 0.0
            sd = pstdev(vals) if len(vals) > 1 else 0.0
            cv = (sd / mu) if mu > 1e-12 else 0.0
            out[p][k] = {"mean": n(mu, 6), "stddev": n(sd, 6), "cv": n(cv, 6)}
    ranked = sorted(PHASES, key=lambda p: out[p]["bottleneck_score"]["mean"], reverse=True)
    return {"phase_aggregate": out, "phase_rank_by_score": ranked}


def _plot_bar(values: Dict[str, float], title: str, ylabel: str, output: Path) -> None:
    labels = list(values.keys())
    ys = [values[k] for k in labels]
    plt.figure(figsize=(10, 4.5))
    plt.bar(range(len(labels)), ys)
    plt.xticks(range(len(labels)), labels, rotation=20, ha="right")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.tight_layout()
    plt.savefig(output, dpi=150)
    plt.close()


def _plot_heatmap(run_metrics: List[Dict[str, object]], output: Path) -> None:
    if not run_metrics:
        return
    mat = []
    names = []
    for rm in run_metrics:
        names.append(rm["run_name"])
        row = [float(rm["phase_stats"][p]["bottleneck_score"]) for p in PHASES]
        mat.append(row)

    fig, ax = plt.subplots(figsize=(11, 0.8 * len(names) + 2.0))
    im = ax.imshow(mat, aspect="auto")
    ax.set_xticks(range(len(PHASES)))
    ax.set_xticklabels(PHASES, rotation=25, ha="right")
    ax.set_yticks(range(len(names)))
    ax.set_yticklabels(names)
    ax.set_title("Run-phase bottleneck score heatmap")
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("score")
    plt.tight_layout()
    plt.savefig(output, dpi=150)
    plt.close(fig)


def _fmt_table(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for i, row in enumerate(rows):
        lines.append("| " + " | ".join(row[j].ljust(widths[j]) for j in range(len(row))) + " |")
        if i == 0:
            lines.append("| " + " | ".join("-" * widths[j] for j in range(len(row))) + " |")
    return "\n".join(lines)


def build_report(run_metrics: List[Dict[str, object]], agg: Dict[str, object]) -> str:
    lines = []
    lines.append("# Bottleneck Attribution Report")
    lines.append("")
    lines.append("## Method")
    lines.append("- Phase classification from tool-call semantics (`discovery/editing/testing/build_install/vcs_revert/runtime_probe/other`).")
    lines.append("- eBPF events are aligned via `CLOCK_SYNC(start)` and attributed to phase windows.")
    lines.append("- Bottleneck score = `0.5*time_share + 0.3*event_share + 0.2*write_share`.")
    lines.append("")

    rows = [["phase", "score_mean", "score_cv", "time_share_mean", "event_share_mean", "write_share_mean"]]
    for p in agg["phase_rank_by_score"]:
        a = agg["phase_aggregate"][p]
        rows.append([
            p,
            str(a["bottleneck_score"]["mean"]),
            str(a["bottleneck_score"]["cv"]),
            str(a["time_share"]["mean"]),
            str(a["event_share"]["mean"]),
            str(a["write_share"]["mean"]),
        ])
    lines.append("## Aggregate Ranking (Across Runs)")
    lines.append(_fmt_table(rows))
    lines.append("")

    top = agg["phase_rank_by_score"][0] if agg["phase_rank_by_score"] else "n/a"
    lines.append("## Main Finding")
    lines.append(f"- Dominant bottleneck phase: `{top}` (highest mean bottleneck score).")
    lines.append("- If `testing` dominates, optimization target is test-loop efficiency and rerun policy.")
    lines.append("- If `discovery` dominates, optimization target is context retrieval/selection quality.")
    lines.append("")

    lines.append("## Per-Run Summary")
    rows2 = [["run", "top_phase", "top_score", "tool_calls", "tool_time_s", "events_in_windows", "write_mb_in_windows"]]
    for rm in run_metrics:
        ph = sorted(PHASES, key=lambda p: rm["phase_stats"][p]["bottleneck_score"], reverse=True)
        tp = ph[0] if ph else "n/a"
        rows2.append([
            rm["run_name"],
            tp,
            str(rm["phase_stats"][tp]["bottleneck_score"] if tp != "n/a" else 0),
            str(rm["totals"]["tool_calls"]),
            str(rm["totals"]["tool_time_s"]),
            str(rm["totals"]["events_in_tool_windows"]),
            str(rm["totals"]["write_mb_in_tool_windows"]),
        ])
    lines.append(_fmt_table(rows2))
    lines.append("")

    if run_metrics:
        first = run_metrics[0]
        lines.append(f"## Deep Dive: {first['run_name']}")
        for p in agg["phase_rank_by_score"][:3]:
            s = first["phase_stats"][p]
            lines.append(
                f"- `{p}`: score={s['bottleneck_score']}, time_share={s['time_share']}, "
                f"event_share={s['event_share']}, write_share={s['write_share']}, "
                f"top_summary={s['top_summary_type'][:3]}"
            )
        lines.append("")

    lines.append("## Caveats")
    lines.append("- Phase classification is rule-based; future work should use model-side action labels.")
    lines.append("- Overlapping tool calls are resolved by fixed phase priority; this can bias attribution.")
    lines.append("- `SUMMARY` is aggregated at flush interval, not raw per-syscall stream.")
    lines.append("")
    lines.append("## Figures")
    lines.append("- `plots/01_phase_time_share.png`")
    lines.append("- `plots/02_phase_event_share.png`")
    lines.append("- `plots/03_phase_write_share.png`")
    lines.append("- `plots/04_phase_bottleneck_score.png`")
    lines.append("- `plots/05_run_phase_score_heatmap.png`")
    lines.append("")
    return "\n".join(lines) + "\n"


def run_all(run_dirs: List[Path], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = output_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    run_metrics = [compute_run_attribution(rd) for rd in run_dirs]
    agg = aggregate_runs(run_metrics)

    # plot aggregate bars
    pa = agg["phase_aggregate"]
    _plot_bar({p: pa[p]["time_share"]["mean"] for p in PHASES}, "Phase time-share mean", "share", plots_dir / "01_phase_time_share.png")
    _plot_bar({p: pa[p]["event_share"]["mean"] for p in PHASES}, "Phase event-share mean", "share", plots_dir / "02_phase_event_share.png")
    _plot_bar({p: pa[p]["write_share"]["mean"] for p in PHASES}, "Phase write-share mean", "share", plots_dir / "03_phase_write_share.png")
    _plot_bar({p: pa[p]["bottleneck_score"]["mean"] for p in PHASES}, "Phase bottleneck score mean", "score", plots_dir / "04_phase_bottleneck_score.png")
    _plot_heatmap(run_metrics, plots_dir / "05_run_phase_score_heatmap.png")

    payload = {"runs": run_metrics, "aggregate": agg}
    with open(output_dir / "bottleneck_metrics.json", "w") as f:
        json.dump(payload, f, indent=2)
    report = build_report(run_metrics, agg)
    (output_dir / "bottleneck_report.md").write_text(report)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bottleneck attribution from eBPF + tool traces.")
    parser.add_argument("--run-dir", action="append", required=True, help="run_swebench_new output dir (repeat flag)")
    parser.add_argument("--output-dir", required=True, help="output directory")
    args = parser.parse_args()

    run_dirs = [Path(x).expanduser().resolve() for x in args.run_dir]
    for rd in run_dirs:
        if not rd.exists():
            raise FileNotFoundError(f"run dir not found: {rd}")

    output_dir = Path(args.output_dir).expanduser().resolve()
    run_all(run_dirs, output_dir)
    print(f"[analyze_bottleneck_attribution] report: {output_dir / 'bottleneck_report.md'}")
    print(f"[analyze_bottleneck_attribution] metrics: {output_dir / 'bottleneck_metrics.json'}")
    print(f"[analyze_bottleneck_attribution] plots: {output_dir / 'plots'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
