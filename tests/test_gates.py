import os
import sys
from pathlib import Path

import pytest

from devflow.gates import run_command, run_gates, scrubbed_env
from devflow.loadconfig import Config, ProjectCfg
from devflow.states import Limits

PY = f'"{sys.executable}"'


def make_cfg(root: Path, gates: dict[str, list[str]], path: str = ".") -> Config:
    return Config(
        root=root, name="r", default_branch="main", remote="", worktree_root=root / "wts",
        limits=Limits(), stage_timeout_minutes=45, gate_timeout_minutes=1,
        human_gates={}, agents={}, projects=[ProjectCfg(path, "python", gates)], github={},
    )


def test_all_gates_pass(tmp_path: Path):
    cfg = make_cfg(tmp_path, {"lint": [f"{PY} -c 'pass'"], "unit": [f"{PY} -c 'import sys'"]})
    report = run_gates(cfg, tmp_path)
    assert report.ok and [r.gate for r in report.results] == ["lint", "unit"]
    assert "typecheck" in " ".join(report.missing)


def test_failure_captures_output_and_exit_code(tmp_path: Path):
    cmd = f"{PY} -c \"print('boom detail'); raise SystemExit(3)\""
    report = run_gates(make_cfg(tmp_path, {"unit": [cmd]}), tmp_path)
    r = report.failures[0]
    assert not report.ok and r.exit_code == 3 and "boom detail" in r.output_tail
    assert report.summary().startswith("FAIL")


def test_no_gates_configured_is_not_ok(tmp_path: Path):
    assert not run_gates(make_cfg(tmp_path, {}), tmp_path).ok


def test_timeout_kills_the_whole_process_group(tmp_path: Path):
    marker = tmp_path / "child_survived"
    cmd = f"({PY} -c 'import time; time.sleep(3); open(\"{marker}\", \"w\").close()' &) ; sleep 30"
    code, _, timed_out = run_command(cmd, tmp_path, timeout_s=1)
    assert timed_out and code is None
    import time

    time.sleep(3.5)
    assert not marker.exists()


def test_fail_fast_stops_after_first_failure(tmp_path: Path):
    cfg = make_cfg(tmp_path, {"lint": ["exit 1"], "unit": [f"{PY} -c 'pass'"]})
    assert len(run_gates(cfg, tmp_path, fail_fast=True).results) == 1
    assert len(run_gates(cfg, tmp_path, fail_fast=False).results) == 2


def test_runs_in_project_subdirectory(tmp_path: Path):
    sub = tmp_path / "svc"
    sub.mkdir()
    (sub / "marker").write_text("x")
    cfg = make_cfg(tmp_path, {"unit": ["test -f marker"]}, path="svc")
    assert run_gates(cfg, tmp_path).ok


def test_secrets_are_scrubbed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for k in ("MY_API_TOKEN", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(k, "leak")
    monkeypatch.setenv("HARMLESS", "ok")
    env = scrubbed_env()
    assert "HARMLESS" in env and "PATH" in env
    assert not any(k in env for k in ("MY_API_TOKEN", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN",
                                      "ANTHROPIC_API_KEY"))
    code, out, _ = run_command("env", tmp_path, 10)
    assert code == 0 and "leak" not in out


def test_scrub_does_not_mutate_process_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SOME_TOKEN", "x")
    scrubbed_env()
    assert os.environ["SOME_TOKEN"] == "x"


def test_project_env_puts_venv_and_node_bins_first(tmp_path: Path):
    from devflow.gates import project_env

    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
    path = project_env(tmp_path, {"PATH": "/usr/bin", "VIRTUAL_ENV": "/x"})["PATH"].split(":")
    assert path[0].endswith(".venv/bin") and path[1].endswith(".bin") and path[-1] == "/usr/bin"
    assert "VIRTUAL_ENV" not in project_env(tmp_path, {"VIRTUAL_ENV": "/x"})


def test_gate_finds_tool_installed_in_project_venv(tmp_path: Path):
    tool = tmp_path / ".venv" / "bin" / "mytool"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\necho from-venv\n")
    tool.chmod(0o755)
    report = run_gates(make_cfg(tmp_path, {"unit": ["mytool"]}), tmp_path)
    assert report.ok and "from-venv" in report.results[0].output_tail


def cfg_with_setup(root: Path, setup: list[str]) -> Config:
    cfg = make_cfg(root, {})
    cfg.projects[0].setup = setup
    return cfg


def test_setup_runs_once_and_is_skipped_after_success(base_repo: Path):
    from devflow.gates import setup_worktree

    cfg = cfg_with_setup(base_repo, ["echo hi >> ran.log"])
    first = setup_worktree(cfg, base_repo)
    assert first.ok and (base_repo / "ran.log").read_text() == "hi\n"
    second = setup_worktree(cfg, base_repo)
    assert second.results == [] and (base_repo / "ran.log").read_text() == "hi\n"
    forced = setup_worktree(cfg, base_repo, force=True)
    assert forced.ok and (base_repo / "ran.log").read_text() == "hi\nhi\n"


def test_setup_marker_is_not_an_untracked_file(base_repo: Path):
    import subprocess

    from devflow.gates import setup_worktree

    setup_worktree(cfg_with_setup(base_repo, ["true"]), base_repo)
    status = subprocess.run(["git", "-C", str(base_repo), "status", "--short"],
                            capture_output=True, text=True, check=True).stdout
    assert "devflow-setup" not in status


def test_failed_setup_stops_and_leaves_no_marker(base_repo: Path):
    from devflow.gates import setup_worktree

    cfg = cfg_with_setup(base_repo, ["false", "echo never >> ran.log"])
    report = setup_worktree(cfg, base_repo)
    assert not report.ok and len(report.results) == 1 and not (base_repo / "ran.log").exists()
    cfg2 = cfg_with_setup(base_repo, ["true"])
    assert setup_worktree(cfg2, base_repo).ok


def test_changed_setup_commands_rerun(base_repo: Path):
    from devflow.gates import setup_worktree

    assert setup_worktree(cfg_with_setup(base_repo, ["echo a >> r.log"]), base_repo).ok
    assert setup_worktree(cfg_with_setup(base_repo, ["echo b >> r.log"]), base_repo).ok
    assert (base_repo / "r.log").read_text() == "a\nb\n"
