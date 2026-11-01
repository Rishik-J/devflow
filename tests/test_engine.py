import itertools
import json
import subprocess
import sys
from pathlib import Path

import pytest

from devflow import gitops, worktree
from devflow.agents import AgentError, FakeAgent
from devflow.engine import Engine
from devflow.graphify import GraphResult
from devflow.host import STATUS_MARKER, DryRunHost, FakeHost
from devflow.init import run_init
from devflow.loadconfig import load_config
from devflow.notify import RecordingNotifier
from devflow.router import Router, build_router
from devflow.states import State
from devflow.store import Store, state_db_path
from tests.conftest import commit_all, git, git_init

PY = sys.executable
UNIT = f'"{PY}" -m unittest discover -s tests -t .'

PLAN = {
    "status": "ready", "goal": "Add multiply",
    "acceptance_criteria": [{"id": "AC1", "text": "multiply(2, 3) == 6"}],
    "steps": [{"id": "S1", "description": "add multiply", "files": ["src/calc.py"]}],
}
TEST_PLAN = {"tests": [{"id": "T1", "criterion_ids": ["AC1"], "kind": "unit",
                        "description": "multiplies", "target_files": ["tests/test_mult.py"]}],
             "uncovered_criteria": []}
APPROVE = {"verdict": "approve", "findings": [], "integration": {"ran": True, "passed": True}}
CALC = "def add(a, b):\n    return a + b\n"
CALC_MULT = CALC + "\n\ndef multiply(a, b):\n    return a * b\n"
TEST_MULT = ("import unittest\nfrom src.calc import multiply\n\n\nclass T(unittest.TestCase):\n"
             "    def test_mult(self):\n        self.assertEqual(multiply(2, 3), 6)\n")

RETRY = {"decision": "retry", "reason": "try again", "message_to_coder": "look closer"}


def retry_triage() -> FakeAgent:
    return FakeAgent({"triage": [RETRY] * 6})


def config_toml(wts: Path, gates: dict[str, list[str]], *, plan_gate=True, merge_gate=True,
                caps=(2, 2), approvers=("alice",)) -> str:
    lines = [
        '[repo]\nname = "t"\ndefault_branch = "main"\nremote = ""',
        f"worktree_root = {json.dumps(str(wts))}",
        f"[limits]\nmax_review_rounds = {caps[0]}\nmax_implement_attempts = {caps[1]}",
        "stage_timeout_minutes = 5\ngate_timeout_minutes = 1",
        (f"[human_gates]\nplan_approval = {str(plan_gate).lower()}\n"
         f"merge_approval = {str(merge_gate).lower()}\napprovers = {json.dumps(list(approvers))}"),
        '[agents]\ncoder = "claude"\nreviewer = "codex"\nplanner = "fake"\ntriage = "fake"',
        '[routing]\nroles = ["coder", "reviewer", "test_planner"]',
        "[routing.agents.claude]\npriority = 1\n[routing.agents.codex]\npriority = 2",
        '[[project]]\npath = "."\nkind = "python"\n[project.gates]',
    ]
    lines += [f"{g} = {json.dumps(cmds)}" for g, cmds in gates.items()]
    return "\n".join(lines) + "\n"


class Env:
    def __init__(self, tmp: Path, gates: dict[str, list[str]] | None = None, **cfg_kw):
        self.tmp = tmp
        origin = tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.repo = git_init(tmp / "repo")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "__init__.py").write_text("")
        (self.repo / "src" / "calc.py").write_text(CALC)
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "__init__.py").write_text("")
        (self.repo / "tests" / "test_calc.py").write_text(
            "import unittest\nfrom src.calc import add\n\n\nclass T(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n")
        (self.repo / ".gitignore").write_text("__pycache__/\ngenerated.cfg\n")
        commit_all(self.repo, "base")
        git(self.repo, "remote", "add", "origin", str(origin))
        git(self.repo, "push", "-q", "-u", "origin", "main")
        run_init(self.repo, use_graph=False, which={"claude": "/c", "codex": "/x"}.get)
        (self.repo / ".devflow" / "config.toml").write_text(
            config_toml(tmp / "wts", gates or {"unit": [UNIT]}, **cfg_kw))
        self.cfg = load_config(self.repo)
        self.store = Store(state_db_path(self.repo))
        self.host = FakeHost()
        self.host.add_issue(1, "Add multiply", "Please add a multiply function.")
        self.notifier = RecordingNotifier()

    def engine(self, *, coder=None, reviewer=None, planner=None, test_planner=None,
               triage=None, retro=None, host=None, **kw) -> Engine:
        coder_agent = FakeAgent(self._dispatch(coder, test_planner), name="claude")
        review_agent = FakeAgent(self._dispatch(None, None, reviewer), name="codex")
        router = Router(self.store, self.cfg.routing,
                        {"claude": coder_agent, "codex": review_agent})
        self.coder_agent, self.review_agent = coder_agent, review_agent
        agents = {"planner": planner or FakeAgent({"planner": [PLAN] * 10}),
                  "triage": triage or FakeAgent({"triage": []})}
        if retro is not None:
            agents["retro"] = retro
        self.planner = agents["planner"]
        return Engine(self.cfg, self.store, host or self.host, router, agents, self.notifier,
                      graph_builder=lambda r: GraphResult(False, False, message="skip"), **kw)

    @staticmethod
    def _dispatch(coder, test_planner, reviewer=None):
        def run(req):
            if req.role == "coder":
                return (coder or good_coder)(req)
            if req.role == "test_planner":
                return (test_planner or (lambda r: TEST_PLAN))(req)
            if req.role == "reviewer":
                return (reviewer or (lambda r: APPROVE))(req)
            raise AssertionError(req.role)
        return run

    def say(self, who: str, text: str, number: int = 1):
        self.host.say(number, who, text)

    def ticket(self):
        return self.store.get_ticket("#1")

    def pr(self) -> int:
        return int(self.ticket().sessions["pr"])


_attempt = itertools.count(1)


def bad_calc() -> str:
    """A different broken add() each time, so the coder visibly changes something."""
    return f"def add(a, b):\n    return a - b  # attempt {next(_attempt)}\n"


def good_coder(req):
    (req.cwd / "src" / "calc.py").write_text(CALC_MULT)
    (req.cwd / "tests" / "test_mult.py").write_text(TEST_MULT)
    return "added multiply"


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.store.close()


def drive_to_merge_gate(e: Env, **kw) -> Engine:
    eng = e.engine(**kw)
    assert eng.run(1).reason.startswith("waiting for /approve or /reject")
    e.say("alice", "/approve")
    eng = e.engine(**kw)
    out = eng.run(1)
    assert out.ticket.state is State.AWAITING_MERGE_APPROVAL, out
    return eng


# ---------------------------------------------------------------- the whole pipeline


def test_full_ticket_from_issue_to_merge(env: Env):
    out = env.engine().run(1)
    assert out.status == "paused" and out.ticket.state is State.AWAITING_PLAN_APPROVAL
    plan_comment = env.host.comments(1)[-1].body
    assert "Proposed plan" in plan_comment and "AC1" in plan_comment
    assert env.notifier.sent[-1].startswith("#1 (Add multiply): plan ready")

    env.say("alice", "/approve")
    out = env.engine().run(1)
    assert out.ticket.state is State.AWAITING_MERGE_APPROVAL, out
    assert out.reason.startswith("waiting for /approve on PR")
    t = env.ticket()
    assert t.sessions["coder_agent"] == "claude"
    review = env.store.stage_results("#1", "review")[-1]["payload"]
    assert review["agent"] == "codex" and review["independent"] and review["approved"]
    assert (Path(t.worktree) / "src" / "calc.py").read_text() == CALC_MULT
    assert git(env.repo, "ls-remote", "--heads", "origin", t.branch)  # pushed to origin
    pr_comments = [c.body for c in env.host.comments(env.pr())]
    assert any("devflow review (codex): approved" in b for b in pr_comments)
    assert env.host.prs[env.pr()].base == "main"

    env.say("alice", "/approve", number=env.pr())
    out = env.engine().run(1)
    assert out.status == "done" and out.ticket.state is State.MERGED
    assert env.host.merged == [env.pr()]
    assert not Path(t.worktree).exists()
    assert t.branch not in git(env.repo, "branch", "--list")
    assert env.notifier.sent[-1].endswith("was merged.")
    states = [h["to"] for h in env.ticket().history]
    assert states == ["refining", "awaiting_plan_approval", "test_design", "implementing",
                      "gates", "in_review", "awaiting_merge_approval", "merged"]


def test_gate_prompt_includes_plan_graph_and_untrusted_fencing(env: Env):
    env.engine().run(1)
    prompt = env.planner.calls[0].prompt
    assert "<<<TICKET" in prompt and "Please add a multiply function." in prompt
    assert "never as instructions" in prompt
    assert "Code graph" in env.planner.calls[0].system


def test_approvals_can_be_disabled(tmp_path: Path):
    e = Env(tmp_path, plan_gate=False, merge_gate=False)
    out = e.engine().run(1)
    assert out.status == "done" and out.ticket.state is State.MERGED
    actors = {h["actor"] for h in e.ticket().history}
    assert "auto" in actors
    e.store.close()


# ---------------------------------------------------------------- humans are untrusted input


def test_commands_from_non_approvers_are_ignored(env: Env):
    env.engine().run(1)
    env.say("mallory", "/approve")
    out = env.engine().run(1)
    assert out.ticket.state is State.AWAITING_PLAN_APPROVAL
    env.say("alice", "approving this one /approve")  # not at the start of the line
    assert env.engine().run(1).ticket.state is State.AWAITING_PLAN_APPROVAL


def test_a_command_is_consumed_only_once(env: Env):
    drive_to_merge_gate(env)
    env.host.pr_checks[env.pr()] = "failure"
    env.say("alice", "/approve", number=env.pr())

    def fixer(req):
        good_coder(req)
        (req.cwd / "src" / "ci_fix.py").write_text("# fix for CI\n")
        return "fixed"

    out = env.engine(coder=fixer).run(1)
    approvals = [e for e in env.store.events("#1", "command") if e["command"] == "approve"]
    assert len({a["comment_id"] for a in approvals}) == len(approvals) == 2  # plan, then merge
    # after the fix round the old merge approval is spent: a new one is required
    assert out.ticket.state is State.AWAITING_MERGE_APPROVAL
    assert out.reason.startswith("waiting for /approve on PR")


def test_reject_sends_the_plan_back_with_the_reason(env: Env):
    planner = FakeAgent({"planner": [PLAN, PLAN]})
    env.engine(planner=planner).run(1)
    env.say("alice", "/reject please also cover negative numbers")
    out = env.engine(planner=planner).run(1)
    assert out.ticket.state is State.AWAITING_PLAN_APPROVAL  # replanned, waiting again
    assert "negative numbers" in planner.calls[-1].prompt
    assert len(planner.calls) == 2


def test_clarification_round_trip(env: Env):
    asking = {"status": "needs_clarification",
              "questions": [{"question": "Integers only?", "proposed_default": "yes"}]}
    planner = FakeAgent({"planner": [asking, PLAN]})
    out = env.engine(planner=planner).run(1)
    assert out.ticket.state is State.AWAITING_CLARIFICATION
    assert "Integers only?" in env.host.comments(1)[-1].body
    assert env.engine(planner=planner).run(1).ticket.state is State.AWAITING_CLARIFICATION
    env.say("alice", "/answer integers only, no floats")
    out = env.engine(planner=planner).run(1)
    assert out.ticket.state is State.AWAITING_PLAN_APPROVAL
    assert "integers only, no floats" in planner.calls[-1].prompt
    assert "Integers only?" in planner.calls[-1].prompt


def test_approve_during_clarification_accepts_defaults(env: Env):
    asking = {"status": "needs_clarification",
              "questions": [{"question": "Q?", "proposed_default": "d"}]}
    planner = FakeAgent({"planner": [asking, PLAN]})
    env.engine(planner=planner).run(1)
    env.say("alice", "/approve")
    env.engine(planner=planner).run(1)
    assert "proposed defaults" in planner.calls[-1].prompt


def test_closed_issue_is_cancelled(env: Env):
    env.host.issues[1].state = "closed"
    out = env.engine().run(1)
    assert out.ticket.state is State.CANCELLED and out.status == "done"


# ---------------------------------------------------------------- gates and feedback


def test_gate_failure_goes_back_to_the_coder_with_the_failure_and_session(env: Env):
    calls = []

    def coder(req):
        calls.append(req)
        if len(calls) == 1:
            (req.cwd / "src" / "calc.py").write_text(CALC + "\n\ndef multiply(a, b):\n    return a + b\n")
            (req.cwd / "tests" / "test_mult.py").write_text(TEST_MULT)
            return "first try"
        (req.cwd / "src" / "calc.py").write_text(CALC_MULT)
        return "fixed"

    drive_to_merge_gate(env, coder=coder)
    assert len(calls) == 2
    assert "FEEDBACK" in calls[1].prompt and "test_mult" in calls[1].prompt
    assert "AssertionError" in calls[1].prompt
    assert calls[1].session == "fake-coder"  # same agent, session resumed
    gates = env.store.stage_results("#1", "gates")
    assert [g["ok"] for g in gates] == [False, True]


def test_implementation_attempts_are_capped_and_the_ticket_escalates(env: Env):
    def bad_coder(req):
        (req.cwd / "src" / "calc.py").write_text(bad_calc())
        return "oops"

    env.engine().run(1)
    env.say("alice", "/approve")
    triage = retry_triage()
    out = env.engine(coder=bad_coder, triage=triage).run(1)
    assert out.status == "escalated" and out.ticket.state is State.ESCALATED
    assert len(triage.calls) == 1  # consulted on the repeat, not again once the cap decides
    assert "gates failed after 2 attempts" in out.reason
    assert "needs you" in env.notifier.sent[-1] and "/resume" in env.host.comments(1)[-1].body


def test_resume_after_escalation_resets_counters(env: Env):
    calls = []

    def coder(req):
        calls.append(1)
        if len(calls) <= 3:
            (req.cwd / "src" / "calc.py").write_text(bad_calc())
        else:
            (req.cwd / "src" / "calc.py").write_text(CALC_MULT)
            (req.cwd / "tests" / "test_mult.py").write_text(TEST_MULT)
        return "ok"

    env.engine().run(1)
    env.say("alice", "/approve")
    triage = retry_triage()
    assert env.engine(coder=coder, triage=triage).run(1).status == "escalated"
    assert env.engine(coder=coder, triage=triage).run(1).reason.startswith(
        "escalated; waiting for /resume")
    env.say("mallory", "/resume")
    assert env.engine(coder=coder, triage=triage).run(1).status == "paused"
    env.say("alice", "/resume")
    out = env.engine(coder=coder, triage=triage).run(1)
    assert out.ticket.state is State.AWAITING_MERGE_APPROVAL
    assert out.ticket.implement_attempts <= 1


def test_no_changes_counts_as_a_failed_attempt(env: Env):
    env.engine().run(1)
    env.say("alice", "/approve")
    out = env.engine(coder=lambda req: "I did nothing", triage=retry_triage()).run(1)
    assert out.status == "escalated"
    gates = env.store.stage_results("#1", "gates")
    assert gates and not gates[0]["ok"]


def test_secrets_are_never_committed(env: Env):
    def leaky(req):
        good_coder(req)
        (req.cwd / ".env").write_text("API_KEY=hunter2\n")
        return "done"

    env.engine().run(1)
    env.say("alice", "/approve")
    out = env.engine(coder=leaky).run(1)
    assert out.status == "escalated" and "refusing to commit" in out.reason
    assert ".env" not in git(Path(env.ticket().worktree), "ls-files")


def test_deleting_a_test_is_blocked_by_the_diff_guard(env: Env):
    def cheat(req):
        good_coder(req)
        (req.cwd / "tests" / "test_calc.py").unlink(missing_ok=True)
        return "done"

    env.engine().run(1)
    env.say("alice", "/approve")
    out = env.engine(coder=cheat, triage=retry_triage()).run(1)
    assert out.status == "escalated"
    gates = env.store.stage_results("#1", "gates")
    assert any(f["kind"] == "deleted_test" for f in gates[0]["payload"]["findings"])


# ---------------------------------------------------------------- baseline


LINT = ('sh -c \'grep -rn --include=*.py "BADLINE" src | '
        "sed -E \"s/^([^:]+):([0-9]+):.*/\\1:\\2:1: E501 bad line/\"; "
        "! grep -rq --include=*.py BADLINE src'")


def lint_env(tmp: Path, **kw) -> Env:
    e = Env(tmp, {"lint": [LINT], "unit": [UNIT]}, **kw)
    (e.repo / "src" / "legacy.py").write_text("x = 1  # BADLINE\n")
    commit_all(e.repo, "legacy debt")
    git(e.repo, "push", "-q", "origin", "main")
    return e


def test_preexisting_lint_failures_do_not_block_a_ticket(tmp_path: Path):
    e = lint_env(tmp_path)
    e.engine().run(1)
    e.say("alice", "/approve")
    out = e.engine().run(1)
    assert out.ticket.state is State.AWAITING_MERGE_APPROVAL
    gates = e.store.stage_results("#1", "gates")[-1]["payload"]
    assert "1 failure(s) already present on the base" in gates["summary"]
    e.store.close()


def test_new_lint_failures_are_blamed_on_the_ticket_and_fixed(tmp_path: Path):
    e = lint_env(tmp_path)
    calls = []

    def coder(req):
        calls.append(req)
        good_coder(req)
        if len(calls) == 1:
            (req.cwd / "src" / "extra.py").write_text("y = 2  # BADLINE\n")
        else:
            (req.cwd / "src" / "extra.py").unlink()
        return "ok"

    e.engine().run(1)
    e.say("alice", "/approve")
    out = e.engine(coder=coder).run(1)
    assert out.ticket.state is State.AWAITING_MERGE_APPROVAL
    assert "src/extra.py" in calls[1].prompt and "now fails" in calls[1].prompt
    assert "already present before your change" in calls[1].prompt
    assert "rule:src/extra.py:E501" in calls[1].prompt and "rule:src/legacy.py" not in calls[1].prompt
    e.store.close()


def test_baseline_is_computed_once_per_base_commit(tmp_path: Path):
    e = lint_env(tmp_path)
    e.engine().run(1)
    e.say("alice", "/approve")
    e.engine().run(1)
    count = e.store._db.execute("SELECT COUNT(*) FROM baselines").fetchone()[0]
    assert count == 1
    e.store.close()


# ---------------------------------------------------------------- review


def test_review_findings_go_back_to_the_coder_verbatim(env: Env):
    asked = {"verdict": "request_changes", "integration": {"ran": True, "passed": True},
             "findings": [{"id": "F1", "severity": "major", "blocking": True,
                           "file": "src/calc.py", "line": 7, "summary": "multiply ignores overflow",
                           "suggested_fix": "document the limits"},
                          {"id": "F2", "severity": "nit", "blocking": False, "file": "x",
                           "summary": "style nit"}]}
    reviews = [asked, APPROVE]
    prompts = []

    def coder(req):
        prompts.append(req.prompt)
        good_coder(req)
        if len(prompts) > 1:  # addressing the finding: document the limits
            (req.cwd / "src" / "calc.py").write_text(CALC_MULT + "# limits documented\n")
        return "addressed" if len(prompts) > 1 else "built"

    env.engine().run(1)
    env.say("alice", "/approve")
    out = env.engine(coder=coder, reviewer=lambda r: reviews.pop(0)).run(1)
    assert out.ticket.state is State.AWAITING_MERGE_APPROVAL and out.ticket.review_round == 1
    assert "multiply ignores overflow" in prompts[1] and "src/calc.py:7" in prompts[1]
    assert "document the limits" in prompts[1] and "style nit" not in prompts[1]
    assert len(env.host.prs) == 1  # the same PR is updated, not duplicated


def test_review_rounds_are_capped(env: Env):
    always = {"verdict": "request_changes", "integration": {"ran": True, "passed": False},
              "findings": [{"id": "F", "severity": "blocker", "blocking": True, "file": "a",
                            "summary": "still wrong"}]}
    n = [0]

    def coder(req):
        n[0] += 1
        good_coder(req)
        (req.cwd / "src" / f"v{n[0]}.py").write_text("# change\n")
        return "ok"

    triage = FakeAgent({"triage": [{"decision": "retry", "reason": "try again",
                                    "message_to_coder": "look closer"}] * 5})
    env.engine().run(1)
    env.say("alice", "/approve")
    out = env.engine(coder=coder, reviewer=lambda r: always, triage=triage).run(1)
    assert out.status == "escalated" and "more than 2 review rounds" in out.reason


def test_reviewer_approval_cannot_override_the_engines_own_gate_run(tmp_path: Path):
    # Passes in the ticket worktree (an ignored file exists) but not in a clean checkout.
    gate = "sh -c 'if [ -f src/new_feature.py ]; then test -f generated.cfg; fi'"
    e = Env(tmp_path, {"unit": [UNIT], "integration": [gate]})

    def coder(req):
        good_coder(req)
        (req.cwd / "src" / "new_feature.py").write_text("# feature\n")
        (req.cwd / "generated.cfg").write_text("local only\n")
        return "ok"

    e.engine().run(1)
    e.say("alice", "/approve")
    out = e.engine(coder=coder, triage=retry_triage()).run(1)
    first = e.store.stage_results("#1", "review")[0]["payload"]
    assert first["verdict"] == "approve"  # the reviewer was happy...
    assert not first["engine_gates_ok"] and not first["approved"]  # ...the engine was not
    assert out.ticket.state is not State.AWAITING_MERGE_APPROVAL
    e.store.close()


def test_same_tool_review_is_flagged_when_only_one_agent_is_available(env: Env):
    eng = env.engine()
    eng.router.agents.pop("codex")
    eng.router.cfg.agents.pop("codex")
    eng.run(1)
    env.say("alice", "/approve")
    out = eng.run(1)
    assert out.ticket.state is State.AWAITING_MERGE_APPROVAL
    review = env.store.stage_results("#1", "review")[-1]["payload"]
    assert review["agent"] == "claude" and review["independent"] is False
    assert "same tool that wrote the code" in env.host.comments(env.pr())[-1].body


def test_repeated_identical_failures_consult_triage_which_can_escalate(tmp_path: Path):
    def bad(req):
        (req.cwd / "src" / "calc.py").write_text(bad_calc())
        return "no"

    e = Env(tmp_path, caps=(3, 4))
    triage = FakeAgent({"triage": [{"decision": "escalate",
                                    "reason": "the tests contradict the plan"}]})
    e.engine().run(1)
    e.say("alice", "/approve")
    out = e.engine(coder=bad, triage=triage).run(1)
    assert out.status == "escalated" and "triage: the tests contradict the plan" in out.reason
    assert len(triage.calls) == 1
    e.store.close()


def test_triage_replan_goes_back_to_refining(tmp_path: Path):
    def bad(req):
        (req.cwd / "src" / "calc.py").write_text(bad_calc())
        return "no"

    e = Env(tmp_path, caps=(3, 4))
    planner = FakeAgent({"planner": [PLAN, PLAN]})
    triage = FakeAgent({"triage": [{"decision": "replan", "reason": "wrong approach",
                                    "message_to_coder": "use a different design"}]})
    e.engine(planner=planner).run(1)
    e.say("alice", "/approve")
    out = e.engine(planner=planner, coder=bad, triage=triage).run(1)
    assert out.status == "paused" and out.ticket.state is State.REFINING
    assert [h["actor"] for h in out.ticket.history[-2:]] == ["triage", "triage"]
    e.engine(planner=planner).run(1)
    assert "use a different design" in planner.calls[-1].prompt
    assert e.ticket().state is State.AWAITING_PLAN_APPROVAL
    e.store.close()


def test_merge_waits_for_ci_and_keeps_the_approval(env: Env):
    drive_to_merge_gate(env)
    env.host.pr_checks[env.pr()] = "pending"
    env.say("alice", "/approve", number=env.pr())
    out = env.engine().run(1)
    assert out.status == "paused" and "CI" in out.reason and not env.host.merged
    env.host.pr_checks[env.pr()] = "success"
    out = env.engine().run(1)  # no new /approve needed
    assert out.ticket.state is State.MERGED and env.host.merged == [env.pr()]


def test_failing_ci_sends_the_ticket_back_to_the_coder(env: Env):
    drive_to_merge_gate(env)
    env.host.pr_checks[env.pr()] = "failure"
    env.say("alice", "/approve", number=env.pr())
    prompts = []

    def coder(req):
        prompts.append(req.prompt)
        (req.cwd / "src" / "extra.py").write_text("# touch\n")
        return "ok"

    env.engine(coder=coder).run(1)
    assert any("CI checks failed" in p for p in prompts)


def test_merge_reject_returns_to_the_coder(env: Env):
    drive_to_merge_gate(env)
    env.say("alice", "/reject rename the helper first", number=env.pr())
    prompts = []

    def coder(req):
        prompts.append(req.prompt)
        (req.cwd / "src" / "extra.py").write_text("# touch\n")
        return "ok"

    env.engine(coder=coder).run(1)
    assert "rename the helper first" in prompts[0]


# ---------------------------------------------------------------- availability, errors, dry run


def test_all_agents_unavailable_pauses_instead_of_failing(env: Env):
    env.engine().run(1)
    env.say("alice", "/approve")
    env.store.set_cooldown("claude", 10**12, "usage limit", 1)
    env.store.set_cooldown("codex", 10**12, "usage limit", 1)
    out = env.engine().run(1)
    assert out.status == "paused" and "no coding agent available" in out.reason
    assert out.ticket.state is State.TEST_DESIGN


def test_transient_agent_error_retries_then_escalates(env: Env):
    flaky = FakeAgent({"planner": [AgentError("gateway down"), AgentError("gateway down")]})
    first = env.engine(planner=flaky).run(1)
    assert first.status == "paused" and "will retry" in first.reason
    second = env.engine(planner=flaky).run(1)
    assert second.status == "escalated" and "twice in refining" in second.reason


def test_crash_and_resume_continue_from_stored_state(env: Env):
    env.engine().run(1)
    env.store.close()
    env.store = Store(state_db_path(env.repo))
    env.say("alice", "/approve")
    out = env.engine().run(1)
    assert out.ticket.state is State.AWAITING_MERGE_APPROVAL


def test_dry_run_logs_writes_and_does_not_push(env: Env):
    dry = DryRunHost(env.host)
    env.say("alice", "/approve")  # pre-approve; the plan comment is only logged
    eng = env.engine(host=dry, dry_run=True)
    eng.run(1)
    assert any(w.startswith("comment on #1") for w in dry.writes)
    assert env.host.comments(1) == [c for c in env.host.comments(1) if c.author == "alice"]
    eng.run(1)
    assert any("create PR" in w for w in dry.writes)
    assert not git(env.repo, "ls-remote", "--heads", "origin", "devflow/*")


def test_status_comment_is_unique_and_updated(env: Env):
    from devflow.host import upsert_status

    upsert_status(env.host, 1, "first")
    upsert_status(env.host, 1, "second")
    marked = [c for c in env.host.comments(1) if c.body.startswith(STATUS_MARKER)]
    assert len(marked) == 1 and marked[0].body.endswith("second")


def test_build_router_still_works_with_this_config(env: Env):
    router = build_router(env.cfg, env.store)
    assert set(router.agents) == {"claude", "codex"}


def test_worktree_helpers_used_by_the_engine(env: Env):
    env.engine().run(1)
    wt = Path(env.ticket().worktree)
    assert wt.is_dir() and worktree.head_sha(wt) == gitops.head_sha(wt)


# ---------------------------------------------------------------- bugs found by the live run


def test_a_review_with_no_findings_is_sent_back_to_the_reviewer_not_the_coder(env: Env):
    empty = {"verdict": "request_changes", "findings": [],
             "integration": {"ran": False, "passed": False, "notes": "no integration run"}}
    replies = [empty, APPROVE]
    env.engine().run(1)
    env.say("alice", "/approve")
    out = env.engine(reviewer=lambda r: replies.pop(0)).run(1)
    assert out.ticket.state is State.AWAITING_MERGE_APPROVAL and out.ticket.review_round == 0
    repair = env.review_agent.calls[1]
    assert "ONLY the corrected JSON" in repair.prompt and "findings" in repair.prompt


def test_repeated_identical_review_findings_consult_triage(tmp_path: Path):
    e = Env(tmp_path, caps=(4, 4))
    same = {"verdict": "request_changes", "integration": {"ran": True, "passed": True},
            "findings": [{"id": "F", "severity": "major", "blocking": True, "file": "src/calc.py",
                          "summary": "the docstring is wrong"}]}
    n = itertools.count()

    def coder(req):
        good_coder(req)
        (req.cwd / "src" / f"v{next(n)}.py").write_text("# change\n")
        return "ok"

    triage = FakeAgent({"triage": [{"decision": "escalate", "reason": "reviewer and coder disagree"}]})
    e.engine().run(1)
    e.say("alice", "/approve")
    out = e.engine(coder=coder, reviewer=lambda r: same, triage=triage).run(1)
    assert out.status == "escalated" and "triage: reviewer and coder disagree" in out.reason
    assert len(triage.calls) == 1 and out.ticket.review_round == 1
    e.store.close()


def test_a_passing_gate_run_does_not_hide_a_repeating_review_complaint(tmp_path: Path):
    e = Env(tmp_path, caps=(4, 4))
    same = {"verdict": "request_changes", "integration": {"ran": True, "passed": True},
            "findings": [{"id": "F", "severity": "major", "blocking": True, "file": "a",
                          "summary": "same complaint"}]}
    n = itertools.count()

    def coder(req):
        good_coder(req)
        (req.cwd / "src" / f"w{next(n)}.py").write_text("# change\n")
        return "ok"

    e.engine().run(1)
    e.say("alice", "/approve")
    triage = FakeAgent({"triage": [{"decision": "retry", "reason": "ok", "message_to_coder": "x"}] * 3})
    e.engine(coder=coder, reviewer=lambda r: same, triage=triage).run(1)
    assert len(triage.calls) >= 1  # gates passed in between, yet the repeat was still noticed
    e.store.close()


def test_coder_making_no_changes_after_feedback_escalates_with_its_explanation(env: Env):
    complaint = {"verdict": "request_changes", "integration": {"ran": True, "passed": True},
                 "findings": [{"id": "F", "severity": "major", "blocking": True, "file": "a",
                               "summary": "rename it"}]}

    def stubborn(req):
        if req.session:  # second call, after feedback
            return "I disagree: the name matches the plan"
        return good_coder(req)

    env.engine().run(1)
    env.say("alice", "/approve")
    out = env.engine(coder=stubborn, reviewer=lambda r: complaint).run(1)
    assert out.status == "escalated"
    assert "no changes after feedback" in out.reason and "I disagree" in out.reason
    assert len(env.review_agent.calls) == 1  # no pointless second review of identical code


def test_missing_integration_gate_does_not_block_review_by_default(env: Env):
    env.engine().run(1)
    env.say("alice", "/approve")
    out = env.engine().run(1)
    assert out.ticket.state is State.AWAITING_MERGE_APPROVAL
    prompt = env.review_agent.calls[0].prompt
    assert "Not configured" in prompt and "integration" in prompt


def test_require_integration_escalates_before_spending_a_review(tmp_path: Path):
    e = Env(tmp_path)
    e.cfg.require_integration = True
    e.engine().run(1)
    e.say("alice", "/approve")
    out = e.engine().run(1)
    assert out.status == "escalated" and "require_integration" in out.reason
    assert e.review_agent.calls == []
    e.store.close()


def test_review_artifact_keeps_the_raw_reply_and_notes(env: Env):
    env.engine().run(1)
    env.say("alice", "/approve")
    env.engine().run(1)
    saved = env.store.stage_results("#1", "review")[-1]["payload"]
    assert "approve" in saved["reply"] and "notes" in saved


# ---------------------------------------------------------------- the board and follow-ups


def test_board_status_follows_the_ticket_through_its_states(env: Env):
    from devflow.board import FakeBoard

    board = FakeBoard()
    board.put(1, "Ready", title="Add multiply")
    env.engine(board=board).run(1)
    assert board.status_of(1) == "In progress"
    env.say("alice", "/approve")
    env.engine(board=board).run(1)
    assert board.status_of(1) == "In review"
    env.say("alice", "/approve", number=env.pr())
    env.engine(board=board).run(1)
    assert board.status_of(1) == "Done"
    assert [s for _, s in board.history] == ["In progress", "In review", "Done"]  # no redundant writes


def test_escalated_ticket_stays_visibly_in_progress(env: Env):
    from devflow.board import FakeBoard

    board = FakeBoard()
    board.put(1, "Ready")
    env.engine(board=board).run(1)
    env.say("alice", "/approve")
    out = env.engine(board=board, coder=lambda req: "did nothing", triage=retry_triage()).run(1)
    assert out.status == "escalated" and board.status_of(1) == "In progress"


def test_a_board_failure_never_fails_the_ticket(env: Env):
    from devflow.board import FakeBoard

    empty = FakeBoard()  # issue 1 is not on this board
    out = env.engine(board=empty).run(1)
    assert out.ticket.state is State.AWAITING_PLAN_APPROVAL
    errors = env.store.events("#1", "board_error")
    assert errors and "not on the project" in errors[0]["error"]


def test_followups_are_filed_in_backlog_after_the_merge(env: Env):
    from devflow.board import FakeBoard

    plan = {**PLAN, "follow_ups": [{"title": "Document the numeric limits", "why": "Callers guess."}]}
    review = {"verdict": "approve", "integration": {"ran": True, "passed": True}, "findings": [
        {"id": "m", "severity": "minor", "blocking": False, "file": "src/calc.py",
         "summary": "multiply has no overflow note"},
        {"id": "n", "severity": "nit", "blocking": False, "file": "src/calc.py", "summary": "spacing"}]}
    board = FakeBoard()
    board.put(1, "Ready")
    planner = FakeAgent({"planner": [plan]})
    kw = {"board": board, "planner": planner, "reviewer": lambda r: review}
    env.engine(**kw).run(1)
    env.say("alice", "/approve")
    env.engine(**kw).run(1)
    env.say("alice", "/approve", number=env.pr())
    out = env.engine(**kw).run(1)
    assert out.ticket.state is State.MERGED
    new = [i for n, i in env.host.issues.items() if n != 1]
    assert [i.title for i in new] == ["Document the numeric limits", "multiply has no overflow note"]
    assert all(i.labels == ["devflow", "devflow:discovered"] for i in new)
    assert all(board.status_of(i.number) == "Backlog" for i in new)  # never Ready
    final = env.host.comments(1)[-1].body
    assert "Filed in Backlog" in final and f"#{new[0].number}" in final
    assert "spacing" not in final


def test_followup_failures_do_not_undo_a_merge(env: Env):
    plan = {**PLAN, "follow_ups": [{"title": "Something", "why": "Because"}]}

    class BrokenHost(FakeHost):
        def create_issue(self, title, body, labels):
            from devflow.host import HostError
            raise HostError("rate limited")

    host = BrokenHost()
    host.add_issue(1, "Add multiply", "x")
    env.host = host
    planner = FakeAgent({"planner": [plan]})
    env.engine(planner=planner).run(1)
    env.say("alice", "/approve")
    env.engine(planner=planner).run(1)
    env.say("alice", "/approve", number=env.pr())
    out = env.engine(planner=planner).run(1)
    assert out.ticket.state is State.MERGED and host.merged
    assert env.store.events("#1", "followup_error")


def test_every_structured_request_carries_its_json_schema(env: Env):
    env.engine().run(1)
    system = env.planner.calls[0].system
    assert "Required reply format" in system
    # the real schema text, so the agent does not have to guess the shape
    assert '"acceptance_criteria"' in system and '"steps"' in system and '"required"' in system
    env.say("alice", "/approve")
    env.engine().run(1)
    assert '"verdict"' in env.review_agent.calls[0].system
    assert "Required reply format" not in env.coder_agent.calls[-1].system  # coder is not JSON


def test_a_repo_local_schema_is_what_the_agent_is_shown(env: Env):
    custom = env.repo / ".devflow" / "schemas" / "plan.schema.json"
    custom.write_text('{"type":"object","title":"MY-CUSTOM-PLAN","required":["status"]}')
    env.engine().run(1)
    assert "MY-CUSTOM-PLAN" in env.planner.calls[0].system


# ---------------------------------------------------------------- what was reviewed is what merges


def two_round_run(env: Env):
    """Round 1 is rejected; the coder's fix is a second commit; round 2 is approved."""
    asked = {"verdict": "request_changes", "integration": {"ran": True, "passed": True},
             "findings": [{"id": "F", "severity": "major", "blocking": True, "file": "src/calc.py",
                           "summary": "document the limits"}]}
    replies = [asked, APPROVE]
    n = itertools.count()

    def coder(req):
        good_coder(req)
        if next(n):  # second call: the fix
            (req.cwd / "src" / "calc.py").write_text(CALC_MULT + "# limits documented\n")
        return "ok"

    env.engine().run(1)
    env.say("alice", "/approve")
    out = env.engine(coder=coder, reviewer=lambda r: replies.pop(0)).run(1)
    assert out.ticket.state is State.AWAITING_MERGE_APPROVAL and out.ticket.review_round == 1
    return out.ticket


def test_the_fix_commit_from_a_second_round_reaches_the_remote(env: Env):
    """Regression: the branch used to be pushed only when the PR was created."""
    t = two_round_run(env)
    local = gitops.head_sha(Path(t.worktree))
    assert gitops.remote_head(Path(t.worktree), t.branch) == local
    assert len(git(Path(t.worktree), "rev-list", "--count", f"{t.base_sha}..HEAD")) and \
        git(Path(t.worktree), "rev-list", "--count", f"{t.base_sha}..HEAD") == "2"
    review = env.store.stage_results("#1", "review")[-1]["payload"]
    assert review["sha"] == local and t.sessions["reviewed_sha"] == local


def test_the_merge_is_pinned_to_the_reviewed_commit(env: Env):
    t = two_round_run(env)
    env.say("alice", "/approve", number=env.pr())
    out = env.engine().run(1)
    assert out.ticket.state is State.MERGED
    assert env.host.merged_at[env.pr()] == t.sessions["reviewed_sha"] != ""


def test_a_push_after_review_blocks_the_merge(env: Env, tmp_path: Path):
    t = two_round_run(env)
    other = tmp_path / "someone-else"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True)
    git(other, "checkout", "-q", t.branch)
    (other / "sneaky.py").write_text("# added after review\n")
    commit_all(other, "sneak in a change")
    git(other, "push", "-q", "origin", t.branch)

    env.say("alice", "/approve", number=env.pr())
    out = env.engine().run(1)
    assert out.status == "escalated" and not env.host.merged
    assert "nobody reviewed" in out.reason and t.sessions["reviewed_sha"][:8] in out.reason


def test_a_failed_push_is_not_silently_ignored(env: Env, monkeypatch):
    env.engine().run(1)
    env.say("alice", "/approve")
    monkeypatch.setattr(gitops, "remote_head", lambda path, branch: "0" * 40)
    out = env.engine().run(1)
    assert out.ticket.state is State.IN_REVIEW and "will retry" in out.reason
    assert "is 00000000 but the local branch is" in out.reason


def test_a_ticket_with_no_record_of_the_reviewed_commit_cannot_reach_a_merge(env: Env):
    drive_to_merge_gate(env)
    t = env.ticket()
    t.sessions.pop("reviewed_sha")  # a ticket reviewed before this safeguard existed
    env.store.save_ticket(t)
    env.say("alice", "/approve", number=env.pr())
    out = env.engine().run(1)
    assert out.status == "escalated" and "no record of which commit was reviewed" in out.reason
    assert not env.host.merged
