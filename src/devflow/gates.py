"""Run the repo's gates in a ticket worktree. The result is facts, not an agent's claim.

Gate commands come from the main checkout's config (see loadconfig), never from the worktree,
and run with secrets scrubbed from the environment.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import telemetry
from .detect import GATES
from .loadconfig import Config

TAIL_CHARS = 6000
SECRET_PATTERNS = (
    "*TOKEN*", "*SECRET*", "*PASSWORD*", "*PASSWD*", "*API_KEY*", "*APIKEY*", "*CREDENTIAL*",
    "AWS_*", "GH_*", "GITHUB_*", "ANTHROPIC_*", "OPENAI_*", "GOOGLE_*", "SUPABASE_*",
    "DATABASE_URL", "*_DSN", "SSH_*", "LANGFUSE_*", "OTEL_*", "DEVFLOW_TELEMETRY_*",
)


def scrubbed_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if any(fnmatch.fnmatch(key.upper(), pat) for pat in SECRET_PATTERNS):
            del env[key]
    return env


def project_env(cwd: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """Scrubbed env with the project's own tool directories first on PATH."""
    env = scrubbed_env(base)
    env.pop("VIRTUAL_ENV", None)
    # Stale .pyc files can make a fixed bug look unfixed (same size, same second), and they
    # would pollute the worktree. Gates always run against the source.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    tool_dirs = [str(p) for p in (cwd / ".venv" / "bin", cwd / "node_modules" / ".bin")
                 if p.is_dir()]
    if tool_dirs:
        env["PATH"] = os.pathsep.join([*tool_dirs, env.get("PATH", "")])
    return env


def _setup_marker(worktree: Path) -> Path:
    proc = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--absolute-git-dir"],
        capture_output=True, text=True, check=False,
    )
    git_dir = Path(proc.stdout.strip()) if proc.returncode == 0 else worktree
    return git_dir / "devflow-setup-ok"


def setup_worktree(
    cfg: Config, worktree: Path, *, timeout_s: float = 1800, force: bool = False
) -> GateReport:
    """Install each project's dependencies in the worktree. Skipped once it has succeeded.

    The marker lives in the worktree's git dir, so it never shows up as an untracked file.
    """
    commands = [(p.path, c) for p in cfg.projects for c in p.setup]
    digest = hashlib.sha256(json.dumps(commands).encode()).hexdigest()
    marker = _setup_marker(worktree)
    report = GateReport()
    if not commands:
        return report
    if not force and marker.exists() and marker.read_text().strip() == digest:
        return report
    for path, command in commands:
        start = time.monotonic()
        code, out, timed_out = run_command(command, worktree / path, timeout_s, project_env(worktree / path))
        report.results.append(GateResult(
            project=path, gate="setup", command=command, ok=code == 0 and not timed_out,
            exit_code=code, duration_s=round(time.monotonic() - start, 2),
            timed_out=timed_out, output_tail=out[-TAIL_CHARS:],
        ))
        if code != 0 or timed_out:
            return report
    marker.write_text(digest)
    return report


@dataclass
class GateResult:
    project: str
    gate: str
    command: str
    ok: bool
    exit_code: int | None
    duration_s: float
    timed_out: bool = False
    output_tail: str = ""


@dataclass
class GateReport:
    results: list[GateResult] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    priority: list[GateResult] = field(default_factory=list)  # diagnostics only

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    @property
    def failures(self) -> list[GateResult]:
        return [r for r in self.results if not r.ok]

    def summary(self) -> str:
        lines = [f"{'PASS' if r.ok else 'FAIL'} {r.project}:{r.gate}  {r.command}"
                 for r in self.results]
        lines += [f"SKIP {m} (no command configured)" for m in self.missing]
        return "\n".join(lines)


def run_command(
    command: str, cwd: Path, timeout_s: float, env: dict[str, str] | None = None
) -> tuple[int | None, str, bool]:
    """Run a shell command in its own process group; kill the whole group on timeout."""
    with telemetry.span("tool.shell", {"devflow.command_kind": "gate_or_setup",
                                       "langfuse.observation.type": "tool"}) as trace:
        trace.content(input={"command": command, "cwd": str(cwd), "timeout_s": timeout_s})
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command], cwd=cwd, env=env if env is not None else scrubbed_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=timeout_s)
            trace.set(**{"process.exit_code": proc.returncode})
            trace.content(output={"output": out, "exit_code": proc.returncode, "timed_out": False})
            if proc.returncode != 0:
                trace.fail()
            return proc.returncode, out, False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            trace.content(output={"output": out or "", "timed_out": True})
            trace.set(**{"devflow.timed_out": True})
            trace.fail()
            return None, out or "", True


def _is_bare_pytest(command: str) -> bool:
    """True for `pytest ...` / `python -m pytest ...` with options only, no path arguments."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if any(w in ("&&", "||", ";", "|", "&") or any(c in w for c in ";|&<>$`()") for w in words):
        return False
    if (len(words) >= 3 and re.fullmatch(r"python[\d.]*", Path(words[0]).name)
            and words[1:3] == ["-m", "pytest"]):
        rest = words[3:]
    elif words[:1] == ["pytest"]:
        rest = words[1:]
    else:
        return False
    skip_value = False
    for w in rest:
        if skip_value:
            skip_value = False
            continue
        if w.startswith("-"):
            if w in ("-k", "-m", "-c", "-p", "-o", "--maxfail", "--tb", "-n", "--ignore",
                     "--deselect", "--rootdir", "--junitxml"):
                skip_value = True
            continue
        return False  # a path or node id
    return True


def _priority_command(command: str, cwd: Path, impacted: list[str]) -> str | None:
    if not impacted or not _is_bare_pytest(command):
        return None
    existing = [p for p in impacted if (cwd / p).is_file()]
    if not existing:
        return None
    return command + " " + " ".join(shlex.quote(p) for p in existing)


def run_gates(
    cfg: Config, worktree: Path, *, gates: Iterable[str] = GATES, fail_fast: bool = False,
    timeout_s: float | None = None, impacted_tests: list[str] | None = None,
    priority_check: Callable[[GateResult], bool] | None = None,
) -> GateReport:
    report = GateReport()
    timeout = timeout_s if timeout_s is not None else cfg.gate_timeout_minutes * 60
    for gate in gates:
        for project in cfg.projects:
            cmds = project.gates.get(gate)
            if not cmds:
                report.missing.append(f"{project.path}:{gate}")
                continue
            cwd = worktree / project.path
            for command in cmds:
                start = time.monotonic()
                prio_cmd = _priority_command(command, cwd, impacted_tests or [])
                if prio_cmd is not None:
                    code, out, timed_out = run_command(prio_cmd, cwd, timeout, project_env(cwd))
                    prio = GateResult(
                        project=project.path, gate=gate, command=command,
                        ok=code == 0 and not timed_out, exit_code=code,
                        duration_s=round(time.monotonic() - start, 2), timed_out=timed_out,
                        output_tail=out[-TAIL_CHARS:],
                    )
                    report.priority.append(prio)
                    short = bool(not prio.ok and priority_check is not None and priority_check(prio))
                    telemetry.event("gate.order", **{
                        "devflow.gate": gate, "devflow.project": project.path,
                        "devflow.impacted_tests": len(impacted_tests or []),
                        "devflow.priority_ran": True, "devflow.priority_ok": prio.ok,
                        "devflow.short_circuited": short})
                    if short:
                        report.results.append(prio)
                        if fail_fast:
                            return report
                        continue
                    start = time.monotonic()
                else:
                    telemetry.event("gate.order", **{
                        "devflow.gate": gate, "devflow.project": project.path,
                        "devflow.impacted_tests": len(impacted_tests or []),
                        "devflow.priority_ran": False, "devflow.priority_ok": False,
                        "devflow.short_circuited": False})
                code, out, timed_out = run_command(command, cwd, timeout, project_env(cwd))
                result = GateResult(
                    project=project.path, gate=gate, command=command,
                    ok=code == 0 and not timed_out, exit_code=code,
                    duration_s=round(time.monotonic() - start, 2), timed_out=timed_out,
                    output_tail=out[-TAIL_CHARS:],
                )
                telemetry.event("gate.result", **{"devflow.gate": gate,
                                                 "devflow.project": project.path,
                                                 "devflow.ok": result.ok,
                                                 "devflow.duration_s": result.duration_s,
                                                 "devflow.timed_out": result.timed_out})
                report.results.append(result)
                if fail_fast and not result.ok:
                    return report
    return report
