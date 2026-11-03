"""Tests for the command-center diagnosis, graph and resume endpoint."""

import http.server
import json
import socketserver
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from devflow import command_center
from devflow.states import State, Ticket
from devflow.store import Store, state_db_path


def make_ticket(root: Path, issue: int, path: list[tuple[State, str, str]], **fields) -> None:
    db = state_db_path(root)
    db.parent.mkdir(parents=True, exist_ok=True)
    with Store(db) as store:
        t = Ticket(id=f"#{issue}", issue=issue, title=f"Ticket {issue}", **fields)
        store.save_ticket(t)
        for to, actor, reason in path:
            store.transition(t, to, actor=actor, reason=reason)


PLAN = [(State.REFINING, "engine", "started"), (State.AWAITING_PLAN_APPROVAL, "planner", "plan ready")]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    make_ticket(tmp_path, 1, PLAN)
    make_ticket(tmp_path, 2, PLAN + [(State.TEST_DESIGN, "human:x", "plan approved"),
                                     (State.IMPLEMENTING, "planner", "tests designed"),
                                     (State.GATES, "coder", "implemented")])
    make_ticket(tmp_path, 3, PLAN + [(State.TEST_DESIGN, "human:x", "ok"), (State.IMPLEMENTING, "a", "b"),
                                     (State.GATES, "a", "b"), (State.IN_REVIEW, "engine", "gates passed"),
                                     (State.AWAITING_MERGE_APPROVAL, "reviewer", "approved"),
                                     (State.MERGED, "engine", "merged")])
    return tmp_path


def test_plan_approval_pause_is_explained(root: Path):
    d = command_center.ticket_detail(root, 1)
    g = d["diagnosis"]
    assert g["status"] == "waiting_human" and g["node"] == "openclaw"
    assert g["needs"]["command"].startswith("/approve") and g["needs"]["where"] == "the issue"
    statuses = {n["id"]: n["status"] for n in d["graph"]["nodes"]}
    assert statuses["openclaw"] == "waiting_human" and statuses["coder"] == "pending"
    assert d["timeline"][-1]["open"] and d["timeline"][-1]["to"] == "awaiting_plan_approval"


def test_gates_state_with_errors_is_blocked(root: Path):
    with Store(state_db_path(root)) as store:
        store.add_event("#2", "error", {"state": "gates", "error": "GitError: worktree missing"})
    g = command_center.ticket_detail(root, 2)["diagnosis"]
    assert g["status"] == "blocked" and "worktree missing" in g["detail"]
    nodes = {n["id"]: n["status"] for n in command_center.ticket_detail(root, 2)["graph"]["nodes"]}
    assert nodes["openclaw"] == "done" and nodes["coder"] == "done" and nodes["engine"] == "blocked"


def test_escalated_explains_reason_and_resume_command(root: Path):
    make_ticket(root, 4, PLAN + [(State.TEST_DESIGN, "h", "ok"), (State.IMPLEMENTING, "a", "b"),
                                 (State.GATES, "a", "b"), (State.ESCALATED, "engine", "review cap reached")])
    g = command_center.ticket_detail(root, 4)["diagnosis"]
    assert g["status"] == "escalated" and g["detail"] == "review cap reached"
    assert g["needs"]["command"] == "/resume"


def test_merged_ticket_is_done_and_list_orders_attention_first(root: Path):
    assert command_center.ticket_detail(root, 3)["diagnosis"]["status"] == "done"
    assert command_center.ticket_detail(root, 99) is None
    rows = command_center.list_tickets(root)
    assert [r["status"] for r in rows][-1] == "done"
    assert rows[0]["status"] == "waiting_human"


def test_stop_file_blocks_queue(root: Path):
    (root / ".devflow").mkdir(exist_ok=True)
    (root / ".devflow" / "STOP").write_text("")
    g = command_center.ticket_detail(root, 2)["diagnosis"]
    assert g["status"] == "blocked" and g["headline"] == "Queue is held"


def test_no_store_means_empty(tmp_path: Path):
    assert command_center.list_tickets(tmp_path) == []
    assert command_center.ticket_detail(tmp_path, 1) is None


def _serve(root: Path):
    from devflow.ui import UiHandler

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    class Bound(UiHandler):
        pass

    Bound.root = root
    server = Server(("127.0.0.1", 0), Bound)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", Bound.token


def test_endpoints_and_resume_security(root: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(command_center, "start_resume",
                        lambda r, i, dry_run=False: calls.append((i, dry_run)) or {"state": "running"})
    server, base, token = _serve(root)
    try:
        assert len(json.load(urllib.request.urlopen(base + "/api/tickets"))) == 3
        assert json.load(urllib.request.urlopen(base + "/api/tickets/1"))["diagnosis"]["status"] == "waiting_human"
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(base + "/api/tickets/42")
        assert missing.value.code == 404
        html = urllib.request.urlopen(base + "/").read().decode()
        assert token in html and "__DEVFLOW_TOKEN__" not in html

        def post(path, headers=None):
            return urllib.request.urlopen(urllib.request.Request(base + path, method="POST", headers=headers or {}))

        with pytest.raises(urllib.error.HTTPError) as forbidden:
            post("/api/tickets/1/resume")  # no token: a cross-site page cannot resume tickets
        assert forbidden.value.code == 403 and calls == []
        with pytest.raises(urllib.error.HTTPError) as wrong_host:
            post("/api/tickets/1/resume", {"X-Devflow-Token": token, "Host": "evil.example"})
        assert wrong_host.value.code == 403
        assert post("/api/tickets/1/resume?dry_run=1", {"X-Devflow-Token": token}).status == 202
        assert calls == [(1, True)]
    finally:
        server.shutdown()
        server.server_close()


def test_start_resume_runs_cli_once(root: Path, monkeypatch):
    spawned = []

    class Proc:
        def poll(self):
            return None

    monkeypatch.setattr(command_center.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd) or Proc())
    monkeypatch.setattr(command_center, "_runner_busy", lambda r: False)
    command_center._RUNS.clear()
    assert command_center.start_resume(root, 1, dry_run=True)["state"] == "running"
    assert spawned[0][-4:] == ["ticket", "1", str(root), "--dry-run"]
    with pytest.raises(RuntimeError):
        command_center.start_resume(root, 1)  # already running from this UI
    command_center._RUNS.clear()


def test_escalated_ticket_is_pinned_to_the_component_that_gave_up(root: Path):
    make_ticket(root, 5, PLAN + [(State.TEST_DESIGN, "h", "ok"), (State.IMPLEMENTING, "a", "b"),
                                 (State.GATES, "a", "b"), (State.ESCALATED, "engine", "gate failed twice")])
    d = command_center.ticket_detail(root, 5)
    assert d["diagnosis"]["node"] == "engine"
    assert {n["id"]: n["status"] for n in d["graph"]["nodes"]}["engine"] == "escalated"
