"""Codex driver: `codex exec --json`, with session (thread) resume.

Codex has no system-prompt flag, so the role prompt is prepended to the task. The sandbox is
`read-only` or `workspace-write`; network and writes outside the worktree stay blocked.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .. import telemetry
from .base import (
    AgentError,
    AgentRequest,
    AgentResult,
    AgentTimeout,
    Runner,
    agent_env,
    run_process,
    tail,
)


class CodexAgent:
    name = "codex"

    def __init__(self, binary: str = "codex", runner: Runner = run_process):
        self.binary = binary
        self.runner = runner

    def build(self, req: AgentRequest, out_file: Path) -> list[str]:
        sandbox = "read-only" if req.read_only else "workspace-write"
        cmd = [self.binary, "exec"]
        if req.session:
            # `resume` does not take -C/-s; the sandbox is set through config and the
            # working directory comes from the subprocess.
            cmd += ["resume", req.session, "-c", f'sandbox_mode="{sandbox}"']
        else:
            cmd += ["-C", str(req.cwd), "-s", sandbox]
        cmd += ["--json", "-o", str(out_file)]
        if req.model:
            cmd += ["-m", req.model]
        for override in req.mcp_overrides:
            cmd += ["-c", override]
        prompt = f"{req.system}\n\n---\n\n{req.prompt}" if req.system else req.prompt
        return [*cmd, prompt]

    @telemetry.agent_call
    def run(self, req: AgentRequest) -> AgentResult:
        with tempfile.TemporaryDirectory(prefix="devflow-codex-") as tmp:
            out_file = Path(tmp) / "last.txt"
            proc = self.runner(self.build(req, out_file), req.cwd,
                               agent_env(("OPENAI_", "CODEX_")), req.timeout_s, None)
            last_message = out_file.read_text() if out_file.exists() else ""
        if proc.timed_out:
            raise AgentTimeout(f"codex {req.role} timed out after {req.timeout_s:.0f}s")

        thread, messages, usage, errors = req.session, [], {}, []
        for line in proc.out.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "thread.started":
                thread = event.get("thread_id") or thread
            elif kind == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                messages.append(event["item"].get("text", ""))
            elif kind == "turn.completed":
                usage = event.get("usage", {})
            elif kind in ("error", "turn.failed"):
                errors.append(str(event.get("message") or event.get("error") or event))

        text = last_message.strip() or (messages[-1] if messages else "")
        if proc.code != 0 and not text:
            raise AgentError(f"codex {req.role}: exit {proc.code}: {tail(proc.err or proc.out)}")
        ok = not errors and proc.code == 0
        return AgentResult(
            text=text, session=thread, cost_usd=None, ok=ok,
            error="" if ok else "; ".join(errors) or f"exit {proc.code}: {tail(proc.err)}",
            raw={"usage": usage, "messages": len(messages)},
        )
