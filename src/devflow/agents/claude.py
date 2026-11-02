"""Claude Code driver: `claude -p --output-format json`, with session resume.

The prompt goes on stdin (no argument-length or quoting limits). Tool access is explicit: a
read-only run gets read tools only, so even an agent that tries to write is denied by Claude
Code itself. Only the graphify MCP server is loaded (`--strict-mcp-config`), so a repo's own
`.mcp.json` credentials are never inherited by a ticket run.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

from .. import telemetry
from .base import (
    READ_TOOLS,
    WRITE_TOOLS,
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


class ClaudeAgent:
    name = "claude"

    def __init__(self, binary: str = "claude", runner: Runner = run_process):
        self.binary = binary
        self.runner = runner

    def build(self, req: AgentRequest, tmp: Path) -> tuple[list[str], str, str]:
        """Return (command, stdin text, session id) for this request."""
        session = req.session or str(uuid.uuid4())
        cmd = [self.binary, "-p", "--output-format", "stream-json" if telemetry.detailed() else "json"]
        if telemetry.detailed():
            cmd += ["--verbose"]
        cmd += ["--resume", session] if req.session else ["--session-id", session]

        tools = req.allowed_tools or list(READ_TOOLS if req.read_only else WRITE_TOOLS)
        cmd += ["--allowedTools", ",".join(tools)]
        if req.disallowed_tools:
            cmd += ["--disallowedTools", ",".join(req.disallowed_tools)]
        if not req.read_only:
            cmd += ["--permission-mode", "acceptEdits"]
        if req.model:
            cmd += ["--model", req.model]
        if req.max_budget_usd is not None:
            cmd += ["--max-budget-usd", str(req.max_budget_usd)]
        if req.system:
            system_file = tmp / "system.md"
            system_file.write_text(req.system)
            cmd += ["--append-system-prompt-file", str(system_file)]
        if req.mcp:
            mcp_file = tmp / "mcp.json"
            mcp_file.write_text(json.dumps(req.mcp))
            cmd += ["--strict-mcp-config", "--mcp-config", str(mcp_file)]
        return cmd, req.prompt, session

    @telemetry.agent_call
    def run(self, req: AgentRequest) -> AgentResult:
        with tempfile.TemporaryDirectory(prefix="devflow-claude-") as tmp:
            cmd, stdin, session = self.build(req, Path(tmp))
            proc = self.runner(cmd, req.cwd, agent_env(("ANTHROPIC_", "CLAUDE_")),
                               req.timeout_s, stdin)
        events = telemetry.cli_events(proc.out, "claude")
        telemetry.current_content(output={"stderr": proc.err, "exit_code": proc.code,
                                          "timed_out": proc.timed_out})
        if proc.timed_out:
            raise AgentTimeout(f"claude {req.role} timed out after {req.timeout_s:.0f}s")
        data = next((e for e in reversed(events) if e.get("type") == "result"), None) or extract_json(proc.out)
        if not isinstance(data, dict) or "result" not in data:
            raise AgentError(
                f"claude {req.role}: unreadable output (exit {proc.code}): "
                f"{tail(proc.err or proc.out)}"
            )
        ok = not data.get("is_error") and data.get("subtype", "success") == "success"
        return AgentResult(
            text=str(data.get("result", "")),
            session=data.get("session_id") or session,
            cost_usd=data.get("total_cost_usd"),
            duration_s=(data.get("duration_ms") or 0) / 1000,
            ok=ok,
            error="" if ok else f"{data.get('subtype', 'error')}: {tail(str(data.get('result', '')))}",
            raw=data,
        )
