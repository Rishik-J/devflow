"""PR4 in the sandbox: bad plans are caught for free, before any reviewer or coder runs."""
from devflow.states import State
from tests.sandbox.harness import GOOD_CODER, GOOD_PLAN_REVIEW
from tests.test_engine import APPROVE, PLAN, TEST_PLAN

CI_PLAN = {**PLAN, "steps": [{"id": "S1", "description": "add a CI job", "files": [".github/workflows/ci.yml"]}]}
BLOCK = "[planning]\nlint = \"block\"\nlint_retries = 2\n"


def finish(sb):
    sb.script("openclaw", "plan_review", {"reply": GOOD_PLAN_REVIEW})
    sb.script("claude", "test_plan", {"reply": TEST_PLAN}).script("claude", "coder", GOOD_CODER)
    return sb.script("openclaw", "review", {"reply": APPROVE})


def test_block_mode_sends_a_protected_path_plan_back_before_the_reviewer_sees_it(make_sandbox):
    sb = finish(make_sandbox(extra_config=BLOCK))
    sb.script("openclaw", "plan", {"reply": CI_PLAN}, {"reply": PLAN})
    out = sb.run()
    assert out.status == "done", out.reason
    planner = sb.calls("openclaw", "plan")
    assert len(planner) == 2
    assert "Deterministic checks rejected this plan" in planner[1]["message"]
    assert ".github/workflows/ci.yml" in planner[1]["message"] and "follow_ups" in planner[1]["message"]
    assert len(sb.calls("openclaw", "plan_review")) == 1  # the LLM reviewer only ever saw the clean plan
    with sb.store() as store:
        assert [r["ok"] for r in store.stage_results("#1", "plan_lint")] == [False, True]
        assert store.stage_results("#1", "plan_draft")[0]["payload"] == CI_PLAN
        assert len(store.stage_results("#1", "plan")) == 1  # drafts do not use up the plan-cycle budget


def test_a_planner_that_cannot_fix_it_escalates_without_spending_on_a_coder(make_sandbox):
    sb = make_sandbox(extra_config=BLOCK)
    sb.script("openclaw", "plan", {"reply": CI_PLAN, "sticky": True})
    out = sb.run()
    assert out.status == "escalated" and "deterministic checks" in out.reason and "protected_path" in out.reason
    assert len(sb.calls("openclaw", "plan")) == 3  # first attempt + 2 retries
    assert sb.calls("claude") == [] and sb.calls("codex") == []
    assert any("deterministic checks" in c for c in sb.comments(1))


def test_the_ticket_3_pattern_a_false_no_integration_gate_claim_is_caught(make_sandbox):
    claim = {**PLAN, "risks": ["The repo configures only lint and unit gates; there is no integration gate."]}
    sb = finish(make_sandbox(extra_config=BLOCK, gates={"unit": ['true'], "integration": ['true']}))
    sb.script("openclaw", "plan", {"reply": claim}, {"reply": PLAN})
    assert sb.run().status == "done"  # the planner fixed it on the retry
    first_retry = sb.calls("openclaw", "plan")[1]["message"]
    assert "gate_claim" in first_retry and "integration" in first_retry


def test_warn_mode_is_the_default_and_never_blocks_a_plan(make_sandbox):
    sb = finish(make_sandbox())
    sb.script("openclaw", "plan", {"reply": CI_PLAN})
    out = sb.run()
    assert out.status == "done" and len(sb.calls("openclaw", "plan")) == 1
    with sb.store() as store:
        lint = store.stage_results("#1", "plan_lint")[-1]
    assert lint["ok"] is False and lint["payload"]["findings"][0]["kind"] == "protected_path"
    # the reviewer is shown the unresolved blocking finding, so a human-meaningful decision can be made
    review_prompt = sb.calls("openclaw", "plan_review")[0]["message"]
    assert "Deterministic checks reported blocking problems" in review_prompt


def test_off_mode_skips_the_checks_entirely(make_sandbox):
    sb = finish(make_sandbox(extra_config='[planning]\nlint = "off"\n'))
    sb.script("openclaw", "plan", {"reply": CI_PLAN})
    assert sb.run().status == "done"
    with sb.store() as store:
        assert store.stage_results("#1", "plan_lint") == []


def test_a_clean_plan_tells_the_reviewer_what_was_already_verified(make_sandbox):
    sb = finish(make_sandbox(extra_config=BLOCK))
    sb.script("openclaw", "plan", {"reply": PLAN})
    assert sb.run().status == "done"
    assert "Deterministic checks already ran" in sb.calls("openclaw", "plan_review")[0]["message"]


def test_clarifying_questions_are_not_linted_and_pause_for_the_human(make_sandbox):
    ask = {"status": "needs_clarification", "questions": [{"question": "Which module?", "proposed_default": "calc"}]}
    sb = make_sandbox(extra_config=BLOCK)
    sb.script("openclaw", "plan", {"reply": ask})
    out = sb.run()
    assert out.ticket.state is State.AWAITING_CLARIFICATION and out.status == "paused"
    with sb.store() as store:
        assert store.stage_results("#1", "plan_lint") == []
    assert any("Which module?" in c for c in sb.comments(1))


def test_a_plan_that_declares_requirements_flows_through_lint_and_routing_together(make_sandbox):
    needs = {**PLAN, "requires": ["sockets"]}
    sb = finish(make_sandbox(extra_config=BLOCK, routing_agents=("codex", "claude")))
    sb.script("openclaw", "plan", {"reply": needs}).script("codex", "test_plan", {"reply": TEST_PLAN})
    assert sb.run().status == "done"
    assert sb.calls("codex", "coder") == [] and len(sb.calls("claude", "coder")) == 1
