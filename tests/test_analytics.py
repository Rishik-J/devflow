"""Tests for trace-log analytics and the UI endpoint."""

import http.server
import json
import socketserver
import threading
import urllib.request
from pathlib import Path

from devflow.analytics import collect_analytics, compute, load_records
from devflow.ui import UiHandler


def span(name, start, end, session="r:issue-1", status="ok", **attrs):
    a = {"langfuse.session.id": session}
    a.update(attrs)
    return {"name": name, "start_ns": int(start * 1e9), "end_ns": int(end * 1e9), "status": status, "attributes": a}


RECORDS = [
    span("ticket.run", 0, 100, **{"devflow.result": "done"}),
    span("agent.claude.coder", 10, 70, **{"devflow.role": "coder", "gen_ai.usage.input_tokens": 100,
                                          "gen_ai.usage.output_tokens": 20}),
    span("agent.codex.reviewer", 70, 90, **{"devflow.role": "reviewer"}),
    span("usage.recorded", 70, 70, **{"devflow.agent": "claude", "devflow.cost_usd": 1.5}),
    span("gate.result", 20, 30, **{"devflow.gate": "unit", "devflow.ok": True, "devflow.duration_s": 10}),
    span("gate.result", 30, 31, **{"devflow.gate": "unit", "devflow.ok": False}),
    span("stage.result", 90, 90, **{"devflow.review_findings": 3, "devflow.blocking_findings": 1}),
    span("workflow.summary", 100, 100, **{"devflow.total_seconds": 90, "devflow.human_wait_seconds": 30}),
    span("tool.git", 1, 2, status="error"),
    # excluded: demo session, synthetic flag, and a session without an issue
    span("ticket.run", 0, 5, session="devflow-connectivity-demo"),
    span("agent.fake.coder", 0, 5, **{"devflow.synthetic": True, "langfuse.trace.metadata.synthetic": "true"}),
    span("ticket.run", 0, 5, session="no-issue-here"),
    {"name": "broken"},
]


def test_compute_per_ticket_and_totals():
    out = compute(RECORDS)
    assert out["spans_analyzed"] == 9
    assert out["spans_skipped"] == 4
    (ticket,) = out["tickets"]
    assert ticket["issue"] == 1 and ticket["repo"] == "r" and ticket["done"] is True
    assert ticket["agent_calls"] == 2 and ticket["agent_seconds"] == 80.0
    assert ticket["human_wait_seconds"] == 30.0 and ticket["total_seconds"] == 90.0
    assert ticket["cost_usd"] == 1.5 and ticket["tokens_in"] == 100 and ticket["tokens_out"] == 20
    assert ticket["gates"] == 2 and ticket["gate_failures"] == 1 and ticket["error_spans"] == 1
    assert out["totals"]["completed"] == 1 and out["totals"]["median_ticket_seconds"] == 90.0
    assert out["gates"]["unit"]["pass"] == 1 and out["gates"]["unit"]["fail"] == 1
    assert out["agent_latency_seconds"]["claude.coder"]["mean"] == 60.0
    assert out["review"] == {"results": 1, "with_blocking": 1, "findings": 3, "blocking": 1}
    assert out["errors_by_span"] == {"tool.git": 1}


def test_compute_filters_repo_and_tolerates_bad_values():
    assert compute(RECORDS, repo="other")["tickets"] == []
    bad = [span("agent.x.y", 5, 1, **{"gen_ai.usage.input_tokens": "lots", "devflow.cost_usd": float("nan")}),
           span("usage.recorded", 0, 0, **{"devflow.cost_usd": float("inf")})]
    out = compute(bad)
    assert out["tickets"][0]["cost_usd"] == 0 and out["tickets"][0]["tokens_in"] == 0


def test_collect_analytics_missing_log_and_custom_path(tmp_path: Path):
    assert collect_analytics(tmp_path)["tickets"] == []
    log = tmp_path / "custom.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in RECORDS[:2]) + "\nnot json\n")
    state = tmp_path / ".devflow/state"
    state.mkdir(parents=True)
    (state / "telemetry.json").write_text(json.dumps({"log_file": str(log)}))
    assert len(load_records(log)) == 2
    assert collect_analytics(tmp_path)["totals"]["tickets"] == 1


def test_analytics_endpoint(tmp_path: Path):
    state = tmp_path / ".devflow/state"
    state.mkdir(parents=True)
    (state / "traces.jsonl").write_text("\n".join(json.dumps(r) for r in RECORDS))

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    class Bound(UiHandler):
        root = tmp_path

    server = Server(("127.0.0.1", 0), Bound)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/analytics") as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["totals"]["tickets"] == 1
        with urllib.request.urlopen(base + "/api/analytics?repo=nope") as resp:
            assert json.loads(resp.read())["tickets"] == []
        with urllib.request.urlopen(base + "/") as resp:
            assert "a-tickets" in resp.read().decode()
    finally:
        server.shutdown()
        server.server_close()
