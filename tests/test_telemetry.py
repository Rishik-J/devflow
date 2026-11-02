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
        assert {"ticket.run", "stage.refining", "agent.fake.planner", "gate.result", "state.transition", "tool.git"} <= names
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


@pytest.fixture
def detailed_capture(capture):
    telemetry.configure(settings={"enabled": True, "capture_content": True, "log_file": str(capture),
                                  "secret_key": "test-collector-secret-value"})
    return capture


def test_detailed_agent_records_prompts_handoff_response_and_redacts_secrets(detailed_capture, tmp_path, monkeypatch):
    monkeypatch.setenv("OTHER_API_KEY", "known-environment-secret-value")
    agent = FakeAgent({"coder": [AgentResult(text="Done. password=secretword", session="resume-1")]})
    with telemetry.span("stage.implementing"):
        agent.run(AgentRequest("coder", "Implement addition known-environment-secret-value", tmp_path,
                               system="System instructions", session="resume-0"))
    record = next(r for r in rows(detailed_capture) if r["name"].startswith("agent."))
    attrs = record["attributes"]
    assert attrs["langfuse.observation.type"] == "agent"
    assert attrs["devflow.handoff.from"] == "stage.implementing"
    assert json.loads(attrs["langfuse.observation.input"])["system"] == "System instructions"
    assert json.loads(attrs["langfuse.observation.output"])["session"] == "resume-1"
    text = detailed_capture.read_text()
    assert "known-environment-secret-value" not in text and "secretword" not in text
    assert "Implement addition" in text and "Done." in text


def test_payload_redaction_and_visible_truncation(detailed_capture):
    telemetry._settings["max_content_characters"] = 1000
    telemetry.content_event("payload", input={"api_key": "arbitrarysecret", "nested": {"token": "sensitive"},
                                             "text": "Bearer abcdefghijklmnop " + "x" * 2000})
    record = rows(detailed_capture)[0]
    assert record["attributes"]["devflow.input_truncated"]
    payload = json.loads(record["attributes"]["langfuse.observation.input"])
    assert payload["truncated"] and payload["original_characters"] > 1000
    assert "arbitrarysecret" not in detailed_capture.read_text() and "abcdefghijklmnop" not in detailed_capture.read_text()


def test_claude_stream_captures_model_usage_and_correlated_tools(detailed_capture, tmp_path):
    from devflow.agents import ClaudeAgent
    from devflow.agents.base import ProcResult
    from tests.test_agents import CLAUDE_OK, Recorder
    events = [
        {"type": "assistant", "message": {"id": "msg-1", "model": "test-model", "usage": {"input_tokens": 30, "output_tokens": 10},
         "content": [{"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"path": "calc.py"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "def add(a,b): return a+b"}]}},
        json.loads(CLAUDE_OK),
    ]
    runner = Recorder(ProcResult(0, "\n".join(json.dumps(e) for e in events), ""))
    result = ClaudeAgent(runner=runner).run(AgentRequest("coder", "Inspect calc.py", tmp_path))
    assert result.ok and result.session == "sess-1"
    assert "stream-json" in runner.calls[0]["cmd"] and "--verbose" in runner.calls[0]["cmd"]
    records = rows(detailed_capture)
    agent = next(r for r in records if r["name"] == "agent.claude.coder")
    tool = next(r for r in records if r["name"] == "cli.tool.Read")
    generation = next(r for r in records if r["name"] == "cli.claude.generation")
    assert tool["parent_id"] == generation["parent_id"] == agent["span_id"]
    assert "def add" in tool["attributes"]["langfuse.observation.output"]
    assert generation["attributes"]["langfuse.observation.model.name"] == "test-model"
    assert json.loads(generation["attributes"]["langfuse.observation.usage_details"])["input"] == 30
    assert generation["attributes"]["devflow.reconstructed"]


def test_codex_command_and_cursor_tool_payloads(detailed_capture):
    with telemetry.span("agent.codex.coder") as parent:
        telemetry.cli_events(json.dumps({"type": "item.completed", "item": {"id": "command-1", "type": "command_execution",
            "command": "python -m pytest", "aggregated_output": "3 passed", "exit_code": 0}}), "codex")
        telemetry.cli_events("\n".join(json.dumps(e) for e in [
            {"type": "tool_call", "subtype": "started", "call_id": "cursor-1", "tool_call": {"readToolCall": {"args": {"path": "a.py"}}}},
            {"type": "tool_call", "subtype": "completed", "call_id": "cursor-1", "tool_call": {"readToolCall": {"result": {"success": {"content": "a = 1"}}}}},
        ]), "cursor")
    records = rows(detailed_capture)
    assert all(r["parent_id"] == parent.span_id for r in records if r["name"].startswith("cli."))
    assert "3 passed" in detailed_capture.read_text() and "a = 1" in detailed_capture.read_text()
    assert "python -m pytest" in detailed_capture.read_text()


def test_gate_failure_and_timeout_capture_output(detailed_capture, tmp_path):
    from devflow.gates import run_command
    code, out, timeout = run_command("printf 'test failure password=hiddenvalue'; exit 2", tmp_path, 3)
    assert code == 2 and not timeout and "test failure" in out
    code, out, timeout = run_command("printf 'before timeout'; sleep 5", tmp_path, .05)
    assert timeout
    records = rows(detailed_capture)
    assert all(r["status"] == "error" for r in records)
    assert "test failure" in detailed_capture.read_text() and "hiddenvalue" not in detailed_capture.read_text()
    assert "before timeout" in detailed_capture.read_text()


def test_metadata_only_mode_does_not_record_cli_content_or_enable_streams(capture, tmp_path):
    from devflow.agents import ClaudeAgent
    from devflow.agents.base import ProcResult
    from tests.test_agents import CLAUDE_OK, Recorder
    runner = Recorder(ProcResult(0, CLAUDE_OK, "private stderr"))
    ClaudeAgent(runner=runner).run(AgentRequest("coder", "private prompt", tmp_path))
    assert "stream-json" not in runner.calls[0]["cmd"]
    assert "private" not in capture.read_text()
    assert not any("langfuse.observation.input" in r["attributes"] for r in rows(capture))


@pytest.mark.parametrize("value,secret", [
    ({"text": 'password="many secret words"'}, "many secret words"),
    (["--api-key", "unrecognized-secret-value"], "unrecognized-secret-value"),
    ({"text": "https://name:passphrase@example.com/"}, "passphrase"),
    ({"text": "-----BEGIN PRIVATE KEY-----\nrawkey\n-----END PRIVATE KEY-----"}, "rawkey"),
])
def test_redaction_handles_common_credential_forms(detailed_capture, value, secret):
    telemetry.content_event("redaction-check", input=value)
    assert secret not in detailed_capture.read_text()


def test_unknown_cli_event_shape_cannot_break_agent_result(detailed_capture):
    events = telemetry.cli_events('{"type":"item.completed","item":null}', "codex")
    assert len(events) == 1


def test_cursor_stream_tool_text_cannot_trigger_false_login_failure(detailed_capture, tmp_path):
    from devflow.agents.base import ProcResult
    from devflow.agents.cursor import CursorAgent
    from tests.test_agents import Recorder
    stream = "\n".join(json.dumps(e) for e in [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Source says not logged in"}]}},
        {"type": "result", "subtype": "success", "result": "Inspected", "session_id": "chat-1"},
    ])
    runner = Recorder(ProcResult(0, stream, ""))
    result = CursorAgent(runner=runner).run(AgentRequest("reviewer", "Read auth.py", tmp_path))
    assert result.ok and result.text == "Inspected"
    assert "stream-json" in runner.calls[0]["cmd"]


def test_incomplete_cursor_stream_cannot_be_treated_as_success(detailed_capture, tmp_path):
    from devflow.agents.base import AgentError, ProcResult
    from devflow.agents.cursor import CursorAgent
    from tests.test_agents import Recorder
    runner = Recorder(ProcResult(0, '{"type":"system","subtype":"init"}', ""))
    with pytest.raises(AgentError, match="unreadable"):
        CursorAgent(runner=runner).run(AgentRequest("reviewer", "Inspect", tmp_path))


def test_openclaw_reports_public_usage_without_reading_other_sessions(detailed_capture, tmp_path):
    from devflow.agents import OpenClawAgent
    from devflow.agents.base import ProcResult
    from tests.test_agents import OPENCLAW_OK, Recorder
    reply = json.loads(OPENCLAW_OK)
    reply["result"]["meta"]["agentMeta"] = {"model": "test-model", "usage": {"input": 24, "output": 6}, "sessionId": "provider-1"}
    agent = OpenClawAgent("devflow", runner=Recorder(ProcResult(0, json.dumps(reply), "")))
    result = agent.run(AgentRequest("planner", "Plan the task", tmp_path))
    assert result.raw["usage"] == {"input_tokens": 24, "output_tokens": 6}
    records = rows(detailed_capture)
    call = next(r for r in records if r["name"] == "agent.openclaw:devflow.planner")
    assert call["attributes"]["devflow.reported_model"] == "test-model"
    assert call["attributes"]["gen_ai.usage.input_tokens"] == 24
    assert "provider-1" in detailed_capture.read_text()


# ---- repository and graph provenance ------------------------------------------------------

from devflow.graphify import GraphResult


@pytest.mark.parametrize("url,expected", [
    ("https://user:secret@github.com/o/r.git?x=1#f", "github.com/o/r"),
    ("git@github.com:o/r.git", "github.com/o/r"),
    ("ssh://git@host.example:2222/o/r", "host.example/o/r"),
    ("file://localhost/Users/alice/private-repo.git", ""), ("file:///Users/alice/repo.git", ""),
    ("ftp://host.example/o/r", ""),
    ("", ""), ("not a url", ""), ("/local/path/origin.git", ""), ("https://github.com/", ""),
])
def test_remote_sanitizer(url, expected):
    out = telemetry.sanitize_remote(url)
    assert out == expected and "secret" not in out and "@" not in out


def test_helpers_validate_and_drop_bad_values(capture):
    with telemetry.span("probe") as probe:
        telemetry.annotate(status="bogus", nodes=-1, edges=True, fingerprint="xyz", refreshed="yes", other=1)
        assert not any(k.startswith("devflow.graph.") for k in probe.attributes)
        telemetry.annotate(status="fresh", nodes=3, fingerprint="a" * 40)
        assert probe.attributes["devflow.graph.fingerprint"] == "a" * 12
        assert probe.attributes["devflow.graph.nodes"] == 3
    telemetry.trace_metadata(base_sha="abc123")  # no root: no-op, no raise


def test_helpers_noop_when_disabled():
    telemetry.configure(settings={})
    telemetry.annotate(status="fresh")
    telemetry.trace_metadata(base_sha="abc1234")


def root_of(path):
    return [r for r in rows(path) if r["name"] == "ticket.run"][-1]["attributes"]


def graph_spans(path):
    return [r["attributes"] for r in rows(path) if r["name"] == "graph.refresh"]


def test_trace_metadata_validates_on_root(capture):
    class Cfg:
        name = "t"
    class Eng:
        cfg = Cfg()
        @telemetry.ticket_run
        def run(self, issue):
            telemetry.trace_metadata(base_sha="not hex!", graph_status="nope", graph_nodes=-2,
                                     graph_fingerprint="ZZ", prompt="leak", base_sha2="abc1234")
            telemetry.trace_metadata(base_sha="deadbeef", graph_fingerprint="f" * 30)
            from types import SimpleNamespace
            return SimpleNamespace(ticket=SimpleNamespace(state=State.MERGED), status="done")
    Eng().run(1)
    attrs = root_of(capture)
    assert attrs["langfuse.trace.metadata.base_sha"] == "deadbeef"
    assert attrs["devflow.graph_fingerprint"] == "f" * 12
    allowed = {"base_sha", "remote", "graph_status", "graph_fingerprint", "graph_nodes", "graph_edges",
               "repo", "issue"}
    assert {k.rsplit(".", 1)[1] for k in attrs if k.startswith("langfuse.trace.metadata.")} <= allowed
    assert "leak" not in capture.read_text()


def run_ticket(tmp_path, builder=None, remote=None):
    env = Env(tmp_path / "ticket")
    if remote:
        env.cfg.remote = remote
    engine = env.engine()
    if builder:
        engine.graph_builder = builder
    return env, engine


def test_default_engine_records_base_sha_and_unavailable_graph(capture, tmp_path):
    env, engine = run_ticket(tmp_path)
    try:
        assert engine.run(1).ticket.state is State.AWAITING_PLAN_APPROVAL
        base = env.ticket().base_sha
        attrs = root_of(capture)
        assert base and attrs["langfuse.trace.metadata.base_sha"] == base
        assert attrs["devflow.base_sha"] == base
        assert attrs["langfuse.trace.metadata.repo"] == "t"
        assert attrs["devflow.graph_status"] == "unavailable"
        assert "devflow.graph_nodes" not in attrs
        assert all(g["devflow.graph.status"] == "unavailable" and g["devflow.graph.refreshed"] is False
                   for g in graph_spans(capture))
        # resumed run: base sha comes from the stored ticket
        env.say("alice", "/approve")
        env.engine().run(1)
        assert root_of(capture)["devflow.base_sha"] == base
        later = [r for r in rows(capture) if r["name"].startswith("agent.")]
        assert later and all(r["attributes"].get("langfuse.trace.metadata.base_sha") == base
                             for r in later[-1:])
    finally:
        env.store.close()


def test_ok_builder_records_refreshed_counts_and_latest_check_wins(capture, tmp_path):
    def builder(root):
        return GraphResult(True, True, nodes=7, edges=4)
    env, engine = run_ticket(tmp_path, builder)
    try:
        engine.run(1)
        attrs = root_of(capture)
        assert attrs["devflow.graph_status"] in ("refreshed", "fresh")
        assert attrs["devflow.graph_nodes"] == 7 and attrs["devflow.graph_edges"] == 4
        assert len(attrs["devflow.graph_fingerprint"]) == 12
        statuses = [g["devflow.graph.status"] for g in graph_spans(capture)]
        assert statuses[0] == "refreshed"
        assert attrs["devflow.graph_status"] == statuses[-1]
    finally:
        env.store.close()


def test_raising_builder_does_not_break_the_ticket(capture, tmp_path):
    def broken(root):
        raise RuntimeError("secret-token-xyz")
    env, engine = run_ticket(tmp_path, broken)
    baseline = Env(tmp_path / "baseline")
    try:
        state = engine.run(1).ticket.state
        assert state is baseline.engine().run(1).ticket.state is State.AWAITING_PLAN_APPROVAL
        attrs = root_of(capture)
        assert attrs["devflow.graph_status"] == "unavailable"
        assert len(attrs["devflow.graph_fingerprint"]) == 12
        assert "devflow.graph_nodes" not in attrs
        spans = graph_spans(capture)
        assert spans and all(g["devflow.graph.status"] == "unavailable"
                             and g["devflow.graph.refreshed"] is False
                             and len(g["devflow.graph.fingerprint"]) == 12
                             and "devflow.graph.nodes" not in g for g in spans)
        assert "secret-token-xyz" not in capture.read_text()
    finally:
        env.store.close()
        baseline.store.close()


def test_remote_credentials_never_reach_the_trace(capture, tmp_path):
    env, engine = run_ticket(tmp_path, remote="https://user:p4ssw0rd@github.com/owner/repo.git")
    try:
        engine.run(1)
        assert root_of(capture)["devflow.remote"] == "github.com/owner/repo"
        text = capture.read_text()
        assert "p4ssw0rd" not in text and "user:" not in text
    finally:
        env.store.close()


@pytest.mark.parametrize("content", [False, True])
def test_provenance_present_with_and_without_content_capture(tmp_path, content):
    log = tmp_path / "t.jsonl"
    telemetry.configure(settings={"enabled": True, "log_file": str(log), "capture_content": content})
    env, engine = run_ticket(tmp_path, lambda r: GraphResult(True, True, nodes=2, edges=1))
    try:
        engine.run(1)
        attrs = root_of(log)
        meta = {k: v for k, v in attrs.items() if k.startswith("langfuse.trace.metadata.")}
        assert {"base_sha", "graph_status", "graph_fingerprint", "graph_nodes", "graph_edges"} <= {
            k.rsplit(".", 1)[1] for k in meta}
        for value in meta.values():
            assert "multiply" not in str(value) and "calc.py" not in str(value)
    finally:
        env.store.close()
        telemetry.configure(settings={})


# ---- full ticket lifecycle ----------------------------------------------------------------

def _root(slice_):
    roots = [r for r in slice_ if r["name"] == "ticket.run"]
    assert len(roots) == 1 and roots[0]["parent_id"] == ""
    return roots[0]


def _index(slice_):
    return {r["span_id"]: r for r in slice_}


def _named(slice_, name):
    return [r for r in slice_ if r["name"] == name]


def _stage_chain(slice_):
    stages = [r for r in slice_ if r["name"].startswith("stage.") and r["name"] != "stage.result"]
    return sorted(stages, key=lambda r: r["start_ns"])


def _row(name, span_id, start_ns=0, parent_id="", **attrs):
    return {"name": name, "span_id": span_id, "start_ns": start_ns, "parent_id": parent_id,
            "trace_id": "t", "attributes": attrs}


def test_lifecycle_helpers_sort_filter_and_index():
    rows_ = [_row("stage.b", "3", 30, "1"), _row("ticket.run", "1"), _row("stage.result", "9", 5, "3"),
             _row("stage.a", "2", 10, "1")]
    assert [r["name"] for r in _stage_chain(rows_)] == ["stage.a", "stage.b"]
    assert _root(rows_)["span_id"] == "1"
    assert _index(rows_)["2"]["name"] == "stage.a"
    assert [r["span_id"] for r in _named(rows_, "stage.a")] == ["2"]
    with pytest.raises(AssertionError):
        _root([_row("stage.a", "2", 10, "1")])


def test_full_lifecycle_traces_span_three_invocations_with_consistent_parentage(capture, tmp_path):
    env = Env(tmp_path / "ticket")
    try:
        first = env.engine().run(1)
        assert first.status == "paused" and first.ticket.state is State.AWAITING_PLAN_APPROVAL
        assert first.reason.startswith("waiting for /approve or /reject")
        n1 = len(rows(capture))
        env.say("alice", "/approve")
        second = env.engine().run(1)
        assert second.status == "paused" and second.ticket.state is State.AWAITING_MERGE_APPROVAL
        assert second.reason.startswith("waiting for /approve on PR")
        review = env.store.stage_results("#1", "review")[-1]["payload"]
        assert review["agent"] == "codex" and review["independent"] and review["approved"]
        n2 = len(rows(capture))
        env.say("alice", "/approve", number=env.pr())
        third = env.engine().run(1)
        assert third.status == "done" and third.ticket.state is State.MERGED
        assert env.host.merged == [env.pr()]
        history = [h["to"] for h in env.ticket().history]
    finally:
        env.store.close()

    allrows = rows(capture)
    slices = [allrows[:n1], allrows[n1:n2], allrows[n2:]]
    assert all(slices)
    roots = [_root(s) for s in slices]
    assert len({r["trace_id"] for r in roots}) == 3
    for s, root in zip(slices, roots):
        assert {r["trace_id"] for r in s} == {root["trace_id"]}
    assert all(r["attributes"]["langfuse.session.id"] == "t:issue-1" for r in allrows)
    names = {r["name"] for r in allrows}
    assert {"stage.result", "routing.selected", "usage.recorded", "gate.result", "state.transition",
            "tool.git"} <= names

    for s, root in zip(slices, roots):
        index = _index(s)
        assert len(index) == len(s)
        for r in s:
            cur, hops = r, 0
            while cur is not root:
                assert cur["parent_id"] in index
                cur = index[cur["parent_id"]]
                hops += 1
                assert hops < 50

    expected_chains = [
        ["stage.new", "stage.refining", "stage.awaiting_plan_approval"],
        ["stage.awaiting_plan_approval", "stage.test_design", "stage.implementing", "stage.gates",
         "stage.in_review", "stage.awaiting_merge_approval"],
        ["stage.awaiting_merge_approval"],
    ]
    stage_by_name = []
    for s, root, expected in zip(slices, roots, expected_chains):
        chain = _stage_chain(s)
        assert [r["name"] for r in chain] == expected
        assert all(r["parent_id"] == root["span_id"] for r in chain)
        assert all(r["attributes"]["devflow.state"] == r["name"].removeprefix("stage.") for r in chain)
        stage_by_name.append({r["name"]: r for r in chain})
    assert not any(r["name"] == "stage.merged" for r in allrows)

    expected_transitions = [
        [("refining", "stage.new"), ("awaiting_plan_approval", "stage.refining")],
        [("test_design", "stage.awaiting_plan_approval"), ("implementing", "stage.test_design"),
         ("gates", "stage.implementing"), ("in_review", "stage.gates"),
         ("awaiting_merge_approval", "stage.in_review")],
        [("merged", "stage.awaiting_merge_approval")],
    ]
    destinations = []
    for s, stages, expected in zip(slices, stage_by_name, expected_transitions):
        transitions = sorted(_named(s, "state.transition"), key=lambda r: r["start_ns"])
        got = [(r["attributes"]["devflow.state"], _index(s)[r["parent_id"]]["name"]) for r in transitions]
        assert got == expected
        assert all(r["parent_id"] == stages[parent]["span_id"] for r, (_, parent) in zip(transitions, expected))
        destinations += [d for d, _ in got]
    assert destinations == history == ["refining", "awaiting_plan_approval", "test_design", "implementing",
                                       "gates", "in_review", "awaiting_merge_approval", "merged"]

    def agent_span(s, stages, name, stage):
        found = _named(s, name)
        assert len(found) >= 1
        for r in found:
            assert r["parent_id"] == stages[stage]["span_id"]
            assert "devflow.role" in r["attributes"] and r["attributes"]["devflow.ok"] is True
        return found[0]

    agent_span(slices[0], stage_by_name[0], "agent.fake.planner", "stage.refining")
    agent_span(slices[1], stage_by_name[1], "agent.claude.test_planner", "stage.test_design")
    coder = agent_span(slices[1], stage_by_name[1], "agent.claude.coder", "stage.implementing")
    reviewer = agent_span(slices[1], stage_by_name[1], "agent.codex.reviewer", "stage.in_review")
    assert reviewer["name"].split(".")[1] != coder["name"].split(".")[1]
    results = [r for r in _named(slices[1], "stage.result")
               if r["attributes"].get("devflow.artifact") == "review"]
    assert results and all(r["parent_id"] == stage_by_name[1]["stage.in_review"]["span_id"]
                           and r["attributes"]["devflow.reviewer_independent"] is True
                           and r["attributes"]["devflow.ok"] is True for r in results)
    assert not any(r["name"].startswith("agent.") for r in slices[2])
    for s in slices:
        index = _index(s)
        for r in _named(s, "routing.selected") + _named(s, "usage.recorded"):
            # the router emits these in the calling stage's context, not inside the agent span
            assert index[r["parent_id"]]["name"].startswith("stage.")

    gate_stages = stage_by_name[1]
    for stage in ("stage.gates", "stage.in_review"):
        assert any(r["attributes"].get("devflow.gate") == "unit" and r["attributes"].get("devflow.ok") is True
                   and r["parent_id"] == gate_stages[stage]["span_id"]
                   for r in _named(slices[1], "gate.result"))
    for s in slices:
        index = _index(s)
        assert all(index[r["parent_id"]]["name"].startswith("stage.") for r in _named(s, "gate.result"))

    assert roots[0]["status"] == "waiting" and roots[1]["status"] == "waiting" and roots[2]["status"] == "ok"
    expected_status = [
        {"stage.new": "ok", "stage.refining": "ok", "stage.awaiting_plan_approval": "waiting"},
        {"stage.awaiting_plan_approval": "ok", "stage.test_design": "ok", "stage.implementing": "ok",
         "stage.gates": "ok", "stage.in_review": "ok", "stage.awaiting_merge_approval": "waiting"},
        {"stage.awaiting_merge_approval": "ok"},
    ]
    for stages, expected in zip(stage_by_name, expected_status):
        assert {n: r["status"] for n, r in stages.items()} == expected
    assert not any(r["status"] == "waiting" for r in slices[2])
    assert not any(r["status"] == "error" for r in allrows)
