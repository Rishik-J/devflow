"""A sandbox that replicates the real environment closely enough to run devflow unmodified.

Real: git repo + bare origin, worktrees, `devflow init`, the config file, the SQLite store, the
engine, router, agent drivers (subprocess, stdin, output parsing), gates run as shell commands,
GhHost, preflight, and the CLI entry point.
Stand-in: the four external executables (claude, codex, openclaw, gh) on PATH, scripted per test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from devflow import runtime
from devflow.init import run_init
from devflow.store import Store, state_db_path
from tests.conftest import commit_all, git, git_init
from tests.test_engine import APPROVE, CALC, CALC_MULT, PLAN, TEST_MULT, TEST_PLAN, UNIT

STUB = Path(__file__).parent / "stub_cli.py"
GOOD_PLAN_REVIEW = {"decision": "approve", "reason": "The plan matches the repository."}
GOOD_CODER = {"write": {"src/calc.py": CALC_MULT, "tests/test_mult.py": TEST_MULT},
              "reply": "implemented multiply", "cost": 0.5, "tokens": [2000, 300]}
GITHUB_ISSUE = {"title": "Add multiply", "body": "Please add a multiply function.", "state": "OPEN",
                "labels": [], "author": "alice"}


class Sandbox:
    def __init__(self, tmp: Path, monkeypatch, *, plan_gate=False, merge_gate=False, extra_config="",
                 gates=None, retro=False, routing_agents=("claude", "codex"), reviewer="openclaw:devflow",
                 limits: dict | None = None, agent_caps: dict | None = None):
        self.tmp = tmp
        self.data = tmp / "sandbox"
        self.bin = tmp / "bin"
        self.data.mkdir(parents=True)
        self.bin.mkdir(parents=True)
        for tool in ("claude", "codex", "openclaw", "gh"):
            wrapper = self.bin / tool
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STUB}" {tool} "{self.data}" "$@"\n')
            wrapper.chmod(0o755)
        # Hermetic PATH: only the stand-ins, git and system basics. A sandbox test must never be able
        # to reach a real claude/codex/openclaw/gh that happens to be installed on this machine.
        system = [d for d in ("/usr/bin", "/bin", "/usr/sbin", "/sbin") if Path(d).is_dir()]
        monkeypatch.setenv("PATH", os.pathsep.join([str(self.bin), str(Path(sys.executable).parent), *system]))
        _skip_openclaw_retry_delay(monkeypatch)
        (self.data / "scenario.json").write_text("{}")
        self.write_gh({"user": "devflow-bot", "issues": {"1": dict(GITHUB_ISSUE)}, "comments": {}, "prs": {}})

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
        (self.repo / ".gitignore").write_text("__pycache__/\n")
        commit_all(self.repo, "base")
        git(self.repo, "remote", "add", "origin", str(origin))
        git(self.repo, "push", "-q", "-u", "origin", "main")
        run_init(self.repo, use_graph=False, which={"claude": "/c", "codex": "/x"}.get)
        agents = ['coder = "claude"', f'reviewer = "{reviewer}"', 'planner = "openclaw:devflow"',
                  'triage = "openclaw:devflow"'] + (['retro = "openclaw:devflow"'] if retro else [])
        caps = agent_caps or {}
        routing = "".join(
            f"[routing.agents.{n}]\npriority = {i + 1}\n"
            + (f"capabilities = {json.dumps(caps[n])}\n" if n in caps else "")
            for i, n in enumerate(routing_agents))
        extra_limits = "".join(f"{k} = {v}\n" for k, v in (limits or {}).items())
        gates = gates or {"unit": [UNIT]}
        (self.repo / ".devflow" / "config.toml").write_text(
            f'[repo]\nname = "widgets"\ndefault_branch = "main"\nremote = "https://github.com/acme/widgets.git"\n'
            f"worktree_root = {json.dumps(str(tmp / 'wts'))}\n"
            "[limits]\nmax_review_rounds = 2\nmax_implement_attempts = 2\nstage_timeout_minutes = 5\n"
            "gate_timeout_minutes = 1\n" + extra_limits +
            f"[human_gates]\nplan_approval = {str(plan_gate).lower()}\nmerge_approval = {str(merge_gate).lower()}\n"
            'approvers = ["alice"]\n'
            "[learning]\nenabled = " + ("true" if retro else "false") + "\n"
            "[agents]\n" + "\n".join(agents) + '\n[routing]\nroles = ["coder", "reviewer", "test_planner"]\n'
            + routing + '[[project]]\npath = "."\nkind = "python"\n[project.gates]\n'
            + "\n".join(f"{g} = {json.dumps(c)}" for g, c in gates.items()) + "\n" + extra_config)

    # ---------------------------------------------------------------- scripting
    def script(self, tool: str, role: str, *actions: dict) -> Sandbox:
        scenario = json.loads((self.data / "scenario.json").read_text())
        scenario.setdefault(tool, {}).setdefault(role, []).extend(actions)
        (self.data / "scenario.json").write_text(json.dumps(scenario))
        return self

    def happy(self) -> Sandbox:
        """Everything the autonomous happy path needs, in one call."""
        return (self.script("openclaw", "plan", {"reply": PLAN})
                .script("openclaw", "plan_review", {"reply": GOOD_PLAN_REVIEW})
                .script("claude", "test_plan", {"reply": TEST_PLAN, "cost": 0.1})
                .script("claude", "coder", GOOD_CODER).script("openclaw", "review", {"reply": APPROVE}))

    def reset_cursor(self) -> None:
        (self.data / "cursor.json").unlink(missing_ok=True)

    # ---------------------------------------------------------------- github state
    def gh_state(self) -> dict:
        return json.loads((self.data / "gh_state.json").read_text())

    def write_gh(self, state: dict) -> None:
        (self.data / "gh_state.json").write_text(json.dumps(state))

    def set_gh(self, **changes) -> None:
        state = self.gh_state()
        state.update(changes)
        self.write_gh(state)

    def say(self, who: str, text: str, number: int = 1) -> None:
        state = self.gh_state()
        state["next_id"] = state.get("next_id", 1000) + 1
        state["comments"].setdefault(str(number), []).append(
            {"id": state["next_id"], "user": {"login": who}, "body": text,
             "created_at": "2026-11-06T13:00:00+00:00"})
        self.write_gh(state)

    # ---------------------------------------------------------------- running
    def run(self, issue: int = 1):
        engine, store, _ = runtime.build_engine(self.repo)
        try:
            return engine.run(issue)
        finally:
            store.close()

    def store(self) -> Store:
        return Store(state_db_path(self.repo))

    def calls(self, tool: str | None = None, role: str | None = None) -> list[dict]:
        path = self.data / "calls.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [r for r in rows if (tool is None or r["tool"] == tool) and (role is None or r.get("role") == role)]

    def agent_calls(self) -> list[dict]:
        return [r for r in self.calls() if r["tool"] != "gh"]

    def ticket(self, issue: int = 1):
        with self.store() as store:
            return store.get_ticket(f"#{issue}")

    def events(self, kind: str, issue: int = 1) -> list[dict]:
        with self.store() as store:
            return store.events(f"#{issue}", kind)

    def usage(self, issue: int = 1) -> list[tuple]:
        with self.store() as store:
            return [tuple(r) for r in store._db.execute(
                "SELECT agent, role, cost_usd, tokens_in, tokens_out FROM agent_usage WHERE ticket_id=? ORDER BY id",
                (f"#{issue}",))]

    def comments(self, number: int = 1) -> list[str]:
        return [c["body"] for c in self.gh_state()["comments"].get(str(number), [])]

    def remote_branches(self) -> str:
        return git(self.repo, "ls-remote", "--heads", "origin")

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        """Run the real `devflow` entry point as a separate process, with the stand-ins on PATH."""
        return subprocess.run([sys.executable, "-m", "devflow.cli", *args], capture_output=True, text=True,
                              cwd=self.repo, timeout=180, env={**os.environ}, check=False)


def _skip_openclaw_retry_delay(monkeypatch) -> None:
    """The real driver pauses 20s between retries. The delay is a constructor default bound at import
    time, so patch the defaults (not the time module) to keep outage tests fast but otherwise real."""
    from devflow.agents.openclaw import OpenClawAgent
    init = OpenClawAgent.__init__
    names = init.__code__.co_varnames[:init.__code__.co_argcount]
    count = len(init.__defaults__)
    defaults = dict(zip(names[-count:], init.__defaults__, strict=True))
    defaults.update(retry_delay_s=0.0, sleep=lambda _seconds: None)
    monkeypatch.setattr(init, "__defaults__", tuple(defaults[n] for n in names[-count:]))
