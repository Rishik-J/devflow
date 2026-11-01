"""Agent drivers and the factory that maps a config string to one."""

from __future__ import annotations

from .base import (
    Agent,
    AgentError,
    AgentRequest,
    AgentResult,
    AgentTimeout,
    extract_json,
    run_structured,
)
from .claude import ClaudeAgent
from .codex import CodexAgent
from .cursor import CursorAgent
from .fake import FakeAgent
from .openclaw import OpenClawAgent

__all__ = [
    "Agent", "AgentError", "AgentRequest", "AgentResult", "AgentTimeout", "ClaudeAgent",
    "CodexAgent", "CursorAgent", "FakeAgent", "OpenClawAgent", "check_ready", "extract_json",
    "make_agent", "run_structured",
]


def make_agent(spec: str) -> Agent:
    """`claude`, `codex`, `cursor`, `fake`, `openclaw` (agent "main") or `openclaw:<agent-id>`."""
    kind, _, arg = spec.partition(":")
    if kind == "claude":
        return ClaudeAgent()
    if kind == "codex":
        return CodexAgent()
    if kind == "cursor":
        return CursorAgent()
    if kind == "openclaw":
        return OpenClawAgent(arg or "main")
    if kind == "fake":
        return FakeAgent()
    raise AgentError(
        f"unknown agent {spec!r}; use claude, codex, cursor, openclaw[:id] or fake"
    )


def check_ready(agent: Agent) -> tuple[bool, str]:
    """Can this agent run right now? Drivers that can check (installed, logged in) do."""
    checker = getattr(agent, "check_ready", None)
    if checker is not None:
        return checker()
    import shutil

    binary = getattr(agent, "binary", None)
    if binary and not shutil.which(binary):
        return False, f"{binary} not found on PATH"
    return True, ""
