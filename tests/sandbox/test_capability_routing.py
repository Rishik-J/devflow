"""PR2 in the sandbox: work that needs sockets or a browser is never handed to a sandboxed agent."""
from tests.sandbox.harness import GOOD_CODER, GOOD_PLAN_REVIEW
from tests.test_engine import APPROVE, PLAN, TEST_PLAN


def plan_with(**extra):
    return {**PLAN, **extra}


def setup(sb, plan):
    sb.script("openclaw", "plan", {"reply": plan}).script("openclaw", "plan_review", {"reply": GOOD_PLAN_REVIEW})
    sb.script("codex", "test_plan", {"reply": TEST_PLAN}).script("claude", "test_plan", {"reply": TEST_PLAN})
    sb.script("openclaw", "review", {"reply": APPROVE})
    return sb


def test_an_ordinary_ticket_still_uses_the_preferred_restricted_agent(make_sandbox):
    sb = setup(make_sandbox(routing_agents=("codex", "claude")), plan_with())
    sb.script("codex", "coder", GOOD_CODER)
    assert sb.run().status == "done"
    assert len(sb.calls("codex", "coder")) == 1 and sb.calls("claude", "coder") == []
    assert sb.calls("codex", "coder")[0]["sandbox"] == "workspace-write"  # the real driver's sandbox flag


def test_a_plan_that_declares_browser_work_goes_to_claude_even_when_codex_is_preferred(make_sandbox):
    sb = setup(make_sandbox(routing_agents=("codex", "claude")), plan_with(requires=["browser"]))
    sb.script("claude", "coder", GOOD_CODER)
    assert sb.run().status == "done"
    assert sb.calls("codex", "coder") == [] and len(sb.calls("claude", "coder")) == 1


def test_server_files_in_the_plan_imply_sockets_without_the_planner_saying_so(make_sandbox):
    plan = plan_with(steps=[{"id": "S1", "description": "serve", "files": ["src/ui_server.py"]}])
    sb = setup(make_sandbox(routing_agents=("codex", "claude")), plan)
    sb.script("claude", "coder", {"write": GOOD_CODER["write"], "reply": "ok"})
    assert sb.run().status == "done"
    assert sb.calls("codex", "coder") == [] and len(sb.calls("claude", "coder")) == 1


def test_configured_capabilities_override_the_defaults(make_sandbox):
    # An operator who knows their codex sandbox allows sockets can say so.
    sb = setup(make_sandbox(routing_agents=("codex", "claude"), agent_caps={"codex": ["sockets", "browser"]}),
               plan_with(requires=["browser"]))
    sb.script("codex", "coder", GOOD_CODER)
    assert sb.run().status == "done"
    assert len(sb.calls("codex", "coder")) == 1 and sb.calls("claude", "coder") == []


def test_when_no_agent_has_the_capability_the_ticket_escalates_with_the_reason(make_sandbox):
    sb = setup(make_sandbox(routing_agents=("codex", "claude"), agent_caps={"claude": []}),
               plan_with(requires=["sockets"]))
    out = sb.run()
    assert out.status == "escalated" and "no agent can provide ['sockets']" in out.reason
    assert sb.calls("codex", "coder") == [] and sb.calls("claude", "coder") == []
    assert any("capabilities" in c for c in sb.comments(1))  # tells the operator how to fix the config


def test_the_capable_agent_being_limited_pauses_rather_than_falling_back_to_a_sandboxed_one(make_sandbox):
    sb = setup(make_sandbox(routing_agents=("codex", "claude")), plan_with(requires=["sockets"]))
    sb.script("claude", "coder", {"limit": True})
    sb.script("codex", "coder", GOOD_CODER)  # would succeed, but must not be used
    out = sb.run()
    assert out.status == "paused", out.reason
    assert sb.calls("codex", "coder") == []


def test_codex_can_still_do_the_read_only_planning_and_review_work(make_sandbox):
    sb = setup(make_sandbox(routing_agents=("codex", "claude")), plan_with(requires=["sockets"]))
    sb.script("claude", "coder", GOOD_CODER)
    sb.run()
    assert len(sb.calls("codex", "test_plan")) == 1  # capabilities only gate the tasks that declare needs
