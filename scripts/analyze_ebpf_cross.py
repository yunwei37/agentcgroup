#!/usr/bin/env python3
"""
Cross-analysis for run_swebench_new outputs.

Input run directory layout:
  <run_dir>/
    ebpf_trace.jsonl
    run_manifest.json
    swebench/
      tool_calls.json
      resources.json
      results.json

Outputs:
  <output_dir>/
    report.md
    run_metrics.json
    plots/
      01_event_type_counts.png
      02_summary_type_counts.png
      03_summary_type_bytes.png
      04_timeline_events_tools.png
      05_timeline_resources_vs_events.png
      06_tool_cross_metrics.png
      07_process_contribution.png
      08_path_hotspots.png
      09_run_comparison.png   (only when >=2 runs)
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Dict, Iterable, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def parse_iso8601(ts: str) -> float:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).timestamp()


def safe_float(x: object, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def parse_mem_to_mb(text: str) -> float:
    # examples: "573.4kB / 4.295GB", "297.1MB / 4.295GB"
    if not text:
        return 0.0
    left = text.split("/")[0].strip()
    num = ""
    unit = ""
    for ch in left:
        if ch.isdigit() or ch == ".":
            num += ch
        else:
            unit += ch
    value = safe_float(num, 0.0)
    unit = unit.strip().lower()
    if unit == "kb":
        return value / 1024.0
    if unit == "mb":
        return value
    if unit == "gb":
        return value * 1024.0
    return value


def nsmall(x: float, digits: int = 3) -> float:
    if math.isnan(x) or math.isinf(x):
        return 0.0
    return round(x, digits)


def pearson_corr(xs: List[float], ys: List[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mx = mean(xs)
    my = mean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / math.sqrt(vx * vy)


def floor_second(epoch: float) -> int:
    return int(math.floor(epoch))


def top_items(counter: Counter, n: int = 10) -> List[Tuple[str, float]]:
    return counter.most_common(n)


def truncate_label(s: str, max_len: int = 42) -> str:
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def path_prefix(path: str, depth: int = 2) -> str:
    if not path:
        return ""
    p = path.strip()
    if not p.startswith("/"):
        return p
    parts = [x for x in p.split("/") if x]
    if not parts:
        return "/"
    return "/" + "/".join(parts[: min(depth, len(parts))])


def extract_path_from_summary(ev: Dict[str, object]) -> Optional[str]:
    t = ev.get("type")
    detail = str(ev.get("detail", ""))
    extra = str(ev.get("extra", ""))
    if t in {"DIR_CREATE", "FILE_DELETE", "FILE_RENAME"}:
        if detail.startswith("/"):
            return detail
        if extra.startswith("/"):
            return extra
    if t == "WRITE" and ev.get("path_resolved") and detail.startswith("/"):
        return detail
    return None


@dataclass
class ToolCall:
    tool: str
    start: float
    end: float
    duration: float


@dataclass
class ResourceSample:
    epoch: float
    cpu: float
    mem_mb: float


@dataclass
class EbpfEvent:
    event: str
    summary_type: str
    count: int
    total_bytes: int
    comm: str
    pid: int
    mono_ns: int
    wall_epoch: float
    path: str


@dataclass
class RunData:
    run_dir: Path
    run_name: str
    tool_calls: List[ToolCall]
    resources: List[ResourceSample]
    events: List[EbpfEvent]
    manifest: Dict[str, object]
    results: Dict[str, object]


def load_tool_calls(path: Path) -> List[ToolCall]:
    if not path.exists():
        return []
    with open(path, "r") as f:
        raw = json.load(f)
    calls: List[ToolCall] = []
    for item in raw:
        start = parse_iso8601(item["timestamp"])
        end = parse_iso8601(item.get("end_timestamp", item["timestamp"]))
        if end < start:
            end = start
        calls.append(ToolCall(tool=item.get("tool", "UNKNOWN"), start=start, end=end, duration=end - start))
    return calls


def load_resources(path: Path) -> List[ResourceSample]:
    if not path.exists():
        return []
    with open(path, "r") as f:
        data = json.load(f)
    samples: List[ResourceSample] = []
    for s in data.get("samples", []):
        samples.append(
            ResourceSample(
                epoch=safe_float(s.get("epoch"), 0.0),
                cpu=safe_float(str(s.get("cpu_percent", "0")).strip("%"), 0.0),
                mem_mb=parse_mem_to_mb(str(s.get("mem_usage", ""))),
            )
        )
    return samples


def load_json(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    with open(path, "r") as f:
        return json.load(f)


def load_ebpf(path: Path) -> List[EbpfEvent]:
    if not path.exists():
        return []

    raw: List[Dict[str, object]] = []
    mono0 = None
    wall0_ns = None
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
                wall0_ns = int(obj.get("wall_time_ns", 0))

    if mono0 is None or wall0_ns is None:
        # fallback: use first event as anchor with pseudo wall-clock
        if not raw:
            return []
        mono0 = int(raw[0].get("timestamp", 0))
        wall0_ns = int(datetime.now(tz=timezone.utc).timestamp() * 1e9)

    events: List[EbpfEvent] = []
    for obj in raw:
        mono_ns = int(obj.get("timestamp", 0))
        wall_epoch = (wall0_ns + (mono_ns - mono0)) / 1e9
        event = str(obj.get("event", "UNKNOWN"))
        summary_type = str(obj.get("type", "")) if event == "SUMMARY" else ""
        count = int(obj.get("count", 1))
        total_bytes = int(obj.get("total_bytes", 0))
        comm = str(obj.get("comm", ""))
        pid = int(obj.get("pid", 0))
        p = ""
        if event == "FILE_OPEN":
            p = str(obj.get("filepath", ""))
        elif event == "SUMMARY":
            ep = extract_path_from_summary(obj)
            p = ep or ""

        events.append(
            EbpfEvent(
                event=event,
                summary_type=summary_type,
                count=count,
                total_bytes=total_bytes,
                comm=comm,
                pid=pid,
                mono_ns=mono_ns,
                wall_epoch=wall_epoch,
                path=p,
            )
        )
    return events


def load_run(run_dir: Path) -> RunData:
    sw = run_dir / "swebench"
    return RunData(
        run_dir=run_dir,
        run_name=run_dir.name,
        tool_calls=load_tool_calls(sw / "tool_calls.json"),
        resources=load_resources(sw / "resources.json"),
        events=load_ebpf(run_dir / "ebpf_trace.jsonl"),
        manifest=load_json(run_dir / "run_manifest.json"),
        results=load_json(sw / "results.json"),
    )


def build_second_buckets(events: List[EbpfEvent]) -> Dict[int, Dict[str, float]]:
    per_sec: Dict[int, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in events:
        sec = floor_second(e.wall_epoch)
        per_sec[sec]["events"] += 1
        if e.event == "SUMMARY":
            per_sec[sec]["summary_count"] += max(e.count, 0)
            per_sec[sec]["summary_bytes"] += max(e.total_bytes, 0)
            if e.summary_type == "WRITE":
                per_sec[sec]["write_bytes"] += max(e.total_bytes, 0)
                per_sec[sec]["write_count"] += max(e.count, 0)
        if e.event == "FILE_OPEN":
            per_sec[sec]["file_open"] += 1
        if e.event == "EXEC":
            per_sec[sec]["exec"] += 1
        if e.event == "EXIT":
            per_sec[sec]["exit"] += 1
    return per_sec


def build_tool_second_buckets(calls: List[ToolCall]) -> Dict[int, Dict[str, float]]:
    per_sec: Dict[int, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for c in calls:
        s0 = floor_second(c.start)
        s1 = floor_second(c.end)
        for sec in range(s0, s1 + 1):
            left = max(c.start, sec)
            right = min(c.end, sec + 1)
            overlap = max(0.0, right - left)
            if overlap <= 0:
                continue
            per_sec[sec]["active_calls"] += overlap
            per_sec[sec][f"tool_{c.tool}"] += overlap
    return per_sec


def calc_tool_cross(calls: List[ToolCall], events: List[EbpfEvent]) -> Dict[str, Dict[str, float]]:
    by_tool: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    if not calls or not events:
        return by_tool

    for c in calls:
        dur = max(c.duration, 1e-6)
        ev_n = 0
        summary_cnt = 0
        write_bytes = 0
        for e in events:
            if e.wall_epoch < c.start or e.wall_epoch > c.end:
                continue
            ev_n += 1
            if e.event == "SUMMARY":
                summary_cnt += max(e.count, 0)
                write_bytes += max(e.total_bytes, 0) if e.summary_type == "WRITE" else 0
        item = by_tool[c.tool]
        item["calls"] += 1
        item["duration_s"] += dur
        item["events_in_window"] += ev_n
        item["summary_count_in_window"] += summary_cnt
        item["write_bytes_in_window"] += write_bytes

    for tool, item in by_tool.items():
        dur = max(item["duration_s"], 1e-6)
        item["event_rate_per_s"] = item["events_in_window"] / dur
        item["summary_rate_per_s"] = item["summary_count_in_window"] / dur
        item["write_mb_per_s"] = item["write_bytes_in_window"] / dur / (1024 * 1024)
        item["avg_call_duration_s"] = item["duration_s"] / max(item["calls"], 1.0)
    return by_tool


def compute_run_metrics(run: RunData) -> Dict[str, object]:
    events = run.events
    calls = run.tool_calls
    resources = run.resources

    if events:
        start_epoch = min(e.wall_epoch for e in events)
        end_epoch = max(e.wall_epoch for e in events)
    else:
        start_epoch = 0.0
        end_epoch = 0.0
    duration = max(0.0, end_epoch - start_epoch)

    event_type_counter: Counter = Counter()
    summary_type_counter: Counter = Counter()
    summary_type_bytes: Counter = Counter()
    comm_counter: Counter = Counter()
    comm_summary_counter: Counter = Counter()
    comm_summary_bytes: Counter = Counter()
    path_counter: Counter = Counter()

    for e in events:
        event_type_counter[e.event] += 1
        if e.comm:
            comm_counter[e.comm] += 1
        if e.path:
            path_counter[path_prefix(e.path, depth=2)] += 1
        if e.event == "SUMMARY":
            key = e.summary_type or "UNKNOWN"
            summary_type_counter[key] += max(e.count, 0)
            summary_type_bytes[key] += max(e.total_bytes, 0)
            if e.comm:
                comm_summary_counter[e.comm] += max(e.count, 0)
                comm_summary_bytes[e.comm] += max(e.total_bytes, 0)

    tool_counter: Counter = Counter(c.tool for c in calls)
    total_tool_duration = sum(c.duration for c in calls)

    res_cpu = [r.cpu for r in resources]
    res_mem = [r.mem_mb for r in resources]

    per_sec_events = build_second_buckets(events)
    per_sec_tools = build_tool_second_buckets(calls)

    # resources vs events correlation
    if resources and per_sec_events:
        event_series = {sec: vals["events"] for sec, vals in per_sec_events.items()}
        overlap_secs = sorted(set(event_series.keys()) & {floor_second(r.epoch) for r in resources})
        cpu_x = []
        ev_y = []
        mem_x = []
        for sec in overlap_secs:
            sec_cpu = [r.cpu for r in resources if floor_second(r.epoch) == sec]
            sec_mem = [r.mem_mb for r in resources if floor_second(r.epoch) == sec]
            if not sec_cpu or not sec_mem:
                continue
            cpu_x.append(mean(sec_cpu))
            mem_x.append(mean(sec_mem))
            ev_y.append(event_series.get(sec, 0.0))
        cpu_event_corr = pearson_corr(cpu_x, ev_y)
        mem_event_corr = pearson_corr(mem_x, ev_y)
    else:
        cpu_event_corr = 0.0
        mem_event_corr = 0.0

    tool_cross = calc_tool_cross(calls, events)

    run_metrics: Dict[str, object] = {
        "run_name": run.run_name,
        "run_dir": str(run.run_dir),
        "duration_s": duration,
        "event_count_total": sum(event_type_counter.values()),
        "summary_count_total": int(sum(summary_type_counter.values())),
        "summary_write_bytes_total": int(summary_type_bytes.get("WRITE", 0)),
        "summary_write_mb_total": nsmall(summary_type_bytes.get("WRITE", 0) / (1024 * 1024), 3),
        "tool_calls_total": len(calls),
        "tool_time_total_s": nsmall(total_tool_duration, 3),
        "cpu_avg_percent": nsmall(mean(res_cpu), 3) if res_cpu else 0.0,
        "cpu_max_percent": nsmall(max(res_cpu), 3) if res_cpu else 0.0,
        "mem_avg_mb": nsmall(mean(res_mem), 3) if res_mem else 0.0,
        "mem_max_mb": nsmall(max(res_mem), 3) if res_mem else 0.0,
        "cpu_event_corr": nsmall(cpu_event_corr, 4),
        "mem_event_corr": nsmall(mem_event_corr, 4),
        "event_type_counts": dict(event_type_counter),
        "summary_type_counts": dict(summary_type_counter),
        "summary_type_bytes": dict(summary_type_bytes),
        "tool_counts": dict(tool_counter),
        "top_comm_events": top_items(comm_counter, 12),
        "top_comm_summary_count": top_items(comm_summary_counter, 12),
        "top_comm_summary_bytes": top_items(comm_summary_bytes, 12),
        "top_path_prefixes": top_items(path_counter, 15),
        "tool_cross": {k: {kk: nsmall(vv, 6) for kk, vv in v.items()} for k, v in tool_cross.items()},
        "per_second_events": {str(k): {kk: nsmall(vv, 6) for kk, vv in vals.items()} for k, vals in per_sec_events.items()},
        "per_second_tools": {str(k): {kk: nsmall(vv, 6) for kk, vv in vals.items()} for k, vals in per_sec_tools.items()},
    }
    return run_metrics


def _plot_bar(counter_like: Dict[str, float], output: Path, title: str, ylabel: str, topn: int = 12) -> None:
    if not counter_like:
        return
    items = sorted(counter_like.items(), key=lambda x: x[1], reverse=True)[:topn]
    labels = [truncate_label(k, 45) for k, _ in items]
    values = [v for _, v in items]
    plt.figure(figsize=(11, 5))
    plt.bar(range(len(values)), values)
    plt.xticks(range(len(values)), labels, rotation=30, ha="right")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.tight_layout()
    plt.savefig(output, dpi=150)
    plt.close()


def _plot_timeline(run: RunData, metrics: Dict[str, object], output1: Path, output2: Path) -> None:
    per_sec_events = {int(k): v for k, v in metrics.get("per_second_events", {}).items()}
    per_sec_tools = {int(k): v for k, v in metrics.get("per_second_tools", {}).items()}
    if not per_sec_events:
        return
    secs = sorted(per_sec_events.keys())
    rel = [s - secs[0] for s in secs]
    ev = [per_sec_events[s].get("events", 0.0) for s in secs]
    fo = [per_sec_events[s].get("file_open", 0.0) for s in secs]
    ex = [per_sec_events[s].get("exec", 0.0) for s in secs]
    sy = [per_sec_events[s].get("summary_count", 0.0) for s in secs]
    wb = [per_sec_events[s].get("write_bytes", 0.0) / (1024 * 1024) for s in secs]
    act = [per_sec_tools.get(s, {}).get("active_calls", 0.0) for s in secs]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 7), sharex=True)
    ax1.plot(rel, ev, label="eBPF events/s", linewidth=1.5)
    ax1.plot(rel, fo, label="FILE_OPEN/s", linewidth=1.2)
    ax1.plot(rel, ex, label="EXEC/s", linewidth=1.2)
    ax1.plot(rel, sy, label="SUMMARY.count/s", linewidth=1.2)
    ax1.set_ylabel("count per second")
    ax1.legend(loc="upper right")
    ax1.grid(alpha=0.25)

    ax2.bar(rel, wb, label="WRITE MB/s", alpha=0.7)
    ax2.plot(rel, act, label="active tool-call seconds", linewidth=1.3)
    ax2.set_ylabel("MB/s or active-sec")
    ax2.set_xlabel("seconds from trace start")
    ax2.legend(loc="upper right")
    ax2.grid(alpha=0.25)
    fig.suptitle(f"Timeline cross-view: {run.run_name}")
    fig.tight_layout()
    fig.savefig(output1, dpi=150)
    plt.close(fig)

    if run.resources:
        r_secs = sorted({floor_second(r.epoch) for r in run.resources})
        min_sec = min(secs[0], min(r_secs))
        max_sec = max(secs[-1], max(r_secs))
        xs = list(range(min_sec, max_sec + 1))
        xrel = [x - min_sec for x in xs]
        ev_s = [per_sec_events.get(x, {}).get("events", 0.0) for x in xs]
        cpu_s = []
        mem_s = []
        for sec in xs:
            curr = [r for r in run.resources if floor_second(r.epoch) == sec]
            if curr:
                cpu_s.append(mean(r.cpu for r in curr))
                mem_s.append(mean(r.mem_mb for r in curr))
            else:
                cpu_s.append(0.0)
                mem_s.append(0.0)

        fig, ax1 = plt.subplots(figsize=(13, 4.5))
        ax1.plot(xrel, ev_s, label="eBPF events/s", linewidth=1.5)
        ax1.set_ylabel("eBPF events/s")
        ax1.set_xlabel("seconds (aligned)")
        ax1.grid(alpha=0.25)
        ax2 = ax1.twinx()
        ax2.plot(xrel, cpu_s, label="CPU%", linewidth=1.2, color="tab:orange")
        ax2.plot(xrel, mem_s, label="Mem MB", linewidth=1.2, color="tab:green")
        ax2.set_ylabel("CPU% / Mem MB")
        lines = ax1.get_lines() + ax2.get_lines()
        labels = [l.get_label() for l in lines]
        ax1.legend(lines, labels, loc="upper right")
        fig.suptitle(f"Resources vs eBPF events: {run.run_name}")
        fig.tight_layout()
        fig.savefig(output2, dpi=150)
        plt.close(fig)


def _plot_tool_cross(metrics: Dict[str, object], output: Path) -> None:
    cross: Dict[str, Dict[str, float]] = metrics.get("tool_cross", {})
    if not cross:
        return
    tools = sorted(cross.keys(), key=lambda t: cross[t].get("events_in_window", 0), reverse=True)
    rates = [cross[t].get("event_rate_per_s", 0.0) for t in tools]
    wmb = [cross[t].get("write_mb_per_s", 0.0) for t in tools]
    calls = [cross[t].get("calls", 0.0) for t in tools]

    fig, ax1 = plt.subplots(figsize=(11, 5))
    x = range(len(tools))
    ax1.bar(x, rates, alpha=0.7, label="event_rate_per_s")
    ax1.plot(x, calls, marker="o", linewidth=1.2, label="calls")
    ax1.set_xticks(list(x))
    ax1.set_xticklabels(tools, rotation=20, ha="right")
    ax1.set_ylabel("events/s or calls")
    ax2 = ax1.twinx()
    ax2.plot(x, wmb, marker="s", linewidth=1.2, color="tab:red", label="write_mb_per_s")
    ax2.set_ylabel("write MB/s")

    lines = ax1.get_lines() + ax2.get_lines()
    bars = [ax1.patches[0]] if ax1.patches else []
    labels = [l.get_label() for l in lines] + ["event_rate_per_s"]
    handles = lines + bars
    if handles:
        ax1.legend(handles, labels, loc="upper right")
    ax1.grid(alpha=0.25)
    fig.suptitle("Cross: tool windows vs eBPF activity")
    fig.tight_layout()
    fig.savefig(output, dpi=150)
    plt.close(fig)


def _plot_process_contrib(metrics: Dict[str, object], output: Path) -> None:
    top_cnt = metrics.get("top_comm_summary_count", [])[:10]
    top_bytes = metrics.get("top_comm_summary_bytes", [])[:10]
    if not top_cnt and not top_bytes:
        return
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 4.5))
    if top_cnt:
        labels = [truncate_label(x[0], 24) for x in top_cnt]
        vals = [x[1] for x in top_cnt]
        ax1.barh(labels[::-1], vals[::-1])
        ax1.set_title("SUMMARY count by comm")
        ax1.set_xlabel("count")
    if top_bytes:
        labels = [truncate_label(x[0], 24) for x in top_bytes]
        vals = [x[1] / (1024 * 1024) for x in top_bytes]
        ax2.barh(labels[::-1], vals[::-1], color="tab:orange")
        ax2.set_title("SUMMARY bytes by comm")
        ax2.set_xlabel("MB")
    fig.suptitle("Process contribution")
    fig.tight_layout()
    fig.savefig(output, dpi=150)
    plt.close(fig)


def _plot_path_hotspots(metrics: Dict[str, object], output: Path) -> None:
    paths = metrics.get("top_path_prefixes", [])
    if not paths:
        return
    labels = [truncate_label(str(k), 35) for k, _ in paths[:12]]
    vals = [v for _, v in paths[:12]]
    plt.figure(figsize=(11, 5))
    plt.bar(range(len(vals)), vals)
    plt.xticks(range(len(vals)), labels, rotation=25, ha="right")
    plt.ylabel("event mentions")
    plt.title("Path hotspots (FILE_OPEN + path-like SUMMARY)")
    plt.tight_layout()
    plt.savefig(output, dpi=150)
    plt.close()


def _plot_run_comparison(all_metrics: List[Dict[str, object]], output: Path) -> None:
    if len(all_metrics) < 2:
        return
    names = [m["run_name"] for m in all_metrics]
    duration = [m["duration_s"] for m in all_metrics]
    events = [m["event_count_total"] for m in all_metrics]
    writes = [m["summary_write_mb_total"] for m in all_metrics]
    tools = [m["tool_calls_total"] for m in all_metrics]
    cpu = [m["cpu_avg_percent"] for m in all_metrics]

    fig, axes = plt.subplots(3, 2, figsize=(13, 9))
    plots = [
        (duration, "duration_s"),
        (events, "event_count_total"),
        (writes, "summary_write_mb_total"),
        (tools, "tool_calls_total"),
        (cpu, "cpu_avg_percent"),
    ]
    for i, (vals, title) in enumerate(plots):
        r = i // 2
        c = i % 2
        ax = axes[r][c]
        ax.bar(range(len(vals)), vals)
        ax.set_title(title)
        ax.set_xticks(range(len(vals)))
        ax.set_xticklabels([truncate_label(n, 18) for n in names], rotation=20, ha="right")
        ax.grid(alpha=0.25)
    axes[2][1].axis("off")
    fig.suptitle("Cross-run comparison")
    fig.tight_layout()
    fig.savefig(output, dpi=150)
    plt.close(fig)


def _fmt_table(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for idx, row in enumerate(rows):
        line = "| " + " | ".join(row[i].ljust(widths[i]) for i in range(len(row))) + " |"
        out.append(line)
        if idx == 0:
            out.append("| " + " | ".join("-" * widths[i] for i in range(len(row))) + " |")
    return "\n".join(out)


def build_report(all_metrics: List[Dict[str, object]], out_dir: Path) -> str:
    lines: List[str] = []
    lines.append("# eBPF Cross Analysis Report")
    lines.append("")
    lines.append("## Data Source Clarification")
    lines.append("- `resource_plot.png` uses `resources.json + tool_calls.json` (container CPU/memory + tool timeline), not raw eBPF events.")
    lines.append("- This report uses `ebpf_trace.jsonl` as the primary source and cross-checks with `tool_calls.json` and `resources.json`.")
    lines.append("")

    rows = [["run_name", "duration_s", "event_total", "summary_total", "write_mb", "tool_calls", "cpu_avg%", "mem_avg_mb"]]
    for m in all_metrics:
        rows.append(
            [
                str(m["run_name"]),
                str(nsmall(float(m["duration_s"]), 2)),
                str(m["event_count_total"]),
                str(m["summary_count_total"]),
                str(nsmall(float(m["summary_write_mb_total"]), 2)),
                str(m["tool_calls_total"]),
                str(nsmall(float(m["cpu_avg_percent"]), 2)),
                str(nsmall(float(m["mem_avg_mb"]), 2)),
            ]
        )
    lines.append("## Run Overview")
    lines.append(_fmt_table(rows))
    lines.append("")

    first = all_metrics[0]
    lines.append(f"## Deep Dive: {first['run_name']}")
    lines.append(
        f"- Total eBPF events: `{first['event_count_total']}`; SUMMARY aggregate count: `{first['summary_count_total']}`; "
        f"WRITE bytes: `{first['summary_write_mb_total']} MB`."
    )
    lines.append(
        f"- Resource alignment: corr(events/s, CPU%)=`{first['cpu_event_corr']}`, "
        f"corr(events/s, MemMB)=`{first['mem_event_corr']}`."
    )
    lines.append("")
    lines.append("### Top Event/Summary Types")
    top_ev = sorted(first.get("event_type_counts", {}).items(), key=lambda x: x[1], reverse=True)[:8]
    top_sy = sorted(first.get("summary_type_counts", {}).items(), key=lambda x: x[1], reverse=True)[:8]
    lines.append("- Event types: " + ", ".join([f"{k}={v}" for k, v in top_ev]))
    lines.append("- Summary types: " + ", ".join([f"{k}={v}" for k, v in top_sy]))
    lines.append("")
    lines.append("### Tool-Window Cross Metrics")
    cross = first.get("tool_cross", {})
    cross_items = sorted(cross.items(), key=lambda kv: kv[1].get("events_in_window", 0), reverse=True)
    if cross_items:
        lines.append(_fmt_table([
            ["tool", "calls", "duration_s", "events_in_window", "event_rate/s", "write_mb/s"],
            *[
                [
                    t,
                    str(int(v.get("calls", 0))),
                    str(nsmall(v.get("duration_s", 0.0), 3)),
                    str(int(v.get("events_in_window", 0))),
                    str(nsmall(v.get("event_rate_per_s", 0.0), 3)),
                    str(nsmall(v.get("write_mb_per_s", 0.0), 4)),
                ]
                for t, v in cross_items[:10]
            ],
        ]))
    else:
        lines.append("- no tool cross data")
    lines.append("")

    lines.append("### Process/Path Hotspots")
    top_comm = first.get("top_comm_summary_count", [])[:8]
    top_paths = first.get("top_path_prefixes", [])[:8]
    lines.append("- Top comm by SUMMARY count: " + ", ".join([f"{k}={v}" for k, v in top_comm]) if top_comm else "- no summary comm data")
    lines.append("- Top path prefixes: " + ", ".join([f"{k}={v}" for k, v in top_paths]) if top_paths else "- no path prefix data")
    lines.append("")

    if len(all_metrics) > 1:
        lines.append("## Cross-Run Stability")
        metric_keys = [
            ("duration_s", "duration_s"),
            ("event_count_total", "event_count_total"),
            ("summary_count_total", "summary_count_total"),
            ("summary_write_mb_total", "summary_write_mb_total"),
            ("tool_calls_total", "tool_calls_total"),
        ]
        rows2 = [["metric", "mean", "median", "stddev", "cv"]]
        for key, label in metric_keys:
            vals = [safe_float(m.get(key, 0.0), 0.0) for m in all_metrics]
            mu = mean(vals)
            sd = pstdev(vals) if len(vals) > 1 else 0.0
            cv = (sd / mu) if mu > 1e-9 else 0.0
            rows2.append([label, str(nsmall(mu, 4)), str(nsmall(median(vals), 4)), str(nsmall(sd, 4)), str(nsmall(cv, 4))])
        lines.append(_fmt_table(rows2))
        lines.append("")
        lines.append("- Interpretation: lower CV means better repeatability under same setup.")
        lines.append("")

    lines.append("## Figures")
    lines.append("- `plots/01_event_type_counts.png`")
    lines.append("- `plots/02_summary_type_counts.png`")
    lines.append("- `plots/03_summary_type_bytes.png`")
    lines.append("- `plots/04_timeline_events_tools.png`")
    lines.append("- `plots/05_timeline_resources_vs_events.png`")
    lines.append("- `plots/06_tool_cross_metrics.png`")
    lines.append("- `plots/07_process_contribution.png`")
    lines.append("- `plots/08_path_hotspots.png`")
    if len(all_metrics) > 1:
        lines.append("- `plots/09_run_comparison.png`")
    lines.append("")

    lines.append("## Remaining Gaps")
    lines.append("- WRITE path resolution still depends on fd/path mapping quality; unresolved fd writes remain.")
    lines.append("- SUMMARY is periodic aggregate, not every syscall event; short bursts may be merged.")
    lines.append("- Strict causal mapping from a single tool call to exact syscall sequence is approximate when calls overlap.")
    lines.append("- No automatic semantic phase labels yet (setup/edit/test/fix) for higher-level interpretation.")
    lines.append("")
    return "\n".join(lines) + "\n"


def save_json(path: Path, obj: object) -> None:
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def run_analysis(run_dirs: List[Path], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = output_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    runs = [load_run(rd) for rd in run_dirs]
    all_metrics = [compute_run_metrics(r) for r in runs]

    # Deep plots for first run.
    first = all_metrics[0]
    _plot_bar(first.get("event_type_counts", {}), plots_dir / "01_event_type_counts.png", "Event type counts", "events")
    _plot_bar(first.get("summary_type_counts", {}), plots_dir / "02_summary_type_counts.png", "SUMMARY type counts (aggregated count)", "summary count")
    # bytes in MB for summary types
    syb = {k: v / (1024 * 1024) for k, v in first.get("summary_type_bytes", {}).items()}
    _plot_bar(syb, plots_dir / "03_summary_type_bytes.png", "SUMMARY type bytes", "MB")
    _plot_timeline(runs[0], first, plots_dir / "04_timeline_events_tools.png", plots_dir / "05_timeline_resources_vs_events.png")
    _plot_tool_cross(first, plots_dir / "06_tool_cross_metrics.png")
    _plot_process_contrib(first, plots_dir / "07_process_contribution.png")
    _plot_path_hotspots(first, plots_dir / "08_path_hotspots.png")
    _plot_run_comparison(all_metrics, plots_dir / "09_run_comparison.png")

    report = build_report(all_metrics, output_dir)
    (output_dir / "report.md").write_text(report)
    save_json(output_dir / "run_metrics.json", all_metrics)


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross analyze eBPF trace with tool/resource traces.")
    parser.add_argument(
        "--run-dir",
        action="append",
        required=True,
        help="run_swebench_new output directory (repeat this flag for multiple runs)",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="output directory for report and plots",
    )
    args = parser.parse_args()

    run_dirs = [Path(x).expanduser().resolve() for x in args.run_dir]
    for rd in run_dirs:
        if not rd.exists():
            raise FileNotFoundError(f"run dir not found: {rd}")

    out = Path(args.output_dir).expanduser().resolve()
    run_analysis(run_dirs, out)
    print(f"[analyze_ebpf_cross] report: {out / 'report.md'}")
    print(f"[analyze_ebpf_cross] metrics: {out / 'run_metrics.json'}")
    print(f"[analyze_ebpf_cross] plots: {out / 'plots'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
