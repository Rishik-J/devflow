"""Workflow analytics computed from the local trace log (``traces.jsonl``).

Only span names, counts, durations, outcome names and numeric usage are read; prompt or
response content is never copied into the result.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .telemetry_cli import _ISSUE_SESSION, _attrs, _cost, _is_number, _is_synthetic, _seconds


def log_path(root: Path) -> Path:
    config = root / ".devflow/state/telemetry.json"
    try:
        data = json.loads(config.read_text()) if config.exists() else {}
    except ValueError:
        data = {}
    custom = data.get("log_file") if isinstance(data, dict) else None
    return Path(custom) if isinstance(custom, str) and custom else root / ".devflow/state/traces.jsonl"


def load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with path.open(errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _dist(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": 0.0, "p50": 0.0, "max": 0.0}
    return {"count": len(values), "mean": round(statistics.fmean(values), 2),
            "p50": round(statistics.median(values), 2), "max": round(max(values), 2)}


def _num(value: Any) -> float:
    return float(value) if _is_number(value) else 0.0


def _session_stats(session: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    roles: Counter = Counter()
    agent_seconds = cost = 0.0
    tokens_in = tokens_out = repairs = errors = gates = gate_failures = 0
    outcomes: Counter = Counter()
    review_rounds = attempts = 0
    summary: dict[str, Any] = {}
    starts, ends = [], []
    for record in rows:
        name, attrs = str(record.get("name", "")), _attrs(record)
        seconds = _seconds(record)
        if _is_number(record.get("start_ns")) and _is_number(record.get("end_ns")):
            starts.append(record["start_ns"])
            ends.append(record["end_ns"])
        if record.get("status") == "error":
            errors += 1
        review_rounds = max(review_rounds, int(_num(attrs.get("devflow.review_round"))))
        attempts = max(attempts, int(_num(attrs.get("devflow.implement_attempt"))))
        if name == "ticket.run":
            outcomes[str(attrs.get("devflow.result", record.get("status")))] += 1
        elif name.startswith("agent."):
            roles[str(attrs.get("devflow.role", "unknown"))] += 1
            agent_seconds += seconds or 0.0
            tokens_in += int(_num(attrs.get("gen_ai.usage.input_tokens")))
            tokens_out += int(_num(attrs.get("gen_ai.usage.output_tokens")))
        elif name == "usage.recorded":
            cost += _cost(attrs.get("devflow.cost_usd"))
        elif name == "gate.result":
            gates += 1
            gate_failures += attrs.get("devflow.ok") is False
        elif name == "schema.repair":
            repairs += 1
        elif name == "workflow.summary":
            if _num(attrs.get("devflow.total_seconds")) >= _num(summary.get("total_seconds")):
                summary = {"total_seconds": _num(attrs.get("devflow.total_seconds")),
                           "human_wait_seconds": _num(attrs.get("devflow.human_wait_seconds"))}
    found = _ISSUE_SESSION.search(session)
    repo = session.rsplit(":", 1)[0] if ":" in session else ""
    wall = (max(ends) - min(starts)) / 1e9 if starts else 0.0
    total = summary.get("total_seconds") or wall
    human = summary.get("human_wait_seconds", 0.0)
    return {
        "session": session, "repo": repo, "issue": int(found.group(1)) if found else None,
        "runs": sum(outcomes.values()), "outcomes": dict(outcomes),
        "wall_seconds": round(wall, 1), "total_seconds": round(total, 1),
        "human_wait_seconds": round(human, 1), "agent_seconds": round(agent_seconds, 1),
        "agent_calls": sum(roles.values()), "roles": dict(roles),
        "gates": gates, "gate_failures": gate_failures, "schema_repairs": repairs,
        "error_spans": errors, "review_rounds": review_rounds, "implement_attempts": attempts,
        "cost_usd": round(cost, 4), "tokens_in": tokens_in, "tokens_out": tokens_out,
        "done": outcomes.get("done", 0) > 0,
    }


def compute(records: list[dict[str, Any]], repo: str | None = None) -> dict[str, Any]:
    """Build the analytics payload. Synthetic/demo records and sessions without an issue are skipped."""
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped = 0
    for record in records:
        session = _attrs(record).get("langfuse.session.id")
        if _is_synthetic(record) or not isinstance(session, str) or not _ISSUE_SESSION.search(session):
            skipped += 1
            continue
        if repo and session.rsplit(":", 1)[0] != repo:
            skipped += 1
            continue
        by_session[session].append(record)

    tickets = sorted((_session_stats(s, rows) for s, rows in by_session.items()),
                     key=lambda t: (t["repo"], t["issue"] or 0))
    agent_latency: dict[str, list[float]] = defaultdict(list)
    gate_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"pass": 0, "fail": 0, "seconds": []})
    stage_latency: dict[str, list[float]] = defaultdict(list)
    findings = {"results": 0, "with_blocking": 0, "findings": 0, "blocking": 0}
    errors: Counter = Counter()
    routing: Counter = Counter()
    cost_by_agent: Counter = Counter()
    for rows in by_session.values():
        for record in rows:
            name, attrs, seconds = str(record.get("name", "")), _attrs(record), _seconds(record)
            if name.startswith("agent.") and seconds is not None:
                agent_latency[name[len("agent."):]].append(seconds)
            elif name.startswith("stage.") and name != "stage.result" and seconds is not None:
                stage_latency[name].append(seconds)
            elif name == "gate.result":
                gate = gate_stats[str(attrs.get("devflow.gate", "unknown"))]
                gate["fail" if attrs.get("devflow.ok") is False else "pass"] += 1
                value = attrs.get("devflow.duration_s")
                gate["seconds"].append(_num(value) if _is_number(value) else (seconds or 0.0))
            elif name == "stage.result" and _is_number(attrs.get("devflow.review_findings")):
                findings["results"] += 1
                findings["findings"] += int(attrs["devflow.review_findings"])
                blocking = int(_num(attrs.get("devflow.blocking_findings")))
                findings["blocking"] += blocking
                findings["with_blocking"] += blocking > 0
            elif name == "routing.selected":
                routing[str(attrs.get("devflow.agent", "unknown"))] += 1
            elif name == "usage.recorded":
                cost_by_agent[str(attrs.get("devflow.agent", "unknown"))] += _cost(attrs.get("devflow.cost_usd"))
            if record.get("status") == "error":
                errors[name] += 1

    completed = [t for t in tickets if t["done"]]
    return {
        "spans_analyzed": sum(len(rows) for rows in by_session.values()),
        "spans_skipped": skipped,
        "repos": sorted({t["repo"] for t in tickets}),
        "totals": {
            "tickets": len(tickets), "completed": len(completed),
            "cost_usd": round(sum(t["cost_usd"] for t in tickets), 2),
            "agent_calls": sum(t["agent_calls"] for t in tickets),
            "agent_seconds": round(sum(t["agent_seconds"] for t in tickets), 1),
            "human_wait_seconds": round(sum(t["human_wait_seconds"] for t in tickets), 1),
            "gates": sum(g["pass"] + g["fail"] for g in gate_stats.values()),
            "gate_failures": sum(g["fail"] for g in gate_stats.values()),
            "schema_repairs": sum(t["schema_repairs"] for t in tickets),
            "error_spans": sum(t["error_spans"] for t in tickets),
            "median_ticket_seconds": round(statistics.median([t["total_seconds"] for t in completed]), 1)
            if completed else 0.0,
        },
        "tickets": tickets,
        "agent_latency_seconds": {k: _dist(v) for k, v in sorted(agent_latency.items())},
        "stage_latency_seconds": {k: _dist(v) for k, v in sorted(stage_latency.items())},
        "gates": {k: {"pass": v["pass"], "fail": v["fail"], **{"mean_seconds": _dist(v["seconds"])["mean"]}}
                  for k, v in sorted(gate_stats.items())},
        "review": findings,
        "routing": dict(routing),
        "cost_by_agent_usd": {k: round(v, 4) for k, v in cost_by_agent.items()},
        "errors_by_span": dict(errors.most_common(10)),
    }


def collect_analytics(root: Path, repo: str | None = None) -> dict[str, Any]:
    return compute(load_records(log_path(root)), repo)
