"""Pick the next ticket from the board.

A ticket is eligible when a person has put it in Ready, it carries the devflow label, it is
open, every issue it says it is blocked by is closed, and devflow is not already working on it.
Order is Priority (P0 before P1, then anything unranked), then oldest issue first. Tickets that
are only waiting on a person do not count against the work-in-progress limit, so one pending
approval never stalls the queue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .board import Board, BoardItem
from .host import Host, HostError
from .loadconfig import BoardCfg
from .states import TERMINAL, State
from .store import Store

DEP_RE = re.compile(r"\b(?:blocked by|depends on)\s+#(\d+)", re.IGNORECASE)
WAITING_ON_PEOPLE = {State.AWAITING_CLARIFICATION, State.AWAITING_PLAN_APPROVAL,
                     State.AWAITING_MERGE_APPROVAL, State.ESCALATED}


def dependencies(body: str) -> list[int]:
    return sorted({int(n) for n in DEP_RE.findall(body or "")})


def priority_rank(priority: str) -> int:
    m = re.fullmatch(r"\s*P?(\d+)\s*", priority or "", re.IGNORECASE)
    return int(m.group(1)) if m else 10_000


@dataclass
class Selection:
    pick: BoardItem | None = None
    ready: list[BoardItem] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    resume: list[int] = field(default_factory=list)
    working: int = 0
    reason: str = ""


def select(board: Board, host: Host, store: Store, cfg: BoardCfg) -> Selection:
    sel = Selection()
    tickets = {t.issue: t for t in store.list_tickets() if t.issue is not None}
    # Tickets devflow already owns come first: they resume, they are not re-picked.
    sel.resume = [n for n, t in tickets.items()
                  if t.state not in TERMINAL and t.state not in WAITING_ON_PEOPLE]
    sel.working = len(sel.resume)

    candidates: list[BoardItem] = []
    for item in board.items():
        if item.status != cfg.statuses["ready"]:
            continue
        if cfg.label not in item.labels:
            sel.skipped.append((item.issue, f"missing the {cfg.label!r} label"))
            continue
        known = tickets.get(item.issue)
        if known is not None:
            why = "already finished" if known.state in TERMINAL else "already in progress"
            sel.skipped.append((item.issue, f"{why} ({known.state.value})"))
            continue
        try:
            issue = host.get_issue(item.issue)
        except HostError as exc:
            sel.skipped.append((item.issue, f"cannot read the issue: {exc}"))
            continue
        if issue.state != "open":
            sel.skipped.append((item.issue, f"issue is {issue.state}"))
            continue
        open_deps = _open_dependencies(host, issue.body)
        if open_deps:
            sel.skipped.append((item.issue, "blocked by " + ", ".join(f"#{n}" for n in open_deps)))
            continue
        candidates.append(item)

    candidates.sort(key=lambda i: (priority_rank(i.priority), i.issue))
    sel.ready = candidates
    if sel.working >= cfg.max_active:
        sel.reason = f"{sel.working} ticket(s) already being worked (max_active = {cfg.max_active})"
    elif not candidates:
        sel.reason = "no eligible tickets in Ready"
    else:
        sel.pick = candidates[0]
        sel.reason = f"highest priority Ready ticket ({sel.pick.priority or 'unranked'})"
    return sel


def _open_dependencies(host: Host, body: str) -> list[int]:
    still_open = []
    for n in dependencies(body):
        try:
            if host.get_issue(n).state == "open":
                still_open.append(n)
        except HostError:
            still_open.append(n)  # cannot confirm it is done, so treat it as blocking
    return still_open
