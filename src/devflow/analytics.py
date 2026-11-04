"""Read-only workflow analytics for the dashboard: stage latency, stalls, friction and blast radius.

Everything here is derived from `.devflow/state/traces.jsonl` (needs telemetry enabled) and the
SQLite store (opened only when it already exists). Nothing is written and nothing is cached.
"""
from __future__ import annotations

import math
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import stall, telemetry
from .store import Store, state_db_path
from .telemetry_cli import _stage_stats, _stall_counts

WINDOWS = {"all": None, "24h": 24 * 3600, "7d": 7 * 24 * 3600}
STAGES = ["Refining", "Planning", "Implementing", "Gates", "Review"]
STAGE_BUCKET = {"refining": "Refining", "test_design": "Planning", "implementing": "Implementing",
                "changes_requested": "Implementing", "gates": "Gates", "in_review": "Review"}
STALL_RATE_THRESHOLD = 0.01
LABEL_CAP = 200
TOP_FAILURES = 10


def parse_window(value: str | None) -> str:
    """'all', '24h' or '7d'; a missing or empty value means 'all'. Anything else is a ValueError."""
    if value is None or value == "":
        return "all"
    if value not in WINDOWS:
        raise ValueError("window must be one of: all, 24h, 7d")
    return value


def _finite(value: Any) -> bool:
    return stall._is_number(value) and math.isfinite(value)


def _clean(value: Any) -> Any:
    """Replace non-finite floats with None so the payload is strict JSON."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _ns(seconds: float) -> int:
    return seconds * 10**9 if isinstance(seconds, int) else round(seconds * 1e9)


def _label(value: Any) -> str:
    text = telemetry.redact(str(value))
    return text[:LABEL_CAP]


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _kpi(value: float | None, samples: int) -> dict[str, Any]:
    return {"value": value, "samples": samples}


def _stage_section(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    durations: dict[str, list[float]] = defaultdict(list)
    for record in records:
        name = record.get("name")
        if not isinstance(name, str) or not name.startswith("stage."):
            continue
        bucket = STAGE_BUCKET.get(name[len("stage."):])
        seconds = stall._seconds(record)
        if bucket and seconds is not None:
            durations[bucket].append(seconds)
    rows = []
    for stage in STAGES:
        values = durations.get(stage, [])
        if values:
            stats = _stage_stats(values)
        else:
            stats = {"count": 0, "mean": None, "max": None, "p50": None, "p90": None, "p99": None}
        rows.append({"stage": stage, **stats, "low_samples": len(values) < stall.MIN_SAMPLES})
    return rows, {s: len(durations.get(s, [])) for s in STAGES}


def _bottleneck(stages: list[dict[str, Any]]) -> str | None:
    best = None
    for row in stages:
        if row["count"] and (best is None or row["p50"] > best["p50"]):
            best = row
    return best["stage"] if best else None


def _stall_section(records: list[dict[str, Any]], span_counts: dict[str, int]) -> dict[str, Any]:
    by_stage = _stall_counts(records)
    per_bucket: Counter = Counter()
    for stage, count in by_stage.items():
        bucket = STAGE_BUCKET.get(stage[len("stage."):]) if stage.startswith("stage.") else None
        if bucket:
            per_bucket[bucket] += count
    rows = []
    for stage in STAGES:
        count, spans = per_bucket.get(stage, 0), span_counts.get(stage, 0)
        rate = round(min(1.0, count / spans), 4) if spans else None
        rows.append({"stage": stage, "count": count, "spans": spans, "rate": rate,
                     "elevated": count >= 1 and rate is not None and rate > STALL_RATE_THRESHOLD})
    return {"total": sum(by_stage.values()), "stages": rows}


def _short_circuit(records: list[dict[str, Any]]) -> tuple[int, int]:
    total = short = 0
    for record in records:
        if record.get("name") != "gate.order":
            continue
        total += 1
        short += stall._attrs(record).get("devflow.short_circuited") is True
    return short, total


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _store_metrics(root: Path, cutoff_s: float | None) -> dict[str, Any]:
    cycles: list[float] = []
    merged = first_pass = 0
    blocking = nitpick = 0
    review_tickets: set[str] = set()
    failures: Counter = Counter()
    hop1: list[int] = []
    covered = impacted = 0
    db = state_db_path(root)
    if db.exists():
        with Store(db) as store:
            for ticket in store.list_tickets():
                counts = dict.fromkeys(("merged", "first_pass", "blocking", "nitpick", "covered",
                                        "impacted"), 0)
                try:
                    _ticket_metrics(store, ticket.id, cutoff_s, cycles, failures, hop1, review_tickets,
                                    counts)
                except Exception:  # noqa: BLE001, S112 - one bad ticket must not hide the rest
                    continue
                merged += counts["merged"]
                first_pass += counts["first_pass"]
                blocking += counts["blocking"]
                nitpick += counts["nitpick"]
                covered += counts["covered"]
                impacted += counts["impacted"]
    top = sorted(failures.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_FAILURES]
    return {
        "cycle": cycles, "merged": merged, "first_pass": first_pass,
        "review": {"blocking": blocking, "nitpick": nitpick, "tickets": len(review_tickets)},
        "gate_failures": [{"label": k, "count": v} for k, v in top],
        "blast": {"tickets_with_impact": len(hop1),
                  "avg_hop1_callers": round(sum(hop1) / len(hop1), 3) if hop1 else None,
                  "coverage_ratio": _ratio(covered, impacted) if hop1 else None,
                  "impacted_symbols": impacted},
    }


def _in_store_window(row: dict[str, Any], cutoff_s: float | None) -> bool:
    if cutoff_s is None:
        return True
    ts = row.get("ts")
    return _finite(ts) and ts >= cutoff_s


def _ticket_metrics(store: Store, ticket_id: str, cutoff_s: float | None, cycles: list[float],
                    failures: Counter, hop1: list[int], review_tickets: set[str],
                    counts: dict[str, int]) -> None:
    moves = [e for e in store.events(ticket_id, "transition") if _finite(e.get("ts"))]
    merged_at = next((e["ts"] for e in moves if e.get("to") == "merged"), None)
    if merged_at is not None and (cutoff_s is None or merged_at >= cutoff_s):
        counts["merged"] = 1
        counts["first_pass"] = int(not any(e.get("to") == "changes_requested" for e in moves))
        cycle = merged_at - moves[0]["ts"]
        if cycle >= 0:
            cycles.append(cycle)
    reviews = [r for r in store.stage_results(ticket_id, "review") if _in_store_window(r, cutoff_s)]
    for row in reviews:
        findings = _dicts((row["payload"] or {}).get("findings"))
        review_tickets.add(ticket_id)
        for finding in findings:
            counts["blocking" if finding.get("blocking") else "nitpick"] += 1
    gates = [r for r in store.stage_results(ticket_id, "gates") if _in_store_window(r, cutoff_s)]
    for row in gates:
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        labels = [str(n) for f in _dicts(payload.get("failures")) for n in (f.get("new") or [])
                  if isinstance(n, str)]
        if not labels and not row.get("ok"):
            labels = [str(r.get("command")) for r in _dicts(payload.get("results"))
                      if r.get("command") and (r.get("ok") is False or r.get("timed_out"))]
        for label in labels:
            failures[_label(label)] += 1
    impact = next((p["impact"] for p in (r["payload"] for r in reversed(gates))
                   if isinstance(p, dict) and isinstance(p.get("impact"), dict) and p["impact"]), None)
    if impact is None:
        impact = next((p["impact"] for p in (r["payload"] for r in reversed(reviews))
                       if isinstance(p, dict) and isinstance(p.get("impact"), dict) and p["impact"]), None)
    if impact is not None:
        callers = impact.get("callers")
        hop1.append(len(callers.get("1", [])) if isinstance(callers, dict)
                    and isinstance(callers.get("1"), list) else 0)
        uncovered = {u for u in (impact.get("uncovered") or []) if isinstance(u, str)} \
            if isinstance(impact.get("uncovered"), list) else set()
        symbols = _dicts(impact.get("symbols"))
        counts["impacted"] = len(symbols)
        counts["covered"] = sum(1 for s in symbols if s.get("id") not in uncovered)


def collect_analytics(root: Path, window: str = "all", now: float | None = None) -> dict[str, Any]:
    """Strict-JSON analytics for one window; raises ValueError for an unknown window."""
    window = parse_window(window)
    now = time.time() if now is None else now
    seconds = WINDOWS[window]
    cutoff_ns = None if seconds is None else _ns(now) - _ns(seconds)
    cutoff_s = None if seconds is None else now - seconds

    all_records = [r for r in stall.read_log_records(root / ".devflow" / "state" / "traces.jsonl")
                   if not stall._is_synthetic(r)]
    if cutoff_ns is None:
        records = all_records
    else:
        records = [r for r in all_records if _finite(r.get("end_ns")) and r["end_ns"] >= cutoff_ns]

    stages, span_counts = _stage_section(records)
    stalls = _stall_section(records, span_counts)
    short, orders = _short_circuit(records)
    store = _store_metrics(root, cutoff_s)
    cycles = store["cycle"]
    payload = {
        "window": window,
        "generated_at": now,
        "telemetry": {"available": bool(all_records), "spans": len(records)},
        "kpis": {
            "median_cycle_seconds": _kpi(round(statistics.median(cycles), 3) if cycles else None,
                                         len(cycles)),
            "first_pass_approval_rate": _kpi(_ratio(store["first_pass"], store["merged"]),
                                             store["merged"]),
            "blast_short_circuit_rate": _kpi(_ratio(short, orders), orders),
            "total_stalls": _kpi(stalls["total"] if all_records else None, stalls["total"]),
        },
        "stages": stages,
        "bottleneck": _bottleneck(stages),
        "stalls": stalls,
        "friction": {"review": store["review"], "gate_failures": store["gate_failures"]},
        "blast": store["blast"],
    }
    return _clean(payload)
