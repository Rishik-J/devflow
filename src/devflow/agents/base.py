"""Common types and helpers for agent drivers.

A driver turns an AgentRequest into one headless CLI run and an AgentResult. Drivers never
decide anything: the engine picks the stage, the driver only runs it and reports facts
(text, session id, cost, whether it succeeded).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .. import schema, telemetry
from ..gates import scrubbed_env


class AgentError(RuntimeError):
    """The agent could not be run, or its output could not be used."""


class AgentTimeout(AgentError):
    pass


# Tools every write-capable run is denied, whatever the role asks for: only the engine pushes,
# merges, or talks to GitHub.
DEFAULT_DENY = ("Bash(git push:*)", "Bash(gh:*)", "Bash(git remote:*)")
READ_TOOLS = ("Read", "Grep", "Glob", "Bash(git diff:*)", "Bash(git log:*)",
              "Bash(git show:*)", "Bash(git status:*)", "Bash(graphify:*)")
WRITE_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write", "Bash")


@dataclass
class AgentRequest:
    role: str
    prompt: str
    cwd: Path
    system: str = ""
    schema: str | None = None
    session: str | None = None
    read_only: bool = True
    timeout_s: float = 2700
    model: str | None = None
    max_budget_usd: float | None = None
    mcp: dict | None = None
    mcp_overrides: list[str] = field(default_factory=list)
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_DENY))


@dataclass
class AgentResult:
    text: str
    session: str | None = None
    cost_usd: float | None = None
    duration_s: float = 0.0
    ok: bool = True
    error: str = ""
    data: Any = None
    raw: dict = field(default_factory=dict)


class Agent(Protocol):
    name: str

    def run(self, req: AgentRequest) -> AgentResult: ...


@dataclass
class ProcResult:
    code: int | None
    out: str
    err: str
    timed_out: bool = False


Runner = Callable[[list[str], Path, dict[str, str], float, str | None], ProcResult]


def run_process(
    cmd: list[str], cwd: Path, env: dict[str, str], timeout_s: float, stdin_text: str | None
) -> ProcResult:
    """Run a command in its own process group; kill the whole group on timeout."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout_s)
        return ProcResult(proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return ProcResult(None, out or "", err or "", timed_out=True)


def agent_env(keep_prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    """Scrub secrets from the environment, except the variables this agent itself needs."""
    env = scrubbed_env()
    for key, value in os.environ.items():
        if key.startswith(keep_prefixes):
            env[key] = value
    return env


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any | None:
    """Pull the JSON object or array out of an agent's reply, tolerating prose and code fences."""
    text = text.strip()
    candidates = [text, *(m.strip() for m in _FENCE.findall(text))]
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(text[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict | list):
                return value
    return None


REPAIR_PROMPT = (
    "Your last reply could not be used: {problems}\n"
    "Reply again with ONLY the corrected JSON object. No prose, no code fences."
)


def run_structured(
    agent: Agent, req: AgentRequest, root: Path | None = None, *, max_repairs: int = 2
) -> AgentResult:
    """Run an agent and require schema-valid JSON, asking it to repair its output if needed.

    Raises AgentError if the run failed or the output is still invalid after the repairs.
    """
    if not req.schema:
        raise ValueError("run_structured needs req.schema")
    current = req
    problems: list[str] = []
    for _ in range(max_repairs + 1):
        result = agent.run(current)
        if not result.ok:
            raise AgentError(f"{agent.name} {req.role} failed: {result.error or 'unknown error'}")
        data = extract_json(result.text)
        problems = ["no JSON object found in the reply"] if data is None else schema.errors(
            req.schema, data, root
        )
        if not problems:
            result.data = data
            return result
        telemetry.event("schema.repair", **{"devflow.role": req.role,
                                          "devflow.validation_errors": len(problems)})
        shown = "; ".join(problems[:4])
        if result.session:
            current = _with(req, prompt=REPAIR_PROMPT.format(problems=shown),
                            session=result.session)
        else:
            current = _with(
                req,
                prompt=f"{req.prompt}\n\nYour previous reply was:\n{result.text}\n\n"
                + REPAIR_PROMPT.format(problems=shown),
            )
    raise AgentError(
        f"{agent.name} {req.role}: output still invalid after {max_repairs} repairs: "
        + "; ".join(problems[:4])
    )


def _with(req: AgentRequest, **changes: Any) -> AgentRequest:
    return AgentRequest(**{**req.__dict__, **changes})


def tail(text: str, n: int = 400) -> str:
    return text.strip()[-n:]
