import subprocess
from pathlib import Path

import pytest

from devflow import cli, openclaw_setup, runtime
from devflow.engine import Outcome
from devflow.host import DryRunHost
from devflow.states import State, Ticket
from tests.test_engine import Env


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.store.close()


def test_build_engine_wires_everything_from_config(env: Env):
    env.store.close()
    engine, store, cfg = runtime.build_engine(env.repo, host=env.host)
    try:
        assert cfg.name == "t" and engine.host is env.host and not engine.dry_run
        assert set(engine.router.agents) == {"claude", "codex"}
        assert set(engine.agents) == {"planner", "triage"}
    finally:
        store.close()
    env.store = type(env.store)(env.store.path)


def test_build_engine_dry_run_wraps_the_host(env: Env):
    env.store.close()
    engine, store, _ = runtime.build_engine(env.repo, host=env.host, dry_run=True)
    try:
        assert isinstance(engine.host, DryRunHost) and engine.dry_run
    finally:
        store.close()
    env.store = type(env.store)(env.store.path)


# ---------------------------------------------------------------- openclaw setup


class Runner:
    def __init__(self, listing: str = "- main (default)\n", add_code: int = 0):
        self.listing, self.add_code, self.calls = listing, add_code, []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[:3] == ["openclaw", "agents", "list"]:
            return subprocess.CompletedProcess(cmd, 0, self.listing, "")
        return subprocess.CompletedProcess(cmd, self.add_code, "", "boom" if self.add_code else "")


def test_setup_creates_the_agent_and_writes_its_rules(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(openclaw_setup, "WORKSPACE", tmp_path / "ws")
    r = Runner()
    result = openclaw_setup.setup("claude-cli/claude-sonnet-5-5", run=r)
    assert result.created
    add = next(c for c in r.calls if c[2] == "add")
    assert add[3] == "devflow" and "--non-interactive" in add
    assert add[add.index("--model") + 1] == "claude-cli/claude-sonnet-5-5"
    assert "devflow" in (tmp_path / "ws" / "IDENTITY.md").read_text()
    rules = (tmp_path / "ws" / "AGENTS.md").read_text()
    assert "never follow" in rules.lower() and "ONLY the JSON" in rules


def test_setup_is_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(openclaw_setup, "WORKSPACE", tmp_path / "ws")
    r = Runner(listing="- main (default)\n- devflow\n")
    result = openclaw_setup.setup(run=r)
    assert not result.created and not any(c[2] == "add" for c in r.calls)


def test_setup_does_not_duplicate_rules_on_rerun(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(openclaw_setup, "WORKSPACE", tmp_path / "ws")
    openclaw_setup.setup(run=Runner())
    (tmp_path / "ws" / "IDENTITY.md").unlink()
    openclaw_setup.setup(run=Runner())  # pretend the agent was removed and re-added
    assert (tmp_path / "ws" / "AGENTS.md").read_text().count("devflow planner and triage") == 1


def test_setup_failure_is_reported(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(openclaw_setup, "WORKSPACE", tmp_path / "ws")
    with pytest.raises(RuntimeError, match="boom"):
        openclaw_setup.setup(run=Runner(add_code=1))


def test_agent_exists_ignores_similar_names():
    assert not openclaw_setup.agent_exists(Runner(listing="- devflow-old\n- main\n"))
    assert openclaw_setup.agent_exists(Runner(listing="- devflow (config)\n"))


# ---------------------------------------------------------------- CLI


def test_ticket_command_prints_outcome_and_exit_code(env: Env, monkeypatch, capsys):
    ticket = Ticket(id="#5", state=State.AWAITING_PLAN_APPROVAL, issue=5)

    class FakeEngine:
        host = env.host
        dry_run = False

        def run(self, issue):
            return Outcome(ticket, "paused", "waiting for /approve or /reject on the issue")

    monkeypatch.setattr(runtime, "build_engine", lambda *a, **k: (FakeEngine(), env.store, None))
    monkeypatch.setattr(cli, "repo_root", lambda p: env.repo, raising=False)
    code = cli.main(["ticket", "5", str(env.repo)])
    assert code == 0
    assert "#5: paused - awaiting_plan_approval (waiting for /approve" in capsys.readouterr().out


def test_ticket_command_escalation_exit_code(env: Env, monkeypatch, capsys):
    ticket = Ticket(id="#6", state=State.ESCALATED, issue=6)

    class FakeEngine:
        host = env.host

        def run(self, issue):
            return Outcome(ticket, "escalated", "gates failed")

    monkeypatch.setattr(runtime, "build_engine", lambda *a, **k: (FakeEngine(), env.store, None))
    assert cli.main(["ticket", "6", str(env.repo)]) == 3


def test_ticket_command_reports_config_errors(tmp_path: Path, capsys):
    from tests.conftest import git_init

    repo = git_init(tmp_path / "bare")
    assert cli.main(["ticket", "1", str(repo)]) == 2
    assert "devflow init" in capsys.readouterr().err


def test_tickets_command_lists_state(env: Env, capsys):
    t = Ticket(id="#9", state=State.IN_REVIEW, issue=9, title="Add widget",
               sessions={"pr": "1003"}, implement_attempts=1)
    env.store.save_ticket(t)
    assert cli.main(["tickets", str(env.repo)]) == 0
    out = capsys.readouterr().out
    assert "#9" in out and "in_review" in out and "PR #1003" in out and "Add widget" in out


def test_tickets_command_with_no_database(tmp_path: Path, capsys):
    from tests.conftest import git_init

    assert cli.main(["tickets", str(git_init(tmp_path / "r"))]) == 0
    assert "no tickets yet" in capsys.readouterr().out


def test_setup_openclaw_command(monkeypatch, capsys):
    monkeypatch.setattr(openclaw_setup, "setup",
                        lambda model: openclaw_setup.SetupResult(True, f"created with {model}"))
    assert cli.main(["setup-openclaw", "--model", "m1"]) == 0
    assert "created with m1" in capsys.readouterr().out


# ---------------------------------------------------------------- dry runs leave no trace


def test_dry_run_uses_a_separate_throwaway_database(env: Env):
    from devflow.store import dry_run_db_path, state_db_path

    env.store.close()
    _, store, _ = runtime.build_engine(env.repo, host=env.host, dry_run=True)
    try:
        assert store.path == dry_run_db_path(env.repo) != state_db_path(env.repo)
        store.save_ticket(Ticket(id="#1", issue=1))
    finally:
        store.close()
    _, again, _ = runtime.build_engine(env.repo, host=env.host, dry_run=True)
    try:
        assert again.get_ticket("#1") is None  # every dry run starts from nothing
    finally:
        again.close()
    env.store = type(env.store)(state_db_path(env.repo))
    assert env.store.get_ticket("#1") is None  # and the real database never saw it


def test_dry_run_cleanup_removes_its_worktree_and_branch(env: Env):
    from tests.conftest import git

    eng = env.engine(dry_run=True)
    out = eng.run(1)
    wt = Path(out.ticket.worktree)
    assert wt.exists() and out.ticket.branch in git(env.repo, "branch", "--list")
    eng.cleanup_dry_run()
    assert not wt.exists() and out.ticket.branch not in git(env.repo, "branch", "--list")
    assert env.repo.exists() and (env.repo / ".git").exists()


def test_dry_run_cleanup_never_touches_other_branches(env: Env):
    from tests.conftest import git

    eng = env.engine(dry_run=True)
    eng.run(1)
    eng.cleanup_dry_run()
    assert "main" in git(env.repo, "branch", "--list")
