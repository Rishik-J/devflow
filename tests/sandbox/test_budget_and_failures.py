"""PR1 in the sandbox: per-ticket budget, usage recording, limit and sandbox-denial handling."""
from devflow.states import State
from tests.sandbox.harness import GOOD_CODER, GOOD_PLAN_REVIEW
from tests.test_engine import APPROVE, PLAN, TEST_PLAN


def base(sb):
    sb.script("openclaw", "plan", {"reply": PLAN}).script("openclaw", "plan_review", {"reply": GOOD_PLAN_REVIEW})
    return sb.script("claude", "test_plan", {"reply": TEST_PLAN, "cost": 0.1})


def test_cost_cap_stops_the_ticket_before_the_next_stage_and_resume_gives_a_fresh_window(make_sandbox):
    sb = base(make_sandbox(limits={"max_ticket_cost_usd": 0.4}))
    sb.script("claude", "coder", GOOD_CODER,  # costs 0.5, taking the ticket over the 0.4 cap
              {"reply": "nothing further needed", "cost": 0.05})
    sb.script("openclaw", "review", {"reply": APPROVE})

    out = sb.run()
    assert out.status == "escalated" and out.ticket.state is State.ESCALATED
    assert "ticket budget exceeded" in out.reason and "$0.60 spent, cap $0.40" in out.reason
    assert sb.calls("openclaw", "review") == []  # stopped before spending on a review
    assert any("ticket budget exceeded" in c and "/resume" in c for c in sb.comments(1))
    # the remaining budget was handed to the agent: cap 0.4 minus the 0.1 test planner already spent
    assert sb.calls("claude", "coder")[0]["budget"] == "0.3"

    sb.say("alice", "/resume")
    resumed = sb.run()
    assert resumed.status == "done", resumed.reason  # a new window: the earlier $0.60 no longer counts
    assert resumed.ticket.sessions["budget_base"].startswith("0.6")


def test_without_a_cap_an_expensive_ticket_just_completes(make_sandbox):
    sb = base(make_sandbox())  # budgets default to off
    sb.script("claude", "coder", {**GOOD_CODER, "cost": 75.0}).script("openclaw", "review", {"reply": APPROVE})
    assert sb.run().status == "done"
    assert sb.calls("claude", "coder")[0]["budget"] == ""  # no budget flag when there is no cap


def test_token_cap_counts_openclaw_tokens_that_used_to_go_unrecorded(make_sandbox):
    sb = make_sandbox(limits={"max_ticket_tokens": 4000})
    sb.script("openclaw", "plan", {"reply": PLAN, "tokens": [4500, 500]})  # OpenClaw reports usage
    sb.script("openclaw", "plan_review", {"reply": GOOD_PLAN_REVIEW})
    out = sb.run()
    assert out.status == "escalated" and "tokens used, cap 4,000" in out.reason
    assert ("openclaw:devflow", "planner", 0.0, 4500, 500) in sb.usage()
    assert sb.calls("claude") == []  # the planner alone exhausted the budget; no coder ran


def test_claudes_real_five_hour_limit_message_cools_it_down_and_codex_takes_over(make_sandbox):
    sb = base(make_sandbox())
    sb.script("claude", "coder", {"limit": True})
    sb.script("codex", "coder", GOOD_CODER).script("openclaw", "review", {"reply": APPROVE})
    out = sb.run()
    assert out.status == "done", out.reason
    with sb.store() as store:
        cooldown = store.get_cooldown("claude")
    assert cooldown and "5-hour limit" in cooldown["reason"] and cooldown["strikes"] == 1
    assert len(sb.calls("claude", "coder")) == 1 and len(sb.calls("codex", "coder")) == 1


def test_every_agent_limited_pauses_instead_of_escalating(make_sandbox):
    sb = base(make_sandbox())
    sb.script("claude", "coder", {"limit": True})
    sb.script("codex", "coder", {"fail": "Error: usage limit reached, try again later"})
    out = sb.run()
    assert out.status == "paused" and out.ticket.state is State.IMPLEMENTING
    assert "no coding agent available" in out.reason or "limit" in out.reason


def test_a_sandbox_blocked_only_agent_escalates_at_once_with_an_actionable_reason(make_sandbox):
    sb = base(make_sandbox(routing_agents=("codex",)))
    sb.script("codex", "test_plan", {"reply": TEST_PLAN})
    sb.script("codex", "coder", {"sandbox_denied": True}, {"sandbox_denied": True})
    out = sb.run()
    assert out.status == "escalated" and "sandbox blocked the work" in out.reason
    assert len(sb.calls("codex", "coder")) == 1  # one attempt, not a retry loop
    assert out.ticket.implement_attempts == 0
    assert any("sandbox" in c for c in sb.comments(1))


def test_sandbox_blocked_codex_hands_the_work_to_claude_which_has_sockets(make_sandbox):
    sb = make_sandbox(routing_agents=("codex", "claude"))  # codex has the higher priority
    sb.script("openclaw", "plan", {"reply": PLAN}).script("openclaw", "plan_review", {"reply": GOOD_PLAN_REVIEW})
    sb.script("codex", "test_plan", {"reply": TEST_PLAN})
    sb.script("codex", "coder", {"sandbox_denied": True})
    sb.script("claude", "coder", GOOD_CODER).script("openclaw", "review", {"reply": APPROVE})
    out = sb.run()
    assert out.status == "done", out.reason
    assert len(sb.calls("codex", "coder")) == 1 and len(sb.calls("claude", "coder")) == 1
    assert sb.ticket().sessions["needs"] == "sockets"
    assert sb.events("capability_retry")[0]["agent"] == "codex"


def test_a_codex_process_that_fails_with_a_sandbox_error_escalates_without_a_retry(make_sandbox):
    sb = base(make_sandbox(routing_agents=("codex",)))
    sb.script("codex", "test_plan", {"reply": TEST_PLAN})
    sb.script("codex", "coder", {"fail": "operation not permitted: cannot bind socket 127.0.0.1:0"})
    out = sb.run()
    assert out.status == "escalated" and "sandbox" in out.reason
    assert len(sb.calls("codex", "coder")) == 1


def test_a_genuine_failure_is_not_mistaken_for_a_sandbox_denial(make_sandbox):
    sb = base(make_sandbox())
    sb.script("claude", "coder", {"fail": "AssertionError: permission bits expected 0o600, got 0o644"},
              {"fail": "AssertionError: permission bits expected 0o600, got 0o644"})
    first = sb.run()
    assert first.status == "paused" and "will retry" in first.reason  # ordinary 2-strike handling
    assert sb.run().status == "escalated"
    assert len(sb.calls("claude", "coder")) == 2
