import json

import pytest

from devflow.cli import main
from devflow.telemetry_cli import LABELS, issue_summary
from tests.conftest import git_init

S = 1_000_000_000


def span(name, start=0, end=1, session="repo:issue-1", status="ok", trace="t1", **attrs):
    attributes = dict(attrs)
    if session is not None:
        attributes["langfuse.session.id"] = session
    return {"name": name, "trace_id": trace, "start_ns": start * S, "end_ns": end * S,
            "status": status, "attributes": attributes}


def run_metrics(tmp_path, capsys, records, *extra, raw=None):
    root = git_init(tmp_path / "repo")
    if records is not None or raw is not None:
        lines = raw if raw is not None else [json.dumps(r) for r in records]
        log = root / ".devflow/state/traces.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join(lines) + "\n")
    code = main(["telemetry", "metrics", str(root), *extra])
    return code, capsys.readouterr()


def test_no_argument_output_unchanged(tmp_path, capsys):
    records = [
        span("ticket.run", 0, 10, **{"devflow.result": "done"}),
        span("stage.plan", 0, 2), span("stage.plan", 2, 6), span("stage.result", 0, 1),
        span("agent.claude.coder", 0, 3, **{"devflow.agent": "claude"}),
        span("usage.recorded", **{"devflow.agent": "claude", "devflow.cost_usd": 0.5}),
        span("x", **{"langfuse.trace.metadata.synthetic": "true"}),
    ]
    code, out = run_metrics(tmp_path, capsys, records)
    assert code == 0
    assert json.loads(out.out) == {
        "spans": 7, "synthetic_spans_excluded": 1, "ticket_turn_outcomes": {"done": 1},
        "agent_cli_calls": {"claude": 1}, "coding_cost_usd_recorded": {"claude": 0.5},
        "stage_latency_seconds": {"stage.plan": {"count": 2, "mean": 3.0, "max": 4.0}}}


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_issue_exits_nonzero(tmp_path, capsys, value):
    root = git_init(tmp_path / "repo")
    try:
        code = main(["telemetry", "metrics", str(root), "--issue", value])
    except SystemExit as exc:
        code = exc.code
    assert code not in (0, None)


@pytest.mark.parametrize("action", ["status", "replay", "demo"])
def test_issue_only_on_metrics(tmp_path, action):
    with pytest.raises(SystemExit):
        main(["telemetry", action, str(tmp_path), "--issue", "1"])


def test_issue_filtering_and_prefix_collision():
    records = [span("ticket.run", session="repo:issue-1"), span("ticket.run", session="repo:issue-12"),
               span("ticket.run", session="repo:issue-2"), span("ticket.run", session="other:issue-1"),
               span("stage.plan", session=None, **{"devflow.issue": 1})]
    sessions = issue_summary(records, 1)
    assert [s["session"] for s in sessions] == ["issue-1", "other:issue-1", "repo:issue-1"]
    assert {s["session"]: s["spans"] for s in sessions}["repo:issue-1"] == 1
    assert [s["session"] for s in issue_summary(records, 12)] == ["repo:issue-12"]


def test_synthetic_excluded_and_counted(tmp_path, capsys):
    records = [span("ticket.run"),
               span("demo.x", session="devflow-connectivity-demo"),
               span("ticket.run", **{"langfuse.trace.metadata.synthetic": "true"})]
    code, out = run_metrics(tmp_path, capsys, records, "--issue", "1")
    data = json.loads(out.out)
    assert code == 0
    assert data["synthetic_spans_excluded"] == 2
    assert data["sessions"][0]["runs"] == 1 and data["spans"] == 1


def test_resumed_runs_single_session():
    records = [span("ticket.run", 0, 10, trace="a", **{"devflow.result": "done"}),
               span("ticket.run", 20, 30, trace="b", status="error"),
               span("ticket.run", 40, 50, trace="c", **{"devflow.result": "blocked"})]
    (session,) = issue_summary(records, 1)
    assert session["runs"] == 3
    assert session["outcomes"] == {"done": 1, "error": 1, "blocked": 1}


def run_times(*pairs):
    (s,) = issue_summary([span("ticket.run", a, b) for a, b in pairs], 1)
    return s["active_run_seconds"], s["elapsed_wall_seconds"], s["inferred_gap_seconds"]


def test_time_semantics():
    assert run_times((0, 10), (30, 40)) == (20.0, 40.0, 20.0)
    assert run_times((0, 10), (5, 15)) == (15.0, 15.0, 0.0)
    assert run_times((0, 20), (5, 10)) == (20.0, 20.0, 0.0)
    assert run_times((0, 10)) == (10.0, 10.0, 0.0)
    assert LABELS["inferred_gap_seconds"].startswith("inferred")
    assert LABELS["active_run_seconds"].startswith("observed")
    assert LABELS["elapsed_wall_seconds"].startswith("observed")


def test_stage_and_agent_stats():
    records = [span("stage.plan", 0, 2), span("stage.plan", 0, 4), span("stage.build", 0, 6),
               span("stage.result", 0, 9),
               span("agent.claude.coder", 0, 2), span("agent.claude.coder", 0, 4),
               span("agent.codex.reviewer", 0, 1)]
    (s,) = issue_summary(records, 1)
    assert s["stage_latency_seconds"] == {"stage.plan": {"count": 2, "mean": 3.0, "max": 4.0},
                                          "stage.build": {"count": 1, "mean": 6.0, "max": 6.0}}
    assert s["agent_latency_seconds"]["claude.coder"] == {"count": 2, "mean": 3.0, "max": 4.0}
    assert s["agent_calls"] == {"claude.coder": 2, "codex.reviewer": 1}


def test_privacy(tmp_path, capsys):
    records = [span("ticket.run", **{"langfuse.observation.input": "SECRET-PROMPT-TEXT",
                                     "langfuse.observation.output": "SECRET-OUTPUT",
                                     "devflow.prompt_hash": "hash-9f9f", "devflow.agent_session": "sess-abc123"}),
               span("agent.claude.coder", **{"langfuse.observation.input": "SECRET-PROMPT-TEXT",
                                             "devflow.agent_session_id": "sess-abc123",
                                             "devflow.agent": "PRIVATE-ATTRIBUTE-VALUE"}),
               span("usage.recorded", **{"devflow.agent": "PRIVATE-ATTRIBUTE-VALUE", "devflow.cost_usd": 1})]
    code, out = run_metrics(tmp_path, capsys, records, "--issue", "1")
    assert code == 0
    for secret in ("SECRET-PROMPT-TEXT", "SECRET-OUTPUT", "PRIVATE-ATTRIBUTE-VALUE", "hash-9f9f", "sess-abc123"):
        assert secret not in out.out


def test_empty_missing_and_unrelated(tmp_path, capsys):
    code, out = run_metrics(tmp_path, capsys, None, "--issue", "5")
    data = json.loads(out.out)
    assert code == 0 and data["spans"] == 0 and data["sessions"] == []
    code, out = run_metrics(tmp_path / "a", capsys, [], "--issue", "5", raw=[])
    assert code == 0 and json.loads(out.out)["sessions"] == []
    code, out = run_metrics(tmp_path / "b", capsys, [span("ticket.run", session="repo:issue-1")], "--issue", "5")
    assert code == 0 and json.loads(out.out)["sessions"] == []


def test_malformed_and_incomplete_records(tmp_path, capsys):
    raw = ["not json", "", json.dumps(span("ticket.run", 0, 10)), "[1, 2]",
           json.dumps({"name": "ticket.run", "start_ns": 0,
                       "attributes": {"langfuse.session.id": "repo:issue-1"}}),
           json.dumps({"name": "stage.plan", "status": "ok", "start_ns": "x", "end_ns": -5,
                       "attributes": {"langfuse.session.id": "repo:issue-1"}}),
           json.dumps({"name": "stage.plan", "start_ns": 5 * S, "end_ns": 1 * S,
                       "attributes": {"langfuse.session.id": "repo:issue-1"}}),
           json.dumps({"name": "ticket.run", "devflow.issue": 1})]
    code, out = run_metrics(tmp_path, capsys, None, "--issue", "1", raw=raw)
    assert code == 0
    (session,) = json.loads(out.out)["sessions"]
    assert session["runs"] == 2 and session["active_run_seconds"] == 10.0
    assert session["stage_latency_seconds"] == {}


def test_non_finite_timestamps_yield_strict_json(tmp_path, capsys):
    attrs = '"attributes": {"langfuse.session.id": "repo:issue-1"}'

    def line(name, start, end):
        return f'{{"name": "{name}", "start_ns": {start}, "end_ns": {end}, {attrs}}}'

    raw = [line("ticket.run", 0, "1e309"), line("stage.plan", "NaN", 5),
           line("stage.plan", 0, "9" * 400), json.dumps(span("ticket.run", 0, 10))]
    code, out = run_metrics(tmp_path, capsys, None, "--issue", "1", raw=raw)
    assert code == 0
    data = json.loads(out.out, parse_constant=lambda c: pytest.fail(c))
    (session,) = data["sessions"]
    assert session["runs"] == 2 and session["active_run_seconds"] == 10.0
    assert session["stage_latency_seconds"] == {}
