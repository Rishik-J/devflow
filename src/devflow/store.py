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
CREATE TABLE IF NOT EXISTS agent_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    role TEXT NOT NULL,
    ticket_id TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    cost_usd REAL NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS agent_cooldown (
    agent TEXT PRIMARY KEY,
    until REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    strikes INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS baselines (
    key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_agent_ts ON agent_usage(agent, ts);
CREATE INDEX IF NOT EXISTS idx_stage_ticket ON stage_results(ticket_id, stage);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_id);
"""


def state_db_path(root: Path) -> Path:
    return root / ".devflow" / "state" / "devflow.db"


def dry_run_db_path(root: Path) -> Path:
    """A dry run keeps its own throwaway database, so it can never be mistaken for real work."""
    return root / ".devflow" / "state" / "dry-run.db"


def reset_dry_run_db(root: Path) -> Path:
    path = dry_run_db_path(root)
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return path


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
        # fixed keys last, so a payload field can never overwrite the event's own kind/ts
        return [{**json.loads(r["payload"]), "kind": r["kind"], "ts": r["ts"]} for r in rows]

    def add_stage_result(
        self, ticket_id: str, stage: str, attempt: int, ok: bool, payload: dict[str, Any]
    ) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO stage_results (ticket_id, stage, attempt, ok, payload, ts)"
                " VALUES (?,?,?,?,?,?)",
                (ticket_id, stage, attempt, int(ok), json.dumps(payload), time.time()),
            )

    def record_usage(
        self, agent: str, role: str, ticket_id: str, *, cost_usd: float, tokens_in: int = 0,
        tokens_out: int = 0, ok: bool = True, error: str = "", ts: float | None = None,
    ) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO agent_usage (agent, role, ticket_id, ts, cost_usd, tokens_in,"
                " tokens_out, ok, error) VALUES (?,?,?,?,?,?,?,?,?)",
                (agent, role, ticket_id, ts if ts is not None else time.time(), cost_usd,
                 tokens_in, tokens_out, int(ok), error[:500]),
            )

    def usage_since(self, agent: str, since: float) -> dict[str, float]:
        row = self._db.execute(
            "SELECT COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(tokens_in),0) ti,"
            " COALESCE(SUM(tokens_out),0) toks, COUNT(*) n FROM agent_usage"
            " WHERE agent=? AND ts>=?", (agent, since),
        ).fetchone()
        return {"cost_usd": row["c"], "tokens_in": row["ti"], "tokens_out": row["toks"],
                "runs": row["n"]}

    def usage_for_ticket(self, ticket_id: str) -> dict[str, float]:
        rows = self._db.execute(
            "SELECT agent, COALESCE(SUM(cost_usd),0) c FROM agent_usage WHERE ticket_id=?"
            " GROUP BY agent", (ticket_id,))
        return {r["agent"]: round(r["c"], 4) for r in rows}

    def totals_for_ticket(self, ticket_id: str) -> dict[str, float]:
        """Recorded spend for one ticket across all agents: cost, tokens and runs."""
        row = self._db.execute(
            "SELECT COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(tokens_in),0) i,"
            " COALESCE(SUM(tokens_out),0) o, COUNT(*) n FROM agent_usage WHERE ticket_id=?",
            (ticket_id,)).fetchone()
        return {"cost_usd": round(row["c"], 4), "tokens": int(row["i"] + row["o"]), "runs": row["n"]}

    def get_baseline(self, key: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT payload FROM baselines WHERE key=?", (key,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def set_baseline(self, key: str, payload: dict[str, Any]) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO baselines (key, payload, ts) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, ts=excluded.ts",
                (key, json.dumps(payload), time.time()),
            )

    def set_cooldown(self, agent: str, until: float, reason: str, strikes: int) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO agent_cooldown (agent, until, reason, strikes) VALUES (?,?,?,?)"
                " ON CONFLICT(agent) DO UPDATE SET until=excluded.until,"
                " reason=excluded.reason, strikes=excluded.strikes",
                (agent, until, reason[:300], strikes),
            )

    def get_cooldown(self, agent: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM agent_cooldown WHERE agent=?", (agent,)).fetchone()
        return dict(row) if row else None

    def clear_cooldown(self, agent: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM agent_cooldown WHERE agent=?", (agent,))

    def latest_transition_ts(self, ticket_id: str, to: State | None = None) -> float | None:
        """Timestamp of the newest transition event (optionally into `to`); read-only."""
        rows = self._db.execute(
            "SELECT payload, ts FROM events WHERE ticket_id=? AND kind='transition' ORDER BY id DESC",
            (ticket_id,))
        for r in rows:
            if to is None:
                return r["ts"]
            try:
                if json.loads(r["payload"]).get("to") == to.value:
                    return r["ts"]
            except (ValueError, AttributeError):
                continue
        return None

    def ticket_updated_at(self, ticket_id: str) -> float | None:
        row = self._db.execute("SELECT updated_at FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        return row["updated_at"] if row else None

    def latest_usage(self, ticket_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT agent, role, ts FROM agent_usage WHERE ticket_id=? ORDER BY id DESC LIMIT 1",
            (ticket_id,)).fetchone()
        return dict(row) if row else None

    def stage_results(self, ticket_id: str, stage: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM stage_results WHERE ticket_id=?", [ticket_id]
        if stage:
            sql, args = sql + " AND stage=?", [*args, stage]
        rows = self._db.execute(sql + " ORDER BY id", args)
        return [{"stage": r["stage"], "attempt": r["attempt"], "ok": bool(r["ok"]),
                 "payload": json.loads(r["payload"]), "ts": r["ts"]} for r in rows]
