"""Inspect local telemetry and verify OTLP connectivity without running a real ticket."""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from . import telemetry
from .detect import repo_root


def command(args):
    root = repo_root(Path(args.path).resolve())
    if root is None:
        raise ValueError("not inside a git repository")
    path = root / ".devflow/state/telemetry.json"
    config = json.loads(path.read_text()) if path.exists() else {}
    log = Path(config.get("log_file", root / ".devflow/state/traces.jsonl"))
    if args.telemetry_action == "status":
        print(json.dumps({"enabled": bool(config.get("enabled")), "log_file": str(log),
                          "collector_configured": bool(config.get("endpoint")),
                          "capture_content": config.get("capture_content") is True,
                          "max_content_characters": config.get("max_content_characters", 200_000)}, indent=2))
        return 0
    if args.telemetry_action == "demo":
        if not config.get("enabled"):
            raise ValueError("configure telemetry before running the demo")
        with telemetry.span("devflow.connectivity-demo", {
                "langfuse.trace.name": "devflow.connectivity-demo",
                "langfuse.session.id": "devflow-connectivity-demo",
                "langfuse.trace.metadata.synthetic": "true"}) as trace:
            for phase in ("refine", "test_design", "implement", "gates", "review", "merge", "retro"):
                with telemetry.span("demo." + phase, {"devflow.synthetic": True}):
                    telemetry.event("demo.check", **{"devflow.ok": True})
        telemetry.flush()
        print("Synthetic trace (no model calls or repository changes): " + trace.trace_id)
        return 0
    records = []
    if log.exists():
        for line in log.read_text().splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    if args.telemetry_action == "replay":
        if args.limit < 1:
            raise ValueError("replay limit must be positive")
        try:
            telemetry.export(records[-args.limit:], config)
        except OSError as exc:
            raise ValueError("collector unavailable; local records were not removed") from exc
        print(f"Replayed {min(len(records), args.limit)} locally retained spans")
        return 0
    if args.issue is not None:
        if args.issue < 1:
            raise ValueError("issue must be a positive integer")
        print(json.dumps(issue_metrics(records, args.issue), indent=2))
        return 0
    print(json.dumps(aggregate(records), indent=2))
    return 0


def _stats(values):
    return {"count": len(values), "mean": round(sum(values) / len(values), 3), "max": round(max(values), 3)}


def aggregate(records):
    """Aggregate metrics over all records, ignoring non-object records and tolerating incomplete ones."""
    durations = defaultdict(list)
    calls, usage, outcomes = Counter(), Counter(), Counter()
    synthetic = spans = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        spans += 1
        name = str(record.get("name", ""))
        attributes = _attrs(record)
        if attributes.get("langfuse.trace.metadata.synthetic") == "true":
            synthetic += 1
            continue
        if name.startswith("stage.") and name != "stage.result":
            seconds = _seconds(record)
            if seconds is not None:
                durations[name].append(seconds)
        if name == "agent.call" or name.startswith("agent."):
            calls[str(attributes.get("devflow.agent", "unknown"))] += 1
        if name == "usage.recorded":
            usage[str(attributes.get("devflow.agent", "unknown"))] += _cost(attributes.get("devflow.cost_usd"))
        if name == "ticket.run":
            outcomes[str(attributes.get("devflow.result", record.get("status")))] += 1
    return {"spans": spans, "synthetic_spans_excluded": synthetic, "ticket_turn_outcomes": dict(outcomes),
            "agent_cli_calls": dict(calls), "coding_cost_usd_recorded": dict(usage),
            "stage_latency_seconds": {name: _stats(values) for name, values in durations.items()}}


def _attrs(record):
    attributes = record.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _cost(value):
    return value if _is_number(value) and math.isfinite(value) else 0


def _seconds(record):
    start, end = record.get("start_ns"), record.get("end_ns")
    if not (_is_number(start) and _is_number(end)):
        return None
    try:
        start, end = float(start), float(end)
        seconds = (end - start) / 1e9
    except OverflowError:
        return None
    if not (math.isfinite(start) and math.isfinite(end) and math.isfinite(seconds)) or end < start:
        return None
    return seconds


_ISSUE_SESSION = re.compile(r"(?:^|:)issue-(\d+)$")


def _is_synthetic(record):
    attributes = _attrs(record)
    return (attributes.get("langfuse.trace.metadata.synthetic") == "true"
            or attributes.get("langfuse.session.id") == "devflow-connectivity-demo")


def _matches_issue(record, issue):
    attributes = _attrs(record)
    session = attributes.get("langfuse.session.id")
    if isinstance(session, str):
        found = _ISSUE_SESSION.search(session)
        if found and int(found.group(1)) == issue:
            return True
    for key in ("langfuse.trace.metadata.issue", "devflow.issue"):
        value = attributes.get(key)
        if not isinstance(value, bool) and str(value) == str(issue):
            return True
    return False


def _merged_seconds(intervals):
    total, current_end, current_start = 0.0, None, None
    for start, end in sorted(intervals):
        if current_end is None or start > current_end:
            if current_end is not None:
                total += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None:
        total += current_end - current_start
    return total


def _session_summary(session, records):
    outcomes, agent_calls = Counter(), Counter()
    stages, agents = defaultdict(list), defaultdict(list)
    intervals, runs = [], 0
    for record in records:
        name = str(record.get("name", ""))
        seconds = _seconds(record)
        if name == "ticket.run":
            runs += 1
            outcomes[str(_attrs(record).get("devflow.result", record.get("status")))] += 1
            if seconds is not None:
                intervals.append((float(record["start_ns"]) / 1e9, float(record["end_ns"]) / 1e9))
        elif name.startswith("stage.") and name != "stage.result":
            if seconds is not None:
                stages[name].append(seconds)
        elif name.startswith("agent."):
            key = name[len("agent."):]
            agent_calls[key] += 1
            if seconds is not None:
                agents[key].append(seconds)
    active = elapsed = gap = None
    if intervals:
        active = _merged_seconds(intervals)
        elapsed = max(end for _, end in intervals) - min(start for start, _ in intervals)
        gap = max(0.0, elapsed - active)
        active, elapsed, gap = round(active, 3), round(elapsed, 3), round(gap, 3)
    return {"session": session, "spans": len(records), "runs": runs, "outcomes": dict(outcomes),
            "active_run_seconds": active, "elapsed_wall_seconds": elapsed, "inferred_gap_seconds": gap,
            "stage_latency_seconds": {n: _stats(v) for n, v in stages.items()},
            "agent_latency_seconds": {n: _stats(v) for n, v in agents.items()},
            "agent_calls": dict(agent_calls)}


LABELS = {
    "active_run_seconds": "observed: ticket.run span time, overlapping runs merged",
    "elapsed_wall_seconds": "observed: first ticket.run start to last ticket.run end",
    "inferred_gap_seconds": "inferred: elapsed minus active, i.e. time between invocations",
}


def issue_summary(records, issue):
    """Per-session summaries for one issue. Reads only whitelisted fields; never copies attribute values."""
    sessions = defaultdict(list)
    for record in records:
        if not isinstance(record, dict) or _is_synthetic(record) or not _matches_issue(record, issue):
            continue
        session = _attrs(record).get("langfuse.session.id")
        sessions[session if isinstance(session, str) else f"issue-{issue}"].append(record)
    return [_session_summary(name, rows) for name, rows in sorted(sessions.items())]


def issue_metrics(records, issue):
    """Issue-mode output is built only from span names, counts, durations and outcome names."""
    sessions = issue_summary(records, issue)
    outcomes, calls, stages = Counter(), Counter(), defaultdict(list)
    for record in records:
        if not isinstance(record, dict) or _is_synthetic(record) or not _matches_issue(record, issue):
            continue
        name = str(record.get("name", ""))
        seconds = _seconds(record)
        if name == "ticket.run":
            outcomes[str(_attrs(record).get("devflow.result", record.get("status")))] += 1
        elif name.startswith("agent."):
            calls[name[len("agent."):]] += 1
        elif name.startswith("stage.") and name != "stage.result" and seconds is not None:
            stages[name].append(seconds)
    return {"issue": issue, "spans": sum(s["spans"] for s in sessions),
            "synthetic_spans_excluded": sum(1 for r in records if isinstance(r, dict) and _is_synthetic(r)),
            "ticket_turn_outcomes": dict(outcomes), "agent_cli_calls": dict(calls),
            "coding_cost_usd_recorded": {},
            "stage_latency_seconds": {n: _stats(v) for n, v in stages.items()},
            "labels": LABELS, "sessions": sessions}
