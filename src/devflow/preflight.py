"""Checks run before a ticket starts, so a broken environment pauses it instead of wasting money.

Every check is a small function over injectable inputs (`runner`, `which`, `now`), so the same
code backs `devflow doctor` and the engine, and tests never touch the real machine.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .loadconfig import Config
from .router import Router

MIN_GRAPHQL_REMAINING = 50
MIN_CORE_REMAINING = 20
MIN_FREE_BYTES = 500 * 1024 * 1024


@dataclass
class Check:
    name: str
    ok: bool
    message: str = ""
    blocking: bool = True  # only matters when not ok
    fix: str = ""

    @property
    def failed_blocking(self) -> bool:
        return not self.ok and self.blocking


Runner = Callable[..., subprocess.CompletedProcess]


def _gh(runner: Runner, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return runner(["gh", *args], capture_output=True, text=True, check=False, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None


def check_stop_file(root: Path) -> Check:
    stop = root / ".devflow" / "STOP"
    return Check("stop file", not stop.exists(), "STOP file present" if stop.exists() else "",
                 fix="remove .devflow/STOP to let devflow run")


def check_gh(runner: Runner, which: Callable[[str], str | None]) -> Check:
    if not which("gh"):
        return Check("gh", False, "gh is not installed", fix="install the GitHub CLI")
    done = _gh(runner, "auth", "status")
    ok = done is not None and done.returncode == 0
    return Check("gh authenticated", ok, "" if ok else "gh is not logged in", fix="run `gh auth login`")


def check_rate_limit(runner: Runner, graphql_needed: bool, now: Callable[[], float]) -> Check:
    done = _gh(runner, "api", "rate_limit")
    if done is None or done.returncode != 0:
        return Check("github rate limit", True, "could not read the rate limit", blocking=False)
    try:
        resources = json.loads(done.stdout)["resources"]
        core, graphql = resources["core"], resources["graphql"]
    except (ValueError, KeyError, TypeError):
        return Check("github rate limit", True, "unexpected rate limit response", blocking=False)
    for label, bucket, floor, needed in (("graphql", graphql, MIN_GRAPHQL_REMAINING, graphql_needed),
                                         ("core", core, MIN_CORE_REMAINING, True)):
        if needed and bucket.get("remaining", floor) < floor:
            wait = max(0, int(bucket.get("reset", now()) - now()))
            return Check("github rate limit", False,
                         f"{label} quota nearly exhausted ({bucket.get('remaining')} left; "
                         f"resets in {wait // 60} min)",
                         fix="wait for the reset, or stop other tools and pollers using this token")
    return Check("github rate limit", True, f"graphql {graphql.get('remaining')} left")


def check_agents(router: Router, now: Callable[[], float]) -> list[Check]:
    out = []
    statuses = router.statuses()
    for status in statuses:
        ok = status.ready and status.cooldown_until is None and status.headroom > 0
        why = status.reason or ("cooling down" if status.cooldown_until else "out of budget")
        out.append(Check(f"agent {status.name}", ok, "" if ok else why, blocking=False))
    if statuses and not any(s.eligible for s in statuses):
        waits = [s.cooldown_until for s in statuses if s.cooldown_until]
        when = f"; first available in {int(min(waits) - now()) // 60} min" if waits else ""
        out.append(Check("coding agents", False, "no coding agent is available" + when,
                         fix="wait for a cooldown or log in to an agent"))
    return out


def check_openclaw(cfg: Config, which: Callable[[str], str | None]) -> list[Check]:
    if not any(spec.startswith("openclaw") for spec in cfg.agents.values()):
        return []
    found = which("openclaw") is not None
    return [Check("openclaw", found, "" if found else "openclaw is not on PATH",
                  fix="install OpenClaw and run `devflow setup-openclaw`")]


def check_disk(cfg: Config) -> Check:
    target = cfg.worktree_root
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        free = shutil.disk_usage(target).free
    except OSError:
        return Check("worktree disk", True, "could not read free space", blocking=False)
    return Check("worktree disk", free >= MIN_FREE_BYTES,
                 f"{free // (1024 * 1024)} MB free at {target}", fix="free some disk space")


def run_checks(cfg: Config, router: Router, root: Path, *, runner: Runner = subprocess.run,
               which: Callable[[str], str | None] = shutil.which,
               now: Callable[[], float] = time.time) -> list[Check]:
    checks = [check_stop_file(root), check_gh(runner, which),
              check_rate_limit(runner, cfg.board.enabled, now)]
    checks += check_agents(router, now)
    checks += check_openclaw(cfg, which)
    checks.append(check_disk(cfg))
    return checks


def blocking_failures(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.failed_blocking]


def summarize(checks: list[Check]) -> str:
    return "; ".join(f"{c.name}: {c.message or 'failed'}" + (f" ({c.fix})" if c.fix else "")
                     for c in blocking_failures(checks))

