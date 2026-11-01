"""A scripted agent for tests and dry runs. Never calls a model."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .base import AgentError, AgentRequest, AgentResult

Reply = str | dict | list | AgentResult | Exception


class FakeAgent:
    """Replies come from a per-role queue (or a callable) and every request is recorded."""

    def __init__(
        self,
        script: dict[str, list[Reply]] | Callable[[AgentRequest], Reply] | None = None,
        name: str = "fake",
    ):
        self.name = name
        self._script = script if script is not None else {}
        self.calls: list[AgentRequest] = []

    def run(self, req: AgentRequest) -> AgentResult:
        self.calls.append(req)
        if callable(self._script):
            reply = self._script(req)
        else:
            queue = self._script.get(req.role)
            if not queue:
                raise AgentError(f"fake agent: no scripted reply left for role {req.role!r}")
            reply = queue.pop(0)
        return self._to_result(reply, req)

    @staticmethod
    def _to_result(reply: Any, req: AgentRequest) -> AgentResult:
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, AgentResult):
            return reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return AgentResult(text=text, session=req.session or f"fake-{req.role}", ok=True)

    def roles(self) -> list[str]:
        return [c.role for c in self.calls]
