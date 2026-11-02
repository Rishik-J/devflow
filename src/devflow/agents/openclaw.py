"""OpenClaw driver: `openclaw agent --json`, for the judgment and conversation roles.

OpenClaw is the orchestrator's brain and the human interface: planner, triage, retro, and
notifications. Its agents run in their own workspace, not in the ticket worktree, so the prompt
names the checkout and the agent reads it through its own tools and graphify. A stable session
key per ticket and role is the session: calling again with the same key resumes it.

Its model calls can stall for minutes, so calls are retried with a pause, and every run uses a
dedicated session key so it can never collide with the interactive iMessage session.
"""

from __future__ import annotations

import tempfile
import time
import uuid
from collections.abc import Callable
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


class OpenClawAgent:
    def __init__(
        self,
        agent_id: str = "main",
        binary: str = "openclaw",
        runner: Runner = run_process,
        retries: int = 1,
        retry_delay_s: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.agent_id = agent_id
        self.name = f"openclaw:{agent_id}"
        self.binary = binary
        self.runner = runner
        self.retries = retries
        self.retry_delay_s = retry_delay_s
        self.sleep = sleep

    def session_key(self, req: AgentRequest) -> str:
        return req.session or f"agent:{self.agent_id}:devflow-{uuid.uuid4().hex[:8]}-{req.role}"

    def message(self, req: AgentRequest) -> str:
        parts = []
        if req.system:
            parts.append(req.system.strip())
        parts.append(f"Repository checkout for this task: {req.cwd}")
        parts.append(req.prompt.strip())
        return "\n\n".join(parts)

    def build(self, req: AgentRequest, message_file: Path, key: str) -> list[str]:
        timeout = int(req.timeout_s)
        cmd = [self.binary, "agent", "--agent", self.agent_id, "--session-key", key,
                "--message-file", str(message_file), "--json", "--timeout", str(timeout)]
        if req.model:
            cmd += ["--model", req.model]
        return cmd

    @telemetry.agent_call
    def run(self, req: AgentRequest) -> AgentResult:
        key = self.session_key(req)
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                self.sleep(self.retry_delay_s)
            try:
                with telemetry.span("provider.attempt", {"devflow.retry_index": attempt}):
                    return self._once(req, key)
            except AgentError as exc:
                last = exc
        assert last is not None
        raise last

    def _once(self, req: AgentRequest, key: str) -> AgentResult:
        with tempfile.TemporaryDirectory(prefix="devflow-openclaw-") as tmp:
            message_file = Path(tmp) / "message.md"
            message_file.write_text(self.message(req))
            proc = self.runner(self.build(req, message_file, key), req.cwd,
                               agent_env(("OPENCLAW_",)), req.timeout_s + 30, None)
        telemetry.current_content(input={"message": self.message(req), "session": key},
                                  output={"stderr": proc.err, "exit_code": proc.code,
                                          "timed_out": proc.timed_out})
        if proc.timed_out:
            raise AgentTimeout(f"{self.name} {req.role} timed out after {req.timeout_s:.0f}s")
        data = extract_json(proc.out)
        if not isinstance(data, dict):
            raise AgentError(
                f"{self.name} {req.role}: unreadable output (exit {proc.code}): "
                f"{tail(proc.err or proc.out)}"
            )
        result = data.get("result") or {}
        text = "\n".join(
            p["text"] for p in result.get("payloads", []) if isinstance(p, dict) and p.get("text")
        ).strip()
        ok = data.get("status") == "ok" and proc.code == 0
        meta = result.get("meta", {})
        # Public metadata only. Never walk OpenClaw's other agents' private session files.
        agent_meta = meta.get("agentMeta") or {}
        usage = agent_meta.get("usage") or {}
        usage = usage if isinstance(usage, dict) else {}
        normalized = {f"{kind}_tokens": usage.get(f"{kind}_tokens", usage.get(kind, usage.get(f"{kind}Tokens")))
                      for kind in ("input", "output")}
        telemetry.current_content(output={"response": text, "status": data.get("status"),
                                          "summary": data.get("summary"), "run_id": data.get("runId"),
                                          "reported_usage": usage, "reported_model": agent_meta.get("model"),
                                          "provider_session": agent_meta.get("sessionId")})
        return AgentResult(
            text=text, session=key, duration_s=(meta.get("durationMs") or 0) / 1000, ok=ok,
            error="" if ok else f"status={data.get('status')}: {data.get('summary') or tail(proc.err)}",
            raw={"runId": data.get("runId"), "status": data.get("status"),
                 "usage": normalized, "model": agent_meta.get("model")},
        )
