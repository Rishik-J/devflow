import json
import math
import sqlite3
from pathlib import Path

import pytest

from devflow import analytics
from devflow.analytics import collect_analytics, parse_window
from devflow.stall import MIN_SAMPLES
from devflow.states import State, Ticket
from devflow.store import Store, state_db_path

S = 1_000_000_000
NOW = 10_000_000  # seconds


def span(name, start=0, end=1, status="ok", synthetic=False, **attrs):
    attributes = dict(attrs)
    if synthetic:
        attributes["langfuse.trace.metadata.synthetic"] = "true"
    return {"name": name, "trace_id": "t", "start_ns": start * S, "end_ns": end * S,
            "status": status, "attributes": attributes}


def stage(state, dur, end=NOW):
    return span("stage." + state, end - dur, end)


def write_traces(root: Path, records, raw=()):
    log = root / ".devflow/state/traces.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join([json.dumps(r) for r in records] + list(raw)) + "\n")
    return log


def collect(root, window="all", now=NOW):
    return collect_analytics(root, window, now=now)


def by_stage(rows):
    return {r["stage"]: r for r in rows}


# ---------------------------------------------------------------- store helpers
def add_ticket(root, tid, issue=1, state=State.MERGED):
    with Store(state_db_path(root)) as store:
        store.save_ticket(Ticket(id=tid, issue=issue, state=state, title="t"))


def put_event(root, tid, to, ts):
    db = sqlite3.connect(state_db_path(root))
    db.execute("INSERT INTO events (ticket_id, kind, payload, ts) VALUES (?,?,?,?)",
               (tid, "transition", json.dumps({"to": to, "actor": "x", "reason": ""}), ts))
    db.commit()
    db.close()


def put_result(root, tid, stage_name, payload, ts, ok=True):
    db = sqlite3.connect(state_db_path(root))
    db.execute("INSERT INTO stage_results (ticket_id, stage, attempt, ok, payload, ts)"
               " VALUES (?,?,?,?,?,?)", (tid, stage_name, 1, int(ok), json.dumps(payload), ts))
    db.commit()
    db.close()


# ---------------------------------------------------------------- T1 stages
def test_stage_mapping_order_and_awaiting_excluded(tmp_path):
    write_traces(tmp_path, [
        stage("refining", 10), stage("test_design", 20), stage("implementing", 30),
        stage("changes_requested", 50), stage("gates", 5), stage("in_review", 60),
        stage("awaiting_plan_approval", 9999), stage("awaiting_merge_approval", 9999),
        span("stage.result", 0, 77),
    ])
    data = collect(tmp_path)
    assert [s["stage"] for s in data["stages"]] == ["Refining", "Planning", "Implementing", "Gates", "Review"]
    rows = by_stage(data["stages"])
    assert rows["Refining"]["p50"] == 10 and rows["Planning"]["p50"] == 20
    assert rows["Implementing"]["count"] == 2 and rows["Implementing"]["max"] == 50
    assert rows["Implementing"]["mean"] == 40 and rows["Gates"]["p50"] == 5
    assert rows["Review"]["p99"] == 60
    assert {"count", "mean", "p50", "p90", "p99", "max"} <= set(rows["Review"])


def test_empty_stage_has_null_stats_and_low_samples_boundary(tmp_path):
    write_traces(tmp_path, [stage("gates", 1)] * MIN_SAMPLES + [stage("refining", 1)] * (MIN_SAMPLES - 1))
    rows = by_stage(collect(tmp_path)["stages"])
    assert rows["Review"]["count"] == 0 and rows["Review"]["p50"] is None and rows["Review"]["mean"] is None
    assert rows["Gates"]["low_samples"] is False
    assert rows["Refining"]["low_samples"] is True


def test_synthetic_and_malformed_ignored(tmp_path):
    bad = span("stage.gates", 0, 5)
    bad["start_ns"] = float("nan")
    write_traces(tmp_path, [stage("gates", 4), stage("gates", 100, 200), bad,
                            span("stage.gates", 0, 100, synthetic=True),
                            {"name": "stage.gates"}, {"name": "stage.gates", "attributes": "x"}],
                 raw=["not json", "[1, 2]", ""])
    gates = by_stage(collect(tmp_path)["stages"])["Gates"]
    assert gates["count"] == 2


# ---------------------------------------------------------------- T2 bottleneck
def test_bottleneck_largest_p50_and_none(tmp_path):
    write_traces(tmp_path, [stage("refining", 5), stage("gates", 50), stage("in_review", 20)])
    assert collect(tmp_path)["bottleneck"] == "Gates"
    write_traces(tmp_path, [])
    assert collect(tmp_path)["bottleneck"] is None


def test_bottleneck_tie_is_deterministic(tmp_path):
    write_traces(tmp_path, [stage("in_review", 10), stage("refining", 10)])
    assert collect(tmp_path)["bottleneck"] == "Refining"


# ---------------------------------------------------------------- T3 stalls
def stall_event(stage_name, **extra):
    return span("stall.detected", **{"devflow.stage": "stage." + stage_name}, **extra)


def test_stall_counts_rates_and_elevated(tmp_path):
    records = [stage("gates", 1)] * 100 + [stall_event("gates")]  # exactly 1% -> not elevated
    records += [stage("refining", 1)] * 10 + [stall_event("refining")]  # 10% -> elevated
    records += [stall_event("implementing")]  # no spans -> no division by zero
    records += [span("stall.detected", synthetic=True, **{"devflow.stage": "stage.gates"})]
    stalls = collect(_root(tmp_path, records))["stalls"]
    rows = by_stage(stalls["stages"])
    assert rows["Gates"]["count"] == 1 and rows["Gates"]["rate"] == 0.01 and rows["Gates"]["elevated"] is False
    assert rows["Refining"]["rate"] == 0.1 and rows["Refining"]["elevated"] is True
    assert rows["Implementing"]["rate"] is None and rows["Implementing"]["elevated"] is False
    assert rows["Review"]["count"] == 0 and rows["Review"]["elevated"] is False
    assert stalls["total"] == 3


def _root(tmp_path, records):
    write_traces(tmp_path, records)
    return tmp_path


# ---------------------------------------------------------------- T4 KPIs
def test_kpis_from_traces_and_store(tmp_path):
    base = NOW - 1000
    write_traces(tmp_path, [
        span("gate.order", **{"devflow.short_circuited": True}),
        span("gate.order", **{"devflow.short_circuited": False}),
        span("gate.order", **{"devflow.short_circuited": False}),
        span("gate.order", **{"devflow.short_circuited": False}),
        span("gate.order", synthetic=True, **{"devflow.short_circuited": True}),
        stall_event("gates"), stall_event("review"),
    ])
    add_ticket(tmp_path, "a", 1)
    for to, ts in [("refining", base), ("changes_requested", base + 50), ("merged", base + 100)]:
        put_event(tmp_path, "a", to, ts)
    add_ticket(tmp_path, "b", 2)
    for to, ts in [("refining", base), ("merged", base + 300)]:
        put_event(tmp_path, "b", to, ts)
    add_ticket(tmp_path, "c", 3, State.IMPLEMENTING)
    put_event(tmp_path, "c", "refining", base)
    k = collect(tmp_path)["kpis"]
    assert k["median_cycle_seconds"] == {"value": 200.0, "samples": 2}
    assert k["first_pass_approval_rate"] == {"value": 0.5, "samples": 2}
    assert k["blast_short_circuit_rate"] == {"value": 0.25, "samples": 4}
    assert k["total_stalls"]["value"] == 2


def test_kpis_null_without_data(tmp_path):
    with Store(state_db_path(tmp_path)):
        pass
    k = collect(tmp_path)["kpis"]
    for key in ("median_cycle_seconds", "first_pass_approval_rate", "blast_short_circuit_rate",
                "total_stalls"):
        assert k[key]["value"] is None
    assert k["median_cycle_seconds"]["samples"] == 0


# ---------------------------------------------------------------- T5 friction
def test_review_friction_counts(tmp_path):
    add_ticket(tmp_path, "a")
    put_result(tmp_path, "a", "review", {"findings": [
        {"blocking": True, "summary": "SECRET-REPLY"}, {"blocking": False}, {}, "junk"],
        "reply": "RAW AGENT TEXT"}, NOW)
    add_ticket(tmp_path, "b", 2)
    put_result(tmp_path, "b", "review", {"findings": [{"blocking": 1}]}, NOW)
    out = collect(tmp_path)
    assert out["friction"]["review"] == {"blocking": 2, "nitpick": 2, "tickets": 2}
    assert "RAW AGENT TEXT" not in json.dumps(out) and "SECRET-REPLY" not in json.dumps(out)


def test_gate_failure_labels_top10_fallback_redact_cap(tmp_path):
    add_ticket(tmp_path, "a")
    for i in range(12):
        put_result(tmp_path, "a", "gates", {"failures": [{"new": [f"fail-{i:02d}"] * 1}]}, NOW, ok=False)
    put_result(tmp_path, "a", "gates", {"failures": [{"new": ["fail-00"]}]}, NOW, ok=False)
    out = collect(tmp_path)["friction"]["gate_failures"]
    assert len(out) == 10 and out[0] == {"label": "fail-00", "count": 2}

    add_ticket(tmp_path, "b", 2)
    token = "ghp_" + "A" * 36
    put_result(tmp_path, "b", "gates", {"failures": [], "results": [
        {"command": f"pytest --token={token}", "ok": False}, {"command": "ok-cmd", "ok": True},
        {"command": "x" * 500, "ok": False}]}, NOW, ok=False)
    labels = [g["label"] for g in collect(tmp_path)["friction"]["gate_failures"]]
    assert all(len(label) <= 200 for label in labels)
    assert not any(token in label for label in labels)
    assert "ok-cmd" not in labels


def test_friction_empty(tmp_path):
    out = collect(tmp_path)["friction"]
    assert out == {"review": {"blocking": 0, "nitpick": 0, "tickets": 0}, "gate_failures": []}


# ---------------------------------------------------------------- T6 blast
def impact(callers1, symbols, uncovered):
    return {"callers": {"1": [{"symbol": f"c{i}"} for i in range(callers1)], "2": [{"symbol": "far"}]},
            "symbols": [{"id": s} for s in symbols], "uncovered": uncovered}


def test_blast_metrics(tmp_path):
    add_ticket(tmp_path, "a")
    put_result(tmp_path, "a", "gates", {"impact": impact(1, ["s1", "s2"], ["s2"])}, NOW)
    put_result(tmp_path, "a", "gates", {"impact": impact(4, ["s1", "s2", "s3", "s4"], ["s4"])}, NOW)
    add_ticket(tmp_path, "b", 2)  # falls back to review impact
    put_result(tmp_path, "b", "gates", {"results": []}, NOW)
    put_result(tmp_path, "b", "review", {"impact": impact(2, ["x"], [])}, NOW)
    add_ticket(tmp_path, "c", 3)
    put_result(tmp_path, "c", "gates", {"impact": {}}, NOW)
    blast = collect(tmp_path)["blast"]
    assert blast["tickets_with_impact"] == 2
    assert blast["avg_hop1_callers"] == 3.0
    assert blast["coverage_ratio"] == round(4 / 5, 4)


def test_blast_null_and_zero_denominator(tmp_path):
    assert collect(tmp_path)["blast"]["avg_hop1_callers"] is None
    add_ticket(tmp_path, "a")
    put_result(tmp_path, "a", "gates", {"impact": {"callers": {}, "symbols": [], "uncovered": []}}, NOW)
    blast = collect(tmp_path)["blast"]
    assert blast["tickets_with_impact"] == 1 and blast["avg_hop1_callers"] == 0
    assert blast["coverage_ratio"] is None


# ---------------------------------------------------------------- T7 window
@pytest.mark.parametrize("window,seconds", [("24h", 86400), ("7d", 7 * 86400)])
def test_window_boundaries(tmp_path, window, seconds):
    cutoff = NOW - seconds
    inside = span("stage.gates", cutoff - 5, cutoff)
    outside = span("stage.gates", cutoff - 5, cutoff)
    outside["end_ns"] -= 1
    write_traces(tmp_path, [inside, outside, stall_event("gates") | {"end_ns": outside["end_ns"]}])
    data = collect(tmp_path, window)
    assert by_stage(data["stages"])["Gates"]["count"] == 1
    assert data["telemetry"]["spans"] == 1 and data["kpis"]["total_stalls"]["value"] == 0
    assert collect(tmp_path, "all")["telemetry"]["spans"] == 3


def test_window_store_rows_and_merged(tmp_path):
    cutoff = NOW - 86400
    add_ticket(tmp_path, "old")
    for to, ts in [("refining", cutoff - 500), ("merged", cutoff - 1)]:
        put_event(tmp_path, "old", to, ts)
    put_result(tmp_path, "old", "review", {"findings": [{"blocking": True}]}, cutoff - 1)
    add_ticket(tmp_path, "new", 2)
    for to, ts in [("refining", cutoff - 500), ("merged", cutoff)]:
        put_event(tmp_path, "new", to, ts)
    put_result(tmp_path, "new", "review", {"findings": [{"blocking": True}]}, cutoff)
    day, everything = collect(tmp_path, "24h"), collect(tmp_path, "all")
    assert day["kpis"]["median_cycle_seconds"] == {"value": 500.0, "samples": 1}
    assert day["friction"]["review"]["blocking"] == 1
    assert everything["kpis"]["median_cycle_seconds"]["samples"] == 2
    assert everything["friction"]["review"]["blocking"] == 2


# ---------------------------------------------------------------- T8 shape & strictness
TOP = {"window", "generated_at", "telemetry", "kpis", "stages", "bottleneck", "stalls", "friction", "blast"}


def test_payload_shape_without_traces_or_db(tmp_path):
    data = collect(tmp_path)
    assert set(data) == TOP and data["window"] == "all"
    assert data["telemetry"] == {"available": False, "spans": 0}
    assert all(v["value"] is None for v in data["kpis"].values())
    assert not (tmp_path / ".devflow").exists()


def test_parse_window():
    assert parse_window(None) == "all" and parse_window("") == "all"
    assert [parse_window(w) for w in ("all", "24h", "7d")] == ["all", "24h", "7d"]
    for bad in ("30d", "24H", "abc", " 7d"):
        with pytest.raises(ValueError):
            parse_window(bad)
    with pytest.raises(ValueError):
        collect_analytics(Path("."), "bogus")


def test_non_finite_values_stay_strict_json(tmp_path):
    nan, inf = float("nan"), float("inf")
    write_traces(tmp_path, [
        {"name": "stage.gates", "start_ns": nan, "end_ns": inf, "attributes": {}},
        {"name": "stage.gates", "start_ns": -inf, "end_ns": 5, "attributes": {}},
        span("stall.detected", **{"devflow.stage": "stage.gates", "devflow.duration_seconds": nan}),
        span("gate.order", **{"devflow.short_circuited": True}),
    ])
    for window in ("all", "24h"):
        data = collect(tmp_path, window)
        json.dumps(data, allow_nan=False)
    assert not any(isinstance(v, float) and not math.isfinite(v) for v in collect(tmp_path)["stages"][3].values())


def test_clean_replaces_non_finite():
    assert analytics._clean({"a": [float("nan"), 1.5, float("inf")]}) == {"a": [None, 1.5, None]}


def test_collection_does_not_mutate_traces(tmp_path):
    log = write_traces(tmp_path, [stage("gates", 3)], raw=["garbage"])
    before = log.read_bytes()
    collect(tmp_path, "24h")
    assert log.read_bytes() == before
