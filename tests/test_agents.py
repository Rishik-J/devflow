import json
import os
import sys
from pathlib import Path

import pytest

from devflow.agents import (
    AgentError,
    AgentRequest,
    AgentResult,
    AgentTimeout,
    ClaudeAgent,
    CodexAgent,
    FakeAgent,
    OpenClawAgent,
    extract_json,
    make_agent,
    run_structured,
)
from devflow.agents.base import ProcResult, agent_env, run_process

READY = {
    "status": "ready", "goal": "g",
    "acceptance_criteria": [{"id": "AC1", "text": "t"}],
    "steps": [{"id": "S1", "description": "d", "files": ["a.py"]}],
}


def req(tmp_path: Path, **kw) -> AgentRequest:
    kw.setdefault("role", "planner")
    kw.setdefault("prompt", "do the thing")
    return AgentRequest(cwd=tmp_path, **kw)


class Recorder:
    """A Runner that returns a canned ProcResult and remembers the command it was given."""

    def __init__(self, *results: ProcResult):
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, cmd, cwd, env, timeout_s, stdin_text):
        self.calls.append({"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout_s,
                           "stdin": stdin_text})
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


# ---------------------------------------------------------------- extract_json


@pytest.mark.parametrize("text,expected", [
    ('{"ok": true}', {"ok": True}),
    ('Sure!\n```json\n{"a": 1}\n```\nDone.', {"a": 1}),
    ('Here you go: {"a": {"b": [1, 2]}} hope that helps', {"a": {"b": [1, 2]}}),
    ('[1, 2, 3]', [1, 2, 3]),
    ('noise { not json } then {"x": 2}', {"x": 2}),
])
def test_extract_json(text, expected):
    assert extract_json(text) == expected


@pytest.mark.parametrize("text", ["", "no json here", "{broken", "just a number 5"])
def test_extract_json_none(text):
    assert extract_json(text) is None


# ---------------------------------------------------------------- claude

CLAUDE_OK = json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "result": '{"ok": true}',
    "session_id": "sess-1", "total_cost_usd": 0.0836, "duration_ms": 1737, "num_turns": 1,
})


def test_claude_command_read_only_new_session(tmp_path: Path):
    r = Recorder(ProcResult(0, CLAUDE_OK, ""))
    out = ClaudeAgent(runner=r).run(
        req(tmp_path, system="You are the planner.", model="opus", max_budget_usd=1.5,
            mcp={"mcpServers": {"graphify": {"command": "g"}}}))
    cmd, call = r.calls[0]["cmd"], r.calls[0]
    assert cmd[:4] == ["claude", "-p", "--output-format", "json"]
    assert "--session-id" in cmd and "--resume" not in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert "Edit" not in allowed and "Write" not in allowed and "Read" in allowed
    assert "--permission-mode" not in cmd
    assert "Bash(git push:*)" in cmd[cmd.index("--disallowedTools") + 1]
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "1.5"
    assert "--strict-mcp-config" in cmd and "--mcp-config" in cmd
    assert "--append-system-prompt-file" in cmd
    assert call["stdin"] == "do the thing" and call["cwd"] == tmp_path
    assert out.ok and out.text == '{"ok": true}' and out.session == "sess-1"
    assert out.cost_usd == pytest.approx(0.0836) and out.duration_s == pytest.approx(1.737)


def test_claude_write_mode_and_resume(tmp_path: Path):
    r = Recorder(ProcResult(0, CLAUDE_OK, ""))
    ClaudeAgent(runner=r).run(req(tmp_path, role="coder", read_only=False, session="abc"))
    cmd = r.calls[0]["cmd"]
    assert cmd[cmd.index("--resume") + 1] == "abc" and "--session-id" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert "Edit" in cmd[cmd.index("--allowedTools") + 1].split(",")


def test_claude_temp_files_are_removed_and_deny_list_cannot_be_dropped(tmp_path: Path):
    r = Recorder(ProcResult(0, CLAUDE_OK, ""))
    ClaudeAgent(runner=r).run(req(tmp_path, system="s", mcp={"mcpServers": {}}))
    cmd = r.calls[0]["cmd"]
    assert not Path(cmd[cmd.index("--append-system-prompt-file") + 1]).exists()
    assert not Path(cmd[cmd.index("--mcp-config") + 1]).exists()
    assert "Bash(gh:*)" in cmd[cmd.index("--disallowedTools") + 1]


def test_claude_error_result_is_not_ok(tmp_path: Path):
    bad = json.dumps({"subtype": "error_max_budget_usd", "is_error": True,
                      "result": "budget exceeded", "session_id": "s"})
    out = ClaudeAgent(runner=Recorder(ProcResult(1, bad, ""))).run(req(tmp_path))
    assert not out.ok and "error_max_budget_usd" in out.error


def test_claude_garbage_and_timeout(tmp_path: Path):
    with pytest.raises(AgentError, match="unreadable"):
        ClaudeAgent(runner=Recorder(ProcResult(1, "oops", "stderr text"))).run(req(tmp_path))
    with pytest.raises(AgentTimeout):
        ClaudeAgent(runner=Recorder(ProcResult(None, "", "", timed_out=True))).run(req(tmp_path))


# ---------------------------------------------------------------- codex

CODEX_OUT = (
    '{"type":"thread.started","thread_id":"thr-9"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"working"}}\n'
    '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"{\\"ok\\": true}"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}'
)


def test_codex_command_and_parse(tmp_path: Path):
    r = Recorder(ProcResult(0, CODEX_OUT, ""))
    out = CodexAgent(runner=r).run(req(tmp_path, role="reviewer", system="Be strict.",
                                       mcp_overrides=["mcp_servers.graphify.command=\"g\""]))
    cmd = r.calls[0]["cmd"]
    assert cmd[:2] == ["codex", "exec"] and "resume" not in cmd
    assert cmd[cmd.index("-s") + 1] == "read-only" and cmd[cmd.index("-C") + 1] == str(tmp_path)
    assert "--json" in cmd and "-o" in cmd
    assert 'mcp_servers.graphify.command="g"' in cmd
    assert cmd[-1].startswith("Be strict.") and cmd[-1].endswith("do the thing")
    assert r.calls[0]["stdin"] is None
    assert out.ok and out.text == '{"ok": true}' and out.session == "thr-9"
    assert out.raw["usage"]["input_tokens"] == 10


def test_codex_resume_uses_config_for_sandbox(tmp_path: Path):
    r = Recorder(ProcResult(0, CODEX_OUT, ""))
    CodexAgent(runner=r).run(req(tmp_path, role="coder", read_only=False, session="thr-9"))
    cmd = r.calls[0]["cmd"]
    assert cmd[2:4] == ["resume", "thr-9"] and "-s" not in cmd and "-C" not in cmd
    assert 'sandbox_mode="workspace-write"' in cmd


def test_codex_error_event_and_failure(tmp_path: Path):
    events = '{"type":"thread.started","thread_id":"t"}\n{"type":"error","message":"rate limited"}'
    out = CodexAgent(runner=Recorder(ProcResult(0, events, ""))).run(req(tmp_path))
    assert not out.ok and "rate limited" in out.error
    with pytest.raises(AgentError, match="exit 2"):
        CodexAgent(runner=Recorder(ProcResult(2, "", "boom"))).run(req(tmp_path))
    with pytest.raises(AgentTimeout):
        CodexAgent(runner=Recorder(ProcResult(None, "", "", timed_out=True))).run(req(tmp_path))


# ---------------------------------------------------------------- openclaw

OPENCLAW_OK = json.dumps({
    "runId": "r1", "status": "ok", "summary": "completed",
    "result": {"payloads": [{"text": '{"ok": true}', "mediaUrl": None}],
               "meta": {"durationMs": 3257}},
})


def test_openclaw_command_uses_isolated_session_and_names_checkout(tmp_path: Path):
    r = Recorder(ProcResult(0, OPENCLAW_OK, ""))
    out = OpenClawAgent("planner", runner=r).run(req(tmp_path, system="Role text", timeout_s=100))
    cmd = r.calls[0]["cmd"]
    assert cmd[:4] == ["openclaw", "agent", "--agent", "planner"]
    key = cmd[cmd.index("--session-key") + 1]
    assert key.startswith("agent:planner:devflow-") and key.endswith("-planner")
    assert key != "agent:planner:main"
    assert cmd[cmd.index("--timeout") + 1] == "100" and "--json" in cmd
    assert r.calls[0]["timeout"] == 130
    assert out.ok and out.text == '{"ok": true}' and out.session == key
    assert out.duration_s == pytest.approx(3.257)


def test_openclaw_message_contents_and_resume_key(tmp_path: Path):
    agent = OpenClawAgent("main", runner=Recorder(ProcResult(0, OPENCLAW_OK, "")))
    msg = agent.message(req(tmp_path, system="Role text"))
    assert msg.startswith("Role text") and f"checkout for this task: {tmp_path}" in msg
    assert agent.session_key(req(tmp_path, session="agent:main:keep")) == "agent:main:keep"


def test_openclaw_retries_once_then_succeeds(tmp_path: Path):
    sleeps: list[float] = []
    r = Recorder(ProcResult(1, "", "gateway stalled"), ProcResult(0, OPENCLAW_OK, ""))
    agent = OpenClawAgent(runner=r, retries=1, retry_delay_s=7, sleep=sleeps.append)
    out = agent.run(req(tmp_path))
    assert out.ok and len(r.calls) == 2 and sleeps == [7]
    keys = {c["cmd"][c["cmd"].index("--session-key") + 1] for c in r.calls}
    assert len(keys) == 1  # a retry continues the same session


def test_openclaw_gives_up_after_retries(tmp_path: Path):
    r = Recorder(ProcResult(1, "", "down"))
    agent = OpenClawAgent(runner=r, retries=2, sleep=lambda s: None)
    with pytest.raises(AgentError, match="unreadable"):
        agent.run(req(tmp_path))
    assert len(r.calls) == 3


def test_openclaw_non_ok_status(tmp_path: Path):
    bad = json.dumps({"status": "error", "summary": "model unavailable", "result": {}})
    out = OpenClawAgent(runner=Recorder(ProcResult(0, bad, ""))).run(req(tmp_path))
    assert not out.ok and "model unavailable" in out.error


# ---------------------------------------------------------------- run_structured


def test_structured_happy_path_returns_parsed_data(tmp_path: Path):
    agent = FakeAgent({"planner": [f"Here is the plan:\n```json\n{json.dumps(READY)}\n```"]})
    out = run_structured(agent, req(tmp_path, schema="plan"))
    assert out.data == READY and len(agent.calls) == 1


def test_structured_repairs_in_the_same_session(tmp_path: Path):
    agent = FakeAgent({"planner": [{"status": "ready"}, READY]})
    out = run_structured(agent, req(tmp_path, schema="plan"))
    assert out.data == READY and len(agent.calls) == 2
    repair = agent.calls[1]
    assert repair.session == "fake-planner"
    assert "goal" in repair.prompt and "ONLY the corrected JSON" in repair.prompt


def test_structured_repair_without_session_restates_the_reply(tmp_path: Path):
    bad = AgentResult(text='{"status": "ready"}', session=None)
    agent = FakeAgent({"planner": [bad, READY]})
    run_structured(agent, req(tmp_path, schema="plan"))
    assert 'Your previous reply was:\n{"status": "ready"}' in agent.calls[1].prompt
    assert agent.calls[1].prompt.startswith("do the thing")


def test_structured_gives_up_after_max_repairs(tmp_path: Path):
    agent = FakeAgent({"planner": ["not json"] * 5})
    with pytest.raises(AgentError, match="still invalid after 2 repairs"):
        run_structured(agent, req(tmp_path, schema="plan"), max_repairs=2)
    assert len(agent.calls) == 3


def test_structured_failed_run_is_not_repaired(tmp_path: Path):
    agent = FakeAgent({"planner": [AgentResult(text="", ok=False, error="budget")]})
    with pytest.raises(AgentError, match="budget"):
        run_structured(agent, req(tmp_path, schema="plan"))
    assert len(agent.calls) == 1


def test_structured_requires_schema(tmp_path: Path):
    with pytest.raises(ValueError):
        run_structured(FakeAgent(), req(tmp_path))


# ---------------------------------------------------------------- fake + factory + process


def test_fake_agent_script_exhaustion_and_exceptions(tmp_path: Path):
    agent = FakeAgent({"coder": ["a", AgentError("boom")]})
    assert agent.run(req(tmp_path, role="coder")).text == "a"
    with pytest.raises(AgentError, match="boom"):
        agent.run(req(tmp_path, role="coder"))
    with pytest.raises(AgentError, match="no scripted reply"):
        agent.run(req(tmp_path, role="coder"))
    assert agent.roles() == ["coder", "coder", "coder"]


def test_fake_agent_callable(tmp_path: Path):
    agent = FakeAgent(lambda r: {"echo": r.role})
    assert json.loads(agent.run(req(tmp_path, role="x")).text) == {"echo": "x"}


def test_make_agent():
    assert isinstance(make_agent("claude"), ClaudeAgent)
    assert isinstance(make_agent("codex"), CodexAgent)
    oc = make_agent("openclaw:planner")
    assert isinstance(oc, OpenClawAgent) and oc.agent_id == "planner"
    assert make_agent("openclaw").agent_id == "main"
    assert isinstance(make_agent("fake"), FakeAgent)
    with pytest.raises(AgentError, match="unknown agent"):
        make_agent("gpt")


def test_agent_env_keeps_only_the_agents_own_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    env = agent_env(("ANTHROPIC_",))
    assert env["ANTHROPIC_API_KEY"] == "a"
    assert "OPENAI_API_KEY" not in env and "AWS_SECRET_ACCESS_KEY" not in env


def test_run_process_captures_streams_separately(tmp_path: Path):
    code = "import sys; print('out'); print('err', file=sys.stderr)"
    p = run_process([sys.executable, "-c", code], tmp_path, dict(os.environ), 20, None)
    assert p.code == 0 and p.out.strip() == "out" and p.err.strip() == "err"


def test_run_process_feeds_stdin(tmp_path: Path):
    p = run_process([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                    tmp_path, dict(os.environ), 20, "hello")
    assert p.out.strip() == "HELLO"


def test_run_process_timeout_kills_group(tmp_path: Path):
    p = run_process(["/bin/sh", "-c", "sleep 30"], tmp_path, dict(os.environ), 1, None)
    assert p.timed_out and p.code is None
