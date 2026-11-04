"""Preflight checks run against fakes; nothing here touches the real machine."""
import json
import subprocess
from pathlib import Path

import pytest

from devflow import preflight
from devflow.agents import FakeAgent
from devflow.loadconfig import BoardCfg
from devflow.preflight import Check, blocking_failures, summarize
from devflow.router import Router
from devflow.store import Store, state_db_path
from tests.test_engine import Env

NOW = 1_000_000.0


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path / "case")
    yield e
    e.store.close()


def done(code=0, out=""):
    return subprocess.CompletedProcess([], code, out, "")


def gh_runner(*, auth=0, limits=None):
    def run(cmd, **kw):
        if cmd[1:3] == ["auth", "status"]:
            return done(auth)
        if cmd[1:3] == ["api", "rate_limit"]:
            if limits is None:
                return done(1)
            return done(0, json.dumps({"resources": limits}))
        raise AssertionError(cmd)
    return run


def limits(graphql=4000, core=4000, reset=NOW + 600):
    return {"graphql": {"remaining": graphql, "reset": reset}, "core": {"remaining": core, "reset": reset}}


def has_everything(tool):
    return "/usr/bin/" + tool


def test_gh_missing_or_logged_out_blocks():
    assert not preflight.check_gh(gh_runner(), lambda t: None).ok
    assert not preflight.check_gh(gh_runner(auth=1), has_everything).ok
    assert preflight.check_gh(gh_runner(), has_everything).ok


def test_exhausted_graphql_quota_blocks_only_when_a_board_is_configured():
    runner = gh_runner(limits=limits(graphql=5))
    blocked = preflight.check_rate_limit(runner, True, lambda: NOW)
    assert not blocked.ok and "graphql quota nearly exhausted" in blocked.message and "10 min" in blocked.message
    assert preflight.check_rate_limit(runner, False, lambda: NOW).ok  # REST-only repos are unaffected


def test_unreadable_rate_limit_never_blocks():
    check = preflight.check_rate_limit(gh_runner(limits=None), True, lambda: NOW)
    assert check.ok and not check.blocking


def test_stop_file_blocks(tmp_path: Path):
    assert preflight.check_stop_file(tmp_path).ok
    (tmp_path / ".devflow").mkdir()
    (tmp_path / ".devflow" / "STOP").write_text("")
    assert not preflight.check_stop_file(tmp_path).ok


def test_openclaw_only_checked_when_configured(env):
    cfg = env.cfg
    assert preflight.check_openclaw(cfg, lambda t: None) == []  # no openclaw roles in this config
    cfg.agents["planner"] = "openclaw:devflow"
    assert not preflight.check_openclaw(cfg, lambda t: None)[0].ok
    assert preflight.check_openclaw(cfg, has_everything)[0].ok


def test_no_available_coding_agent_blocks_with_a_wait_time(tmp_path: Path):
    store = Store(state_db_path(tmp_path))
    from devflow.loadconfig import RoutingCfg
    router = Router(store, RoutingCfg(), {"claude": FakeAgent(name="claude")}, now=lambda: NOW,
                    ready=lambda a: (True, ""))
    assert all(c.ok for c in preflight.check_agents(router, lambda: NOW))
    store.set_cooldown("claude", NOW + 1800, "usage limit", 1)
    checks = preflight.check_agents(router, lambda: NOW)
    assert checks[-1].name == "coding agents" and not checks[-1].ok and "30 min" in checks[-1].message


def test_summary_lists_only_blocking_failures():
    checks = [Check("a", True), Check("b", False, "broke", fix="fix b"), Check("c", False, blocking=False)]
    assert [c.name for c in blocking_failures(checks)] == ["b"]
    assert summarize(checks) == "b: broke (fix b)"


def test_run_checks_end_to_end_on_a_healthy_fake_machine(env):
    router = Router(env.store, env.cfg.routing, {"claude": FakeAgent(name="claude")},
                    now=lambda: NOW, ready=lambda a: (True, ""))
    env.cfg.board = BoardCfg()
    checks = preflight.run_checks(env.cfg, router, env.repo, runner=gh_runner(limits=limits()),
                                  which=has_everything, now=lambda: NOW)
    assert not blocking_failures(checks), summarize(checks)
