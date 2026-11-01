"""SQLite run state: tickets, transitions, stage results and events.

The database lives in `.devflow/state/` (git-excluded). A transition and the ticket row it
changes are written in one transaction, so a crash never leaves them disagreeing and a run can
resume from the stored state.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

from .states import Limits, State, Ticket, advance

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    issue INTEGER,
    title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    review_round INTEGER NOT NULL DEFAULT 0,
    implement_attempts INTEGER NOT NULL DEFAULT 0,
    branch TEXT NOT NULL DEFAULT '',
    worktree TEXT NOT NULL DEFAULT '',
    base_sha TEXT NOT NULL DEFAULT '',
    sessions TEXT NOT NULL DEFAULT '{}',
    history TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    payload TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stage_ticket ON stage_results(ticket_id, stage);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_id);
"""


def state_db_path(root: Path) -> Path:
    return root / ".devflow" / "state" / "devflow.db"


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    @staticmethod
    def _write_ticket(db: sqlite3.Connection, t: Ticket) -> None:
        db.execute(
            """INSERT INTO tickets (id, issue, title, state, review_round, implement_attempts,
                   branch, worktree, base_sha, sessions, history, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   issue=excluded.issue, title=excluded.title, state=excluded.state,
                   review_round=excluded.review_round,
                   implement_attempts=excluded.implement_attempts, branch=excluded.branch,
                   worktree=excluded.worktree, base_sha=excluded.base_sha,
                   sessions=excluded.sessions, history=excluded.history,
                   updated_at=excluded.updated_at""",
            (t.id, t.issue, t.title, t.state.value, t.review_round, t.implement_attempts,
             t.branch, t.worktree, t.base_sha, json.dumps(t.sessions), json.dumps(t.history),
             time.time()),
        )

    def save_ticket(self, ticket: Ticket) -> None:
        with self._tx() as db:
            self._write_ticket(db, ticket)

    def get_ticket(self, ticket_id: str) -> Ticket | None:
        row = self._db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        return self._row_to_ticket(row) if row else None

    def list_tickets(self, *states: State) -> list[Ticket]:
        sql, args = "SELECT * FROM tickets", ()
        if states:
            sql += f" WHERE state IN ({','.join('?' * len(states))})"
            args = tuple(s.value for s in states)
        return [self._row_to_ticket(r) for r in self._db.execute(sql + " ORDER BY updated_at", args)]

    @staticmethod
    def _row_to_ticket(row: sqlite3.Row) -> Ticket:
        return Ticket(
            id=row["id"], state=State(row["state"]), review_round=row["review_round"],
            implement_attempts=row["implement_attempts"], history=json.loads(row["history"]),
            issue=row["issue"], title=row["title"], branch=row["branch"],
            worktree=row["worktree"], base_sha=row["base_sha"],
            sessions=json.loads(row["sessions"]),
        )

    def transition(
        self, ticket: Ticket, to: State, *, actor: str, reason: str = "",
        limits: Limits | None = None,
    ) -> Ticket:
        """Validate, apply and persist a transition atomically.

        If the move is illegal or a cap is hit, nothing is written and the in-memory ticket is
        unchanged. If the write fails, the in-memory ticket is rolled back to match the database.
        """
        before = (ticket.state, ticket.review_round, ticket.implement_attempts,
                  list(ticket.history))
        advance(ticket, to, actor=actor, reason=reason, limits=limits)
        try:
            with self._tx() as db:
                self._write_ticket(db, ticket)
                db.execute(
                    "INSERT INTO events (ticket_id, kind, payload, ts) VALUES (?,?,?,?)",
                    (ticket.id, "transition",
                     json.dumps({"to": to.value, "actor": actor, "reason": reason}), time.time()),
                )
        except BaseException:
            ticket.state, ticket.review_round, ticket.implement_attempts = before[:3]
            ticket.history = before[3]
            raise
        return ticket

    def add_event(self, ticket_id: str, kind: str, payload: dict[str, Any]) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO events (ticket_id, kind, payload, ts) VALUES (?,?,?,?)",
                       (ticket_id, kind, json.dumps(payload), time.time()))

    def events(self, ticket_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM events WHERE ticket_id=?", [ticket_id]
        if kind:
            sql, args = sql + " AND kind=?", [*args, kind]
        rows = self._db.execute(sql + " ORDER BY id", args)
        return [{"kind": r["kind"], "ts": r["ts"], **json.loads(r["payload"])} for r in rows]

    def add_stage_result(
        self, ticket_id: str, stage: str, attempt: int, ok: bool, payload: dict[str, Any]
    ) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO stage_results (ticket_id, stage, attempt, ok, payload, ts)"
                " VALUES (?,?,?,?,?,?)",
                (ticket_id, stage, attempt, int(ok), json.dumps(payload), time.time()),
            )

    def stage_results(self, ticket_id: str, stage: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM stage_results WHERE ticket_id=?", [ticket_id]
        if stage:
            sql, args = sql + " AND stage=?", [*args, stage]
        rows = self._db.execute(sql + " ORDER BY id", args)
        return [{"stage": r["stage"], "attempt": r["attempt"], "ok": bool(r["ok"]),
                 "payload": json.loads(r["payload"]), "ts": r["ts"]} for r in rows]
