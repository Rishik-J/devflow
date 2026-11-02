"""Distil a run record into candidate lessons with an agent, then file them in the ledger.

The agent only proposes; every proposal passes the ledger's sanitising before it is stored, and
rejected proposals are reported, not hidden.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .. import graphctx
from ..agents import Agent, AgentRequest, run_structured
from ..context import load_brief
from ..prompts import fence, schema_block
from .ledger import Ledger, Lesson, LessonRejected

RECORD_CHARS = 7000
EXISTING_LIMIT = 40


@dataclass
class RetroReport:
    added: list[Lesson] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    proposed: int = 0


def build_task(record: dict, ledger: Ledger) -> str:
    text = json.dumps(record, indent=1, default=str)
    if len(text) > RECORD_CHARS:
        text = text[:RECORD_CHARS] + "\n... (record truncated)"
    existing = [f"{x.id} [{x.status}] {x.statement}"
                for x in list(ledger.lessons.values())[:EXISTING_LIMIT] if x.status != "retired"]
    parts = ["Here is the record of one finished ticket.", fence("RUN RECORD", text)]
    if existing:
        parts += ["Existing lessons (return an exact statement again only if this ticket adds independent evidence):",
                  fence("EXISTING LESSONS", "\n".join(existing))]
    parts.append("Reply with only the JSON lessons object. An empty list is fine.")
    return "\n\n".join(parts)


def distill(
    agent: Agent, record: dict, ledger: Ledger, *, root: Path, system: str, cwd: Path,
    ticket: int, promote_after: int, timeout_s: float = 600, model: str | None = None,
) -> RetroReport:
    system += "\n\n" + load_brief(root) + "\n\n" + graphctx.agent_preamble(cwd, record.get("plan", {}).get("files", []))
    req = AgentRequest(
        role="retro", prompt=build_task(record, ledger), cwd=cwd,
        system=system + "\n\n" + schema_block("lesson", root), schema="lesson", read_only=True,
        timeout_s=timeout_s, model=model, mcp=graphctx.mcp_config(cwd),
        mcp_overrides=graphctx.codex_overrides(cwd),
    )
    result = run_structured(agent, req, root)
    report = RetroReport(proposed=len(result.data["lessons"]))
    for raw in result.data["lessons"]:
        try:
            report.added.append(ledger.add(
                type=raw["type"], statement=raw["statement"], scope=raw.get("scope"),
                ticket=ticket, evidence=raw["evidence"], source="agent",
                supersedes=raw.get("supersedes"), promote_after=promote_after))
        except LessonRejected as exc:
            report.rejected.append((str(raw.get("statement", ""))[:80], str(exc)))
    return report
