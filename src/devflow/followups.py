"""File the follow-up work a ticket uncovered, as new tickets in Backlog.

Candidates come from two explicit places only: the planner's `follow_ups`, and the reviewer's
non-blocking findings of major or minor severity (nits are dropped). New tickets always land in
Backlog and are never marked Ready: a person decides what is worth doing. Each ticket is capped,
checked against open issues for duplicates, and filed at most once.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .board import Board
from .host import Host, Issue
from .loadconfig import BoardCfg
from .states import Ticket
from .store import Store

DISCOVERED_LABEL = "devflow:discovered"
SEVERITY_ORDER = {"major": 0, "minor": 1}
SIMILARITY = 0.6
_WORD = re.compile(r"[a-z0-9]+")
STOPWORDS = {"the", "a", "an", "of", "to", "in", "for", "and", "on", "is", "be", "add", "fix"}


@dataclass
class Candidate:
    key: str
    title: str
    why: str
    source: str
    rank: int = 5


def _digest(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode()).hexdigest()[:10]


def _stem(word: str) -> str:
    """Just enough to treat plurals as the same word ("lookups" and "lookup")."""
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def tokens(title: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(title.lower()) if w not in STOPWORDS}


def similar(a: str, b: str) -> bool:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return a.strip().lower() == b.strip().lower()
    return len(ta & tb) / len(ta | tb) >= SIMILARITY


def collect(plan: dict | None, review: dict | None) -> list[Candidate]:
    found = [
        Candidate(f"plan:{_digest(f['title'])}", f["title"].strip(), f["why"].strip(),
                  "the plan", rank=0)
        for f in (plan or {}).get("follow_ups", [])
    ]
    for f in (review or {}).get("findings", []):
        if f.get("blocking") or f.get("severity") not in SEVERITY_ORDER:
            continue
        where = f"`{f['file']}`" + (f" line {f['line']}" if f.get("line") else "")
        found.append(Candidate(
            f"review:{_digest(f['summary'])}", f["summary"].strip()[:90],
            f"{f['summary'].strip()}\n\nWhere: {where}"
            + (f"\nSuggested: {f['suggested_fix']}" if f.get("suggested_fix") else ""),
            "the review", rank=1 + SEVERITY_ORDER[f["severity"]]))
    return sorted(found, key=lambda c: c.rank)


def body_for(c: Candidate, parent: Ticket) -> str:
    marker = f"<!-- devflow:followup parent=#{parent.issue} key={c.key} -->"
    return (f"Found while working on #{parent.issue} ({parent.title}), from {c.source}.\n\n"
            f"{c.why}\n\n_Filed by devflow into Backlog. Move it to Ready to have it worked._\n"
            f"{marker}")


@dataclass
class Filed:
    issue: Issue
    candidate: Candidate


@dataclass
class FilingReport:
    filed: list[Filed]
    skipped: list[tuple[str, str]]


def file_followups(
    host: Host, board: Board | None, store: Store, cfg: BoardCfg, parent: Ticket,
    candidates: list[Candidate],
) -> FilingReport:
    done = {e["key"] for e in store.events(parent.id, "followup")}
    open_titles = [i.title for i in host.list_open_issues()]
    report = FilingReport([], [])
    ensured = False
    for c in candidates:
        if c.key in done:
            report.skipped.append((c.title, "already filed"))
        elif len(report.filed) >= cfg.followups_per_ticket:
            report.skipped.append((c.title, f"over the cap of {cfg.followups_per_ticket} per ticket"))
        elif any(similar(c.title, t) for t in open_titles):
            report.skipped.append((c.title, "an open issue already covers this"))
        else:
            if not ensured:
                host.ensure_label(cfg.label, "0e8a16", "Worked by devflow")
                host.ensure_label(DISCOVERED_LABEL, "fbca04", "Found by devflow while working another ticket")
                ensured = True
            issue = host.create_issue(c.title, body_for(c, parent), [cfg.label, DISCOVERED_LABEL])
            if board is not None:
                board.add_issue(issue.number, getattr(issue, "url", "") or _issue_url(host, issue),
                                cfg.statuses["backlog"])
            store.add_event(parent.id, "followup", {"key": c.key, "issue": issue.number})
            open_titles.append(c.title)
            report.filed.append(Filed(issue, c))
    return report


def _issue_url(host: Host, issue: Issue) -> str:
    slug = getattr(host, "slug", "")
    return f"https://github.com/{slug}/issues/{issue.number}" if slug else f"#{issue.number}"
