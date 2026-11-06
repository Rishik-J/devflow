"""The core ticket loop in the sandbox: happy paths, human gates, review loop, gate failures, CLI."""
from devflow.states import State
from tests.sandbox.harness import GOOD_CODER, GOOD_PLAN_REVIEW
from tests.test_engine import APPROVE, CALC_MULT, PLAN, TEST_PLAN


def test_happy_path_is_idempotent_once_merged(make_sandbox):
    sb = make_sandbox().happy()
    assert sb.run().status == "done"
    before = len(sb.agent_calls())
    again = sb.run()
    assert again.status == "done" and again.ticket.state is State.MERGED
    assert len(sb.agent_calls()) == before  # a finished ticket never wakes an agent


def test_human_gates_pause_then_continue_only_for_an_approver(make_sandbox):
    sb = make_sandbox(plan_gate=True, merge_gate=True)
    sb.script("openclaw", "plan", {"reply": PLAN}).script("claude", "test_plan", {"reply": TEST_PLAN})
    sb.script("claude", "coder", GOOD_CODER).script("openclaw", "review", {"reply": APPROVE})

    first = sb.run()
    assert first.status == "paused" and first.ticket.state is State.AWAITING_PLAN_APPROVAL
    assert sb.calls("claude") == []  # nothing is built before the plan is approved
    assert any("plan" in c.lower() for c in sb.comments(1))  # the plan was posted for approval

    sb.say("mallory", "/approve")  # not an approver
    assert sb.run().ticket.state is State.AWAITING_PLAN_APPROVAL

    sb.say("alice", "/approve")
    paused = sb.run()
    assert paused.status == "paused" and paused.ticket.state is State.AWAITING_MERGE_APPROVAL
    assert sb.gh_state().get("merges") is None  # not merged without merge approval

    pr = sb.ticket().sessions["pr"]
    sb.say("alice", "/approve", number=int(pr))
    done = sb.run()
    assert done.status == "done" and sb.gh_state()["merges"][0]["pr"] == pr


def test_a_rejected_plan_goes_back_to_the_planner_with_the_reason(make_sandbox):
    sb = make_sandbox()
    sb.script("openclaw", "plan", {"reply": PLAN}, {"reply": PLAN})
    sb.script("openclaw", "plan_review", {"reply": {"decision": "reject", "reason": "AC1 is not testable"}},
              {"reply": GOOD_PLAN_REVIEW})
    sb.script("claude", "test_plan", {"reply": TEST_PLAN}).script("claude", "coder", GOOD_CODER)
    sb.script("openclaw", "review", {"reply": APPROVE})
    assert sb.run().status == "done"
    planner_calls = sb.calls("openclaw", "plan")
    assert len(planner_calls) == 2 and "AC1 is not testable" in planner_calls[1]["message"]


def test_review_changes_requested_feeds_findings_to_the_coder_and_then_merges(make_sandbox):
    finding = {"id": "R1", "severity": "major", "blocking": True, "file": "src/calc.py", "line": 3,
               "summary": "multiply ignores negative numbers", "suggested_fix": "handle sign"}
    sb = make_sandbox()
    sb.script("openclaw", "plan", {"reply": PLAN}).script("openclaw", "plan_review", {"reply": GOOD_PLAN_REVIEW})
    sb.script("claude", "test_plan", {"reply": TEST_PLAN})
    sb.script("claude", "coder", GOOD_CODER,
              {"write": {"src/calc.py": CALC_MULT + "\n# handles sign\n"}, "reply": "fixed", "cost": 0.2})
    sb.script("openclaw", "review", {"reply": {"verdict": "request_changes", "findings": [finding],
                                                "integration": {"ran": False, "passed": False}}},
              {"reply": APPROVE})
    out = sb.run()
    assert out.status == "done", out.reason
    coder_calls = sb.calls("claude", "coder")
    assert len(coder_calls) == 2 and "multiply ignores negative numbers" in coder_calls[1]["prompt"]
    assert sb.ticket().review_round == 1
    assert [r["verdict"] for r in [s["payload"] for s in sb.store().stage_results("#1", "review")]] == [
        "request_changes", "approve"]


def test_failing_gates_are_repaired_then_escalate_at_the_attempt_cap(make_sandbox):
    def broken(n):
        return {"write": {"src/calc.py": f"def add(a, b):\n    return a - b  # attempt {n}\n"},
                "reply": f"attempt {n}", "cost": 0.1}

    retry = {"reply": {"decision": "retry", "reason": "x", "message_to_coder": "look closer"}}
    sb = make_sandbox()  # the sandbox config allows 2 implementation attempts
    sb.script("openclaw", "plan", {"reply": PLAN}).script("openclaw", "plan_review", {"reply": GOOD_PLAN_REVIEW})
    sb.script("claude", "test_plan", {"reply": TEST_PLAN})
    sb.script("claude", "coder", *[broken(n) for n in range(1, 6)])
    sb.script("openclaw", "triage", retry, retry, retry)
    out = sb.run()
    for _ in range(3):  # a retry decision pauses the ticket once; keep driving it like `--watch` would
        if out.status == "escalated":
            break
        out = sb.run()
    assert out.status == "escalated", (out.status, out.reason)
    assert "gates failed" in out.reason
    coder = sb.calls("claude", "coder")
    assert 2 <= len(coder) <= 3
    assert "test_add" in coder[1]["prompt"] or "FAIL" in coder[1]["prompt"]  # real unittest output reached the coder
    assert sb.calls("openclaw", "review") == []  # never reviewed code that fails its gates
    assert any("/resume" in c for c in sb.comments(1))  # the human is told how to continue


def test_an_invalid_structured_reply_is_repaired_with_the_validation_errors(make_sandbox):
    sb = make_sandbox()
    sb.script("openclaw", "plan", {"reply": PLAN}).script("openclaw", "plan_review", {"reply": GOOD_PLAN_REVIEW})
    sb.script("claude", "test_plan", {"reply": TEST_PLAN}).script("claude", "coder", GOOD_CODER)
    sb.script("openclaw", "review", {"reply": {"verdict": "approve", "findings": []}}, {"reply": APPROVE})
    out = sb.run()
    assert out.status == "done"
    reviews = sb.calls("openclaw", "review")
    assert len(reviews) == 2 and "integration" in reviews[1]["message"]  # the missing field was named back to it


def test_the_real_cli_runs_a_ticket_in_a_separate_process(make_sandbox):
    sb = make_sandbox().happy()
    proc = sb.cli("ticket", "1")
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "merged" in proc.stdout
    assert sb.ticket().state is State.MERGED
    listing = sb.cli("tickets")
    assert listing.returncode == 0 and "#1" in listing.stdout
