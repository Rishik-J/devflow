"""Cursor Agent driver: `agent -p --output-format json`, with chat resume.

Verified live (see tests/test_live_agents.py). Headless runs refuse an untrusted directory, so
every run passes `--trust`: the directories are worktrees devflow created itself. Read-only runs
use `--mode ask` (no edits, no commands). Write runs use `--force` inside `--sandbox enabled`. Cursor reads MCP servers from the project, so the graphify MCP server is
not injected here (that would mean writing files into the ticket worktree); the graph digest in
the prompt and the `graphify` CLI cover it.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from .. import telemetry
from .base import (
    AgentError,
    AgentRequest,
    AgentResult,
    AgentTimeout,
    Runner,
    agent_env,
    extract_json,
    run_process,
    tail,
)

LOGIN_HINT = "Cursor agent is not logged in. Run `agent login` once on this machine."
_NOT_LOGGED_IN = ("not logged in", "please log in", "run `agent login`", "authentication required")
READY_CACHE_S = 60.0


class CursorAgent:
    name = "cursor"

    def __init__(self, binary: str | None = None, runner: Runner = run_process):
        self.binary = binary or shutil.which("agent") or shutil.which("cursor-agent") or "agent"
        self.runner = runner
        self._ready: tuple[float, bool, str] | None = None

    def check_ready(self) -> tuple[bool, str]:
        """Installed and logged in? Cached briefly so routing does not spawn it every pick."""
        now = time.monotonic()
        if self._ready and now - self._ready[0] < READY_CACHE_S:
            return self._ready[1], self._ready[2]
        if not shutil.which(self.binary) and not Path(self.binary).exists():
            ok, why = False, "cursor agent not installed"
        else:
            try:
                proc = subprocess.run(
                    [self.binary, "status"], capture_output=True, text=True, timeout=20,
                    check=False,
                )
                text = (proc.stdout + proc.stderr).lower()
                ok = proc.returncode == 0 and not any(s in text for s in _NOT_LOGGED_IN)
                why = "" if ok else LOGIN_HINT
            except (subprocess.SubprocessError, OSError) as exc:
                ok, why = False, f"cursor status failed: {exc}"
        self._ready = (now, ok, why)
        return ok, why

    def build(self, req: AgentRequest) -> list[str]:
        cmd = [self.binary, "-p", "--trust", "--output-format", "json"]
        if req.read_only:
            cmd += ["--mode", "ask"]
        else:
            cmd += ["--force", "--sandbox", "enabled"]
        if req.session:
            cmd += ["--resume", req.session]
        if req.model:
            cmd += ["--model", req.model]
        prompt = f"{req.system}\n\n---\n\n{req.prompt}" if req.system else req.prompt
        return [*cmd, prompt]

    @telemetry.agent_call
    def run(self, req: AgentRequest) -> AgentResult:
        proc = self.runner(self.build(req), req.cwd, agent_env(("CURSOR_",)), req.timeout_s, None)
        if proc.timed_out:
            raise AgentTimeout(f"cursor {req.role} timed out after {req.timeout_s:.0f}s")
        combined = (proc.out + "\n" + proc.err).lower()
        if any(s in combined for s in _NOT_LOGGED_IN):
            raise AgentError(LOGIN_HINT)
        data = extract_json(proc.out)
        if not isinstance(data, dict):
            raise AgentError(
                f"cursor {req.role}: unreadable output (exit {proc.code}): "
                f"{tail(proc.err or proc.out)}"
            )
        text = data.get("result", data.get("text", data.get("response", "")))
        failed = bool(data.get("is_error")) or data.get("subtype", "success") != "success"
        ok = not failed and proc.code == 0
        if ok:
            error = ""
        elif failed:
            error = f"{data.get('subtype', 'error')}: {tail(str(text))}"
        else:
            error = f"exit {proc.code}: {tail(proc.err)}"
        return AgentResult(
            text=str(text or ""),
            session=data.get("session_id") or data.get("chatId") or req.session,
            cost_usd=None,
            duration_s=(data.get("duration_ms") or 0) / 1000,
            ok=ok,
            error=error,
            raw={"usage": _usage(data.get("usage")),
                 **{k: data[k] for k in ("request_id", "subtype") if k in data}},
        )


def _usage(usage: object) -> dict[str, int]:
    """Cursor reports camelCase token counts; the router reads input_tokens/output_tokens."""
    if not isinstance(usage, dict):
        return {}
    return {
        "input_tokens": int(usage.get("inputTokens", usage.get("input_tokens", 0)) or 0),
        "output_tokens": int(usage.get("outputTokens", usage.get("output_tokens", 0)) or 0),
    }
