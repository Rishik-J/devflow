import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from devflow import telemetry
from devflow.agents import AgentRequest, AgentResult, FakeAgent
from devflow.states import State
from tests.test_engine import Env


@pytest.fixture
def capture(tmp_path):
    log = tmp_path / "traces.jsonl"
    telemetry.configure(settings={"enabled": True, "log_file": str(log)})
    yield log
    telemetry.configure(settings={})


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_nested_spans_keep_trace_parent_and_ticket_metadata(capture):
    with (telemetry.span("root", {"langfuse.session.id": "repo:issue-1"}) as root,
          telemetry.span("child") as child):
        telemetry.event("step", **{"devflow.ok": True})
    records = {r["name"]: r for r in rows(capture)}
    assert records["child"]["parent_id"] == root.span_id
    assert records["step"]["parent_id"] == child.span_id
    assert {r["trace_id"] for r in records.values()} == {root.trace_id}
    assert all(r["attributes"]["langfuse.session.id"] == "repo:issue-1" for r in records.values())
    assert all(r["end_ns"] >= r["start_ns"] for r in records.values())


def test_errors_record_the_type_and_never_the_exception_message(capture):
    with pytest.raises(ValueError), telemetry.span("error"):
        raise ValueError("SENSITIVE_VALUE_DO_NOT_EXPORT")
    text = capture.read_text()
    assert "SENSITIVE_VALUE" not in text
    record = rows(capture)[0]
    assert record["status"] == "error" and record["attributes"]["error.type"] == "ValueError"


def test_agent_trace_counts_usage_without_capturing_content(capture, tmp_path):
    result = AgentResult(text="PRIVATE RESPONSE", cost_usd=.5,
                         raw={"usage": {"input_tokens": 10, "output_tokens": 3}})
    agent = FakeAgent({"coder": [result]})
    req = AgentRequest("coder", "PRIVATE PROMPT", tmp_path)
    assert agent.run(req) is result
    record = rows(capture)[0]
    assert record["attributes"]["gen_ai.usage.input_tokens"] == 10
    assert json.loads(record["attributes"]["langfuse.observation.cost_details"]) == {"total": .5}
    assert "PRIVATE" not in capture.read_text()


def test_no_configuration_means_no_pending_export(tmp_path):
    telemetry.configure(settings={})
    with telemetry.span("disabled"):
        pass
    assert not telemetry._pending


def test_export_outage_keeps_local_records_and_does_not_raise(capture, monkeypatch, capsys):
    with telemetry.span("recorded"):
        pass
    def offline(*args):
        raise OSError("hidden secret in network exception")
    monkeypatch.setattr(telemetry, "export", offline)
    telemetry.flush()
    assert rows(capture)[0]["name"] == "recorded"
    error = capsys.readouterr().err
    assert "local trace log retained" in error and "hidden secret" not in error


def test_otlp_json_structure_and_headers_are_accepted_by_http_receiver(capture):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with telemetry.span("root", {"count": 3, "ok": True}):
            telemetry.event("child")
        config = {"endpoint": f"http://127.0.0.1:{server.server_port}/api/public/otel/v1/traces",
                  "public_key": "public", "secret_key": "private"}
        telemetry.export(rows(capture), config)
        path, headers, payload = received[0]
        assert path.endswith("/v1/traces")
        assert headers["Authorization"].startswith("Basic ")
        assert headers["x-langfuse-ingestion-version"] == "4"
        spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 2 and spans[0]["traceId"] == spans[1]["traceId"]
        assert spans[0]["parentSpanId"] == spans[1]["spanId"]
        attrs = {a["key"]: a["value"] for a in spans[1]["attributes"]}
        assert attrs["count"] == {"intValue": "3"} and attrs["ok"] == {"boolValue": True}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_full_ticket_emits_stage_agent_gate_and_state_spans(capture, tmp_path):
    env = Env(tmp_path / "ticket")
    try:
        engine = env.engine()
        assert engine.run(1).ticket.state is State.AWAITING_PLAN_APPROVAL
        env.say("alice", "/approve")
        env.engine().run(1)
        records = rows(capture)
        names = {r["name"] for r in records}
        assert {"ticket.run", "stage.refining", "agent.call", "gate.result", "state.transition", "tool.git"} <= names
        stages = [r for r in records if r["name"].startswith("stage.")]
        assert any(r["status"] == "waiting" for r in stages)
        roots = [r for r in records if r["name"] == "ticket.run"]
        assert roots and all(r["attributes"]["langfuse.session.id"] == "t:issue-1" for r in roots)
    finally:
        env.store.close()


def test_malformed_config_cannot_break_workflow(tmp_path, monkeypatch, capsys):
    config = tmp_path / "invalid.json"
    config.write_text("not json")
    monkeypatch.setenv("DEVFLOW_TELEMETRY_CONFIG", str(config))
    telemetry.configure(tmp_path)
    with telemetry.span("still works"):
        pass
    assert not telemetry._settings["enabled"]
    assert "tracing disabled" in capsys.readouterr().err
