"""The whole ticket loop in a sandbox that replicates the real environment."""
from devflow.states import State


def test_autonomous_ticket_runs_from_issue_to_merge_through_the_real_stack(make_sandbox):
    sb = make_sandbox().happy()
    out = sb.run()
    assert out.status == "done" and out.ticket.state is State.MERGED, out.reason

    # real git: the branch reached the origin, and the PR was opened and merged through gh
    gh = sb.gh_state()
    assert len(gh["prs"]) == 1 and gh["merges"][0]["match_head"]  # merge pinned to the reviewed commit
    assert "devflow/1-" in sb.remote_branches()

    # each external tool was actually invoked, with the real drivers' command lines
    claude = sb.calls("claude")
    assert [c["role"] for c in claude] == ["test_plan", "coder"]
    assert "--permission-mode" in next(c for c in claude if c["role"] == "coder")["argv"]
    assert [c["role"] for c in sb.calls("openclaw")] == ["plan", "plan_review", "review"]
    assert sb.calls("codex") == []  # claude has the headroom and priority 1

    # real driver parsing: cost, tokens and sessions made it into the store
    usage = {(a, r): (c, i) for a, r, c, i, _ in sb.usage()}
    assert usage[("claude", "coder")] == (0.5, 2000)
    assert ("openclaw:devflow", "planner") in usage  # OpenClaw usage is recorded (budget foundation)
    assert [e["to"] for e in sb.events("transition")][-1] == "merged"
