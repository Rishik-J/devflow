"""Lesson management, explicit replay of a retro, and aggregate run statistics."""
from __future__ import annotations

import json
from pathlib import Path

from ..context import ledger_path
from ..detect import repo_root
from ..host import GhHost, repo_slug
from ..loadconfig import load_config
from ..store import Store, state_db_path
from .ledger import Ledger


def run(args) -> int:
    root = repo_root(Path(args.path).resolve())
    if root is None:
        raise ValueError("not inside a git repository")
    cfg = load_config(root)
    ledger = Ledger(ledger_path(root))
    action = args.learn_action
    if action == "list":
        for lesson in sorted(ledger.lessons.values(), key=lambda x: x.id):
            print(f"{lesson.id} {lesson.status:9} [{lesson.type}] {lesson.statement}")
        print(f"{len(ledger.lessons)} lesson(s)")
    elif action in ("approve", "reject"):
        lesson = ledger.approve(args.lesson_id) if action == "approve" else ledger.retire(args.lesson_id)
        ledger.save()
        print(f"{lesson.id}: {lesson.status}")
    elif action == "publish":
        from .publish import publish
        pr = publish(cfg, GhHost(repo_slug(cfg.remote)))
        print(pr.url if pr else "context already matches the default branch")
    elif action == "retro":
        from ..runtime import build_engine
        engine, store, _ = build_engine(root)
        try:
            ticket = store.get_ticket(f"#{args.issue}")
            if ticket is None or ticket.state.value not in ("merged", "escalated"):
                raise ValueError("retro requires a merged or escalated ticket")
            if "retro" not in engine.agents:
                raise ValueError("configure [agents] retro before replaying a retro")
            before = len(store.events(ticket.id, "learning_error"))
            engine._learn(ticket, ticket.state.value, model=args.model)
            errors = store.events(ticket.id, "learning_error")
            if len(errors) > before:
                raise ValueError(errors[-1]["error"])
            print(f"Retro recorded for {ticket.id}; use devflow learn list to inspect lessons")
        finally:
            store.close()
    return 0


def stats(args) -> int:
    root = repo_root(Path(args.path).resolve())
    if root is None:
        raise ValueError("not inside a git repository")
    from .capture import capture
    db = state_db_path(root)
    records = []
    if db.exists():
        with Store(db) as store:
            records = [capture(store, t, t.state.value) for t in store.list_tickets()]
    summary = {
        "tickets": len(records),
        "merged": sum(r["outcome"] == "merged" for r in records),
        "escalated": sum(r["outcome"] == "escalated" for r in records),
        "review_rounds": sum(r["review_rounds"] for r in records),
        "cost_usd_recorded": round(sum(sum(r["cost_usd_by_agent"].values()) for r in records), 4),
        "lessons": {s: len(Ledger(ledger_path(root)).by_status(s))
                    for s in ("candidate", "active", "retired")},
    }
    print(json.dumps(summary, indent=2))
    return 0
