"""Inspect local telemetry and verify OTLP connectivity without running a real ticket."""
from __future__ import annotations

import json
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
    durations = defaultdict(list)
    calls, usage, outcomes = Counter(), Counter(), Counter()
    synthetic = 0
    for record in records:
        name = record["name"]
        if record["attributes"].get("langfuse.trace.metadata.synthetic") == "true":
            synthetic += 1
            continue
        if name.startswith("stage.") and name != "stage.result":
            durations[name].append((record["end_ns"] - record["start_ns"]) / 1e9)
        attributes = record["attributes"]
        if name == "agent.call" or name.startswith("agent."):
            calls[attributes.get("devflow.agent", "unknown")] += 1
        if name == "usage.recorded":
            usage[attributes.get("devflow.agent", "unknown")] += attributes.get("devflow.cost_usd", 0)
        if name == "ticket.run":
            outcomes[attributes.get("devflow.result", record["status"])] += 1
    print(json.dumps({"spans": len(records), "synthetic_spans_excluded": synthetic, "ticket_turn_outcomes": dict(outcomes),
                      "agent_cli_calls": dict(calls), "coding_cost_usd_recorded": dict(usage),
                      "stage_latency_seconds": {
                          name: {"count": len(values), "mean": round(sum(values)/len(values), 3),
                                 "max": round(max(values), 3)} for name, values in durations.items()}}, indent=2))
    return 0
