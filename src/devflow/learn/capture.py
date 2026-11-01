"""Turn a finished ticket's database records into one compact, factual run record.

This step is deterministic: it reports what happened (attempts, failures, findings, human
corrections, timings, cost) and decides nothing. The retro agent reads the record to distil
lessons, and `devflow stats` aggregates the same records.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from ..states import State, Ticket
from ..store import Store

TEXT_LIMIT = 300
FINDINGS_LIMIT = 12


def _clip(text: object, n: int = TEXT_LIMIT) -> str:
    return " ".join(str(text or "").split())[:n]


def _timings(events: list[dict[str, Any]]) -> tuple[float, dict[str, float]]:
    moves = [e for e in events if e["kind"] == "transition"]
    if len(moves) < 2:
        return 0.0, {}
    per_state: dict[str, float] = {}
    for current, nxt in pairwise(moves):
        per_state[current["to"]] = per_state.get(current["to"], 0.0) + (nxt["ts"] - current["ts"])
    return moves[-1]["ts"] - moves[0]["ts"], {k: round(v, 1) for k, v in per_state.items()}


def capture(store: Store, ticket: Ticket, outcome: str, reason: str = "") -> dict[str, Any]:
    events = store.events(ticket.id)
    plan = (store.stage_results(ticket.id, "plan") or [{"payload": {}}])[-1]["payload"]
    gates = store.stage_results(ticket.id, "gates")
    reviews = store.stage_results(ticket.id, "review")
    implement = store.stage_results(ticket.id, "implement")
    total, per_state = _timings(events)

    review_rounds = []
    for r in reviews:
        p = r["payload"]
        review_rounds.append({
            "agent": p.get("agent"), "verdict": p.get("verdict"), "approved": p.get("approved"),
            "engine_gates_ok": p.get("engine_gates_ok"),
            "findings": [
                {"severity": f.get("severity"), "blocking": f.get("blocking"),
                 "file": f.get("file"), "summary": _clip(f.get("summary"))}
                for f in p.get("findings", [])[:FINDINGS_LIMIT]],
        })

    corrections = [
        {"kind": e.get("command"), "text": _clip(e.get("text"), 500)}
        for e in events
        if e["kind"] == "command" and e.get("command") in ("reject", "answer", "resume", "learn")
    ]
    return {
        "ticket": ticket.id,
        "issue": ticket.issue,
        "title": _clip(ticket.title, 120),
        "outcome": outcome,
        "escalation_reason": _clip(reason, 400),
        "plan": {
            "goal": _clip(plan.get("goal"), 400),
            "criteria": len(plan.get("acceptance_criteria", [])),
            "files": sorted({f for s in plan.get("steps", []) for f in s.get("files", [])}),
            "risks": [_clip(x, 200) for x in plan.get("risks", [])[:4]],
        },
        "plan_cycles": len(store.stage_results(ticket.id, "plan")),
        "implement_attempts": ticket.implement_attempts,
        "review_rounds": ticket.review_round,
        "implement": [{"agent": i["payload"].get("agent"), "committed": bool(i["payload"].get("committed")),
                       "switched": i["payload"].get("switched")} for i in implement],
        "gate_runs": [{"ok": g["ok"], "summary": _clip(g["payload"].get("summary"), 400),
                       "blocked_by": [f.get("kind") for f in g["payload"].get("findings", [])]}
                      for g in gates],
        "reviews": review_rounds,
        "human_corrections": corrections,
        "clarification_rounds": sum(1 for e in events if e["kind"] == "answers"),
        "triage": [{"decision": e.get("decision"), "reason": _clip(e.get("reason"), 200)}
                   for e in events if e["kind"] == "triage"],
        "errors": [{"state": e.get("state"), "error": _clip(e.get("error"), 200)}
                   for e in events if e["kind"] == "error"][:6],
        "seconds_total": round(total, 1),
        "seconds_by_state": per_state,
        "cost_usd_by_agent": store.usage_for_ticket(ticket.id),
        "final_state": ticket.state.value if isinstance(ticket.state, State) else str(ticket.state),
    }
