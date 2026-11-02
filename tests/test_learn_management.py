import json

import pytest

from devflow import cli, gitops
from devflow.context import ledger_path
from devflow.learn.ledger import Ledger
from devflow.learn.publish import ALLOWED, publish
from tests.test_engine import Env


@pytest.fixture
def env(tmp_path):
    instance = Env(tmp_path)
    yield instance
    instance.store.close()


def seed(env):
    ledger = Ledger(ledger_path(env.repo))
    lesson = ledger.add(type="gotcha", statement="Clear environment variables in configuration tests",
                        scope=["tests/**"], ticket=1, evidence="review finding")
    ledger.save()
    return lesson.id


def test_commands_promote_and_retire_candidates(env, capsys):
    ident = seed(env)
    assert cli.main(["learn", "list", str(env.repo)]) == 0
    assert ident in capsys.readouterr().out
    cli.main(["learn", "approve", ident, str(env.repo)])
    assert Ledger(ledger_path(env.repo)).lessons[ident].status == "active"
    cli.main(["learn", "reject", ident, str(env.repo)])
    assert Ledger(ledger_path(env.repo)).lessons[ident].status == "retired"


def test_context_pr_excludes_runtime_data_and_keeps_main_checkout(env):
    seed(env)
    before = gitops.head_sha(env.repo)
    pr = publish(env.cfg, env.host)
    assert pr and pr.head.startswith("devflow/context-")
    remote = env.tmp / "origin.git"
    files = gitops.git(remote, "diff", "--name-only", f"main..{pr.head}").splitlines()
    assert set(files) == set(ALLOWED)
    assert gitops.head_sha(env.repo) == before
    assert not list(env.cfg.worktree_root.glob("context-*"))
    assert "candidate" in gitops.git(remote, "show", f"{pr.head}:.devflow/learnings/ledger.jsonl")


def test_publishing_same_snapshot_reuses_branch(env):
    seed(env)
    first = publish(env.cfg, env.host)
    second = publish(env.cfg, env.host)
    assert first.head == second.head
    assert len(env.host.prs) == 1


def test_publish_rejects_context_symlink(env, tmp_path):
    brief = env.repo / ALLOWED[0]
    brief.unlink()
    outside = tmp_path / "outside"
    outside.write_text("not context")
    brief.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        publish(env.cfg, env.host)


def test_stats_summarises_existing_runs_without_model_calls(env, capsys):
    seed(env)
    cli.main(["stats", str(env.repo)])
    report = json.loads(capsys.readouterr().out)
    assert report["tickets"] == 0 and report["lessons"]["candidate"] == 1
