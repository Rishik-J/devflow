"""PR3 in the sandbox: a broken environment pauses the ticket before any agent runs; outages recover."""
import os

from devflow.states import State
from tests.sandbox.harness import GOOD_CODER, GOOD_PLAN_REVIEW
from tests.test_engine import APPROVE, PLAN, TEST_PLAN


def low_quota(sb, **buckets):
    base = {"core": {"remaining": 4000, "reset": 9_999_999_999}, "graphql": {"remaining": 4000, "reset": 9_999_999_999}}
    base.update(buckets)
    sb.set_gh(rate_limit=base)


def test_exhausted_github_quota_pauses_before_any_agent_runs_and_recovers(make_sandbox):
    sb = make_sandbox().happy()
    low_quota(sb, core={"remaining": 3, "reset": 9_999_999_999})
    out = sb.run()
    assert out.status == "paused" and "preflight failed: github rate limit" in out.reason
    assert out.ticket.state is State.NEW
    assert sb.agent_calls() == []  # nothing ran: no money was spent on a doomed ticket
    low_quota(sb)  # the quota resets
    assert sb.run().status == "done"


def test_gh_logged_out_pauses_with_the_fix(make_sandbox):
    sb = make_sandbox().happy()
    sb.set_gh(auth_ok=False)
    out = sb.run()
    assert out.status == "paused" and "gh authenticated" in out.reason and "gh auth login" in out.reason
    assert sb.agent_calls() == []


def test_the_stop_file_holds_a_new_ticket(make_sandbox):
    sb = make_sandbox().happy()
    (sb.repo / ".devflow" / "STOP").write_text("")
    out = sb.run()
    assert out.status == "paused" and "stop file" in out.reason
    (sb.repo / ".devflow" / "STOP").unlink()
    assert sb.run().status == "done"


def test_a_missing_openclaw_binary_is_caught_by_preflight_not_mid_ticket(make_sandbox):
    sb = make_sandbox().happy()
    os.remove(sb.bin / "openclaw")
    out = sb.run()
    assert out.status == "paused" and "openclaw is not on PATH" in out.reason
    assert out.ticket.state is State.NEW and sb.agent_calls() == []


def test_a_non_blocking_failure_does_not_stop_work(make_sandbox):
    # codex missing from PATH is a warning (claude is enough), not a blocker
    sb = make_sandbox().happy()
    os.remove(sb.bin / "codex")
    assert sb.run().status == "done"


def test_an_openclaw_outage_pauses_with_a_counter_and_recovers_like_ticket_63_should_have(make_sandbox):
    sb = make_sandbox()
    outage = {"outage": True}
    # Each planner call fails twice (the real driver retries once); three runs hit the outage.
    sb.script("openclaw", "plan", *[outage] * 6, {"reply": PLAN})
    sb.script("openclaw", "plan_review", {"reply": GOOD_PLAN_REVIEW})
    sb.script("claude", "test_plan", {"reply": TEST_PLAN}).script("claude", "coder", GOOD_CODER)
    sb.script("openclaw", "review", {"reply": APPROVE})
    seen = []
    for _ in range(4):
        out = sb.run()
        seen.append(out.reason)
        if out.status == "done":
            break
        assert out.status == "paused"
    assert "(1/8)" in seen[0] and "(2/8)" in seen[1] and "(3/8)" in seen[2]
    assert "infrastructure problem" in seen[0]
    assert len(sb.events("infra_error")) == 3
    assert out.status == "done"  # it recovered by itself: no /resume, no escalation
    assert all(e["to"] != "escalated" for e in sb.events("transition"))


def test_a_persistent_outage_eventually_escalates_with_the_cause(make_sandbox):
    sb = make_sandbox()
    sb.script("openclaw", "plan", {"outage": True, "sticky": True})
    out = None
    for _ in range(9):
        out = sb.run()
        if out.status == "escalated":
            break
    assert out.status == "escalated" and "infrastructure kept failing" in out.reason
    assert any("kept failing" in c for c in sb.comments(1))


def test_a_github_rate_limit_on_reading_the_issue_pauses_instead_of_escalating(make_sandbox):
    sb = make_sandbox().happy()
    sb.set_gh(rate_limited=True)
    out = sb.run()
    assert out.status == "paused" and "infrastructure problem" in out.reason
    sb.set_gh(rate_limited=False)
    assert sb.run().status == "done"


def test_an_ordinary_planner_failure_keeps_the_two_strike_rule(make_sandbox):
    sb = make_sandbox()
    sb.script("openclaw", "plan", {"fail": "schema validation exploded"}, {"fail": "schema validation exploded"},
              {"fail": "schema validation exploded"}, {"fail": "schema validation exploded"})
    assert sb.run().status == "paused"
    assert sb.run().status == "escalated"  # not 8 attempts: only infrastructure gets patience


def test_doctor_reports_repository_checks_through_the_real_cli(make_sandbox):
    sb = make_sandbox()
    healthy = sb.cli("doctor", str(sb.repo))
    assert "repository checks" in healthy.stdout and "worktree disk" in healthy.stdout
    assert healthy.returncode == 0, healthy.stdout
    sb.set_gh(auth_ok=False)
    broken = sb.cli("doctor", str(sb.repo))
    assert broken.returncode == 1 and "FAILED" in broken.stdout
