"""Tests for devflow ui."""

import http.server
import json
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from devflow.states import State, Ticket
from devflow.store import Store, state_db_path
from devflow.ui import (
    HTML_TEMPLATE,
    LIFECYCLE,
    RUN_OUTCOME_LABELS,
    UiHandler,
    _classify_outcome,
    collect_runs,
    collect_state,
    collect_ticket,
)


def test_html_template_has_vital_elements():
    assert "Devflow Dashboard" in HTML_TEMPLATE
    assert "/api/state" in HTML_TEMPLATE
    assert "board-container" in HTML_TEMPLATE


def test_collect_state(tmp_path: Path):
    res = collect_state(tmp_path)
    assert "columns" in res
    assert "repo" in res
    assert "Backlog" in res["columns"]
    assert "Ready" in res["columns"]


def test_ui_server_and_e2e_browser():
    """End-to-end test: boots local server on ephemeral port and tests endpoints & browser rendering."""
    repo = Path(__file__).resolve().parent.parent

    class CustomServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    class BoundHandler(UiHandler):
        root = repo

    server = CustomServer(("127.0.0.1", 0), BoundHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    time.sleep(0.3)

    try:
        # 1. Test HTML endpoint
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
            assert resp.status == 200
            assert "text/html" in resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8")
            assert "Devflow Dashboard" in body

        # 2. Test JSON API state endpoint
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as resp:
            assert resp.status == 200
            assert "application/json" in resp.headers.get("Content-Type", "")
            data = json.loads(resp.read().decode("utf-8"))
            assert "columns" in data
            assert "repo" in data

        # 3. Test 404 for unknown endpoints
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/invalid_endpoint")
        except urllib.error.HTTPError as e:
            assert e.code == 404

        # 4. End-to-end browser execution (if Chrome/Chromium is installed on host)
        chrome_bin = None
        for cand in [
            shutil.which("google-chrome"),
            shutil.which("chromium"),
            shutil.which("chrome"),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]:
            if cand and Path(cand).exists():
                chrome_bin = str(cand)
                break

        if chrome_bin:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                shot_path = tmp.name
            try:
                cmd = [
                    chrome_bin,
                    "--headless",
                    "--disable-gpu",
                    "--virtual-time-budget=2000",
                    f"--screenshot={shot_path}",
                    f"http://127.0.0.1:{port}/",
                ]
                res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=15)
                assert res.returncode == 0
                shot_file = Path(shot_path)
                assert shot_file.exists()
                assert shot_file.stat().st_size > 1000
            finally:
                Path(shot_path).unlink(missing_ok=True)
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- ticket inspector

IMPACT = {
    "files": ["src/a.py"], "unknown_files": [], "depth": 2, "graph_state": "fresh",
    "symbols": [{"id": "s1", "label": "foo()", "file": "src/a.py", "line": 3, "role": "defined"}],
    "callers": {"1": [{"id": "c1", "symbol": "bar", "file": "src/b.py", "hop": 1, "relation": "calls"},
                      {"id": "c2", "symbol": "t", "file": "tests/test_a.py", "hop": 1, "relation": "calls"}],
                "2": [{"id": "c3", "symbol": "deep", "file": "src/c.py", "hop": 2, "relation": "calls"}]},
    "dependent_modules": [{"file": "src/m.py", "hop": 1}], "external_contracts": [],
    "test_coverage": {}, "uncovered": ["baz()"], "impacted_tests": ["tests/test_a.py"],
}


def seed(tmp_path: Path, issue: int = 38, state: State = State.GATES) -> Store:
    store = Store(state_db_path(tmp_path))
    t = Ticket(id=f"#{issue}", state=state, issue=issue, title="Add <b>thing</b>",
               branch="devflow/38-x", worktree="/tmp/wt", implement_attempts=2, review_round=1,
               sessions={"pr": "77", "coder_agent": "claude"})
    store.save_ticket(t)
    store.add_event(t.id, "transition", {"to": "gates", "actor": "engine", "reason": "committed"})
    return store


def add(store: Store, kind: str, payload: dict, ok: bool = True, issue: int = 38) -> None:
    n = len(store.stage_results(f"#{issue}", kind)) + 1
    store.add_stage_result(f"#{issue}", kind, n, ok, payload)


def no_host(slug):
    raise RuntimeError("offline")


class FakeIssueHost:
    def __init__(self, slug):
        self.slug = slug

    def get_issue(self, number):
        class I:
            body = f"Body **bold** for {number}"
        return I()


def full_store(tmp_path: Path) -> None:
    store = seed(tmp_path)
    add(store, "plan", {"goal": "old", "acceptance_criteria": [], "steps": []})
    add(store, "plan", {"goal": "G", "acceptance_criteria": [{"id": "AC1", "text": "x"}],
                        "steps": [{"id": "S1", "description": "d", "files": ["a.py"]}],
                        "out_of_scope": ["o"], "risks": ["r"], "reasoning": "because", "reply": "SECRET"})
    add(store, "test_plan", {"tests": [{"id": "T1", "kind": "unit", "description": "t"}]})
    add(store, "implement", {"agent": "codex", "reply": "SECRET"})
    add(store, "gates", {"ok": False, "summary": "s1", "unconfigured": [], "findings": [],
                         "results": [{"project": ".", "gate": "unit", "command": "pytest", "ok": False,
                                      "exit_code": 1, "duration_s": 1.5, "timed_out": False,
                                      "output_tail": "boom"}],
                         "failures": [{"project": ".", "gate": "unit", "command": "pytest", "new": ["t::a"]}]},
        ok=False)
    add(store, "gates", {"ok": True, "summary": "s2", "unconfigured": [], "findings": [],
                         "results": [], "failures": [], "impact": IMPACT, "impacted_tests": ["tests/test_a.py"]})
    add(store, "review", {"agent": "codex", "independent": True, "verdict": "approve", "approved": True,
                          "findings": [{"id": "F", "severity": "minor", "blocking": False, "summary": "nit",
                                        "file": "a.py"}],
                          "notes": "ok", "facts": "f", "reply": "SECRET"})
    store.close()


KEYS = {"issue", "title", "state", "lifecycle", "branch", "worktree", "attempts", "review_round",
        "agent", "urls", "history", "brief", "plan", "blast_radius", "gates", "review", "trace"}


def test_collect_ticket_full_shape(tmp_path: Path):
    full_store(tmp_path)
    d = collect_ticket(tmp_path, 38, host_factory=FakeIssueHost)
    assert KEYS <= set(d)
    assert d["lifecycle"] == "Gates" and d["agent"] == "codex" and d["attempts"] == 2
    assert d["urls"]["pr"].endswith("/pull/77") and d["urls"]["issue"].endswith("/issues/38")
    assert d["history"][0]["to"] == "gates"
    assert d["brief"] == {"body": "Body **bold** for 38", "available": True}
    assert "reply" not in json.dumps(d) and "SECRET" not in json.dumps(d)


def test_plan_section_uses_latest_plan_and_test_plan(tmp_path: Path):
    full_store(tmp_path)
    plan = collect_ticket(tmp_path, 38, host_factory=no_host)["plan"]
    assert plan["goal"] == "G" and plan["steps"][0]["files"] == ["a.py"]
    assert plan["out_of_scope"] == ["o"] and plan["risks"] == ["r"]
    assert plan["test_strategy"][0]["id"] == "T1" and plan["reasoning"] == "because"


def test_plan_reasoning_absent_and_empty_plan(tmp_path: Path):
    store = seed(tmp_path)
    add(store, "plan", {"goal": "G"})
    store.close()
    assert collect_ticket(tmp_path, 38, host_factory=no_host)["plan"]["reasoning"] is None
    empty = tmp_path / "e"
    empty.mkdir()
    seed(empty).close()
    assert collect_ticket(empty, 38, host_factory=no_host)["plan"] == {}


def test_gates_attempts_review_and_blast_radius(tmp_path: Path):
    full_store(tmp_path)
    d = collect_ticket(tmp_path, 38, host_factory=no_host)
    assert [a["ok"] for a in d["gates"]["attempts"]] == [False, True]
    assert d["gates"]["attempts"][0]["failures"][0]["new"] == ["t::a"]
    assert d["review"]["verdict"] == "approve" and d["review"]["findings"][0]["blocking"] is False
    assert d["gates"]["review"] == d["review"]
    b = d["blast_radius"]
    assert b["status"] == "available" and b["symbols"][0]["label"] == "foo()"
    assert {c["symbol"]: c["verified"] for c in b["callers"]} == {"bar": False, "t": True}
    assert b["dependent_modules"] and b["uncovered"] == ["baz()"]
    assert b["impacted_tests"] == ["tests/test_a.py"]


def test_legacy_gates_payload_and_partial_impact(tmp_path: Path):
    store = seed(tmp_path)
    add(store, "gates", {"ok": True, "summary": "s", "unconfigured": [], "findings": []})
    store.close()
    d = collect_ticket(tmp_path, 38, host_factory=no_host)
    assert d["gates"]["latest"]["results"] == [] and d["gates"]["latest"]["failures"] == []
    assert d["blast_radius"]["status"] == "unavailable"
    other = tmp_path / "p"
    other.mkdir()
    st = seed(other)
    add(st, "gates", {"ok": True, "impact": {"callers": {"1": [{}]}, "symbols": "bad"}})
    st.close()
    assert collect_ticket(other, 38, host_factory=no_host)["blast_radius"]["status"] == "available"


@pytest.mark.parametrize("state,label", [(s, LIFECYCLE[s]) for s in State])
def test_lifecycle_mapping(tmp_path: Path, state, label):
    seed(tmp_path, state=state).close()
    assert collect_ticket(tmp_path, 38, host_factory=no_host)["lifecycle"] == label
    assert label in {"Refining", "Implementing", "Gates", "Review", "Merged", "Failed"}


def test_empty_sections_are_present(tmp_path: Path):
    seed(tmp_path).close()
    d = collect_ticket(tmp_path, 38, host_factory=no_host)
    for k in ("plan", "blast_radius", "gates", "review"):
        assert d[k] is not None
    assert d["trace"] == [] and d["brief"] == {"body": None, "available": False}


def test_local_only_ticket_without_issue_does_not_crash(tmp_path: Path):
    store = Store(state_db_path(tmp_path))
    store.save_ticket(Ticket(id="local-1", state=State.NEW, title="local"))
    store.close()
    assert collect_state(tmp_path)["columns"]
    assert collect_ticket(tmp_path, 1, host_factory=no_host) is None


def test_trace_filters_issue_synthetic_and_malformed(tmp_path: Path):
    seed(tmp_path).close()
    log = tmp_path / ".devflow" / "state" / "traces.jsonl"
    rows = [
        {"name": "stage.gates", "start_ns": 0, "end_ns": 2_000_000_000,
         "attributes": {"devflow.issue": 38, "devflow.result": "ok", "prompt": "TOPSECRET"}},
        {"name": "other", "attributes": {"devflow.issue": 5}},
        {"name": "synthetic", "attributes": {"devflow.issue": 38, "langfuse.trace.metadata.synthetic": "true"}},
    ]
    log.write_text("\n".join([json.dumps(rows[0]), "{bad json", "", "[1]", json.dumps(rows[1]),
                              json.dumps(rows[2])]) + "\n")
    trace = collect_ticket(tmp_path, 38, host_factory=no_host)["trace"]
    assert trace == [{"name": "stage.gates", "duration_s": 2.0, "outcome": "ok"}]
    assert "TOPSECRET" not in json.dumps(trace)
    log.unlink()
    assert collect_ticket(tmp_path, 38, host_factory=no_host)["trace"] == []


def test_host_failure_and_hang_fall_back(tmp_path: Path, monkeypatch):
    seed(tmp_path).close()
    assert collect_ticket(tmp_path, 38, host_factory=no_host)["brief"]["available"] is False
    from devflow import ui
    monkeypatch.setattr(ui, "HOST_TIMEOUT_S", 0.2)

    class Hang:
        def __init__(self, slug):
            pass

        def get_issue(self, n):
            time.sleep(2)

    start = time.time()
    assert collect_ticket(tmp_path, 38, host_factory=Hang)["brief"] == {"body": None, "available": False}
    assert time.time() - start < 1.5


def serve(root: Path, factory=no_host):
    class CustomServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    class H(UiHandler):
        pass

    H.root = root
    H.host_factory = staticmethod(factory)
    server = CustomServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def get(port: int, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
            return r.status, r.headers, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, e.headers, json.loads(body) if body else None


def test_ticket_endpoint_live(tmp_path: Path):
    full_store(tmp_path)
    server, port = serve(tmp_path, FakeIssueHost)
    try:
        code, headers, data = get(port, "/api/ticket/38?x=1")
        assert code == 200 and KEYS <= set(data)
        assert "application/json" in headers["Content-Type"] and headers["Cache-Control"] == "no-cache"
        assert data["brief"]["available"] is True
        code, _, body = get(port, "/api/ticket/9999")
        assert code == 404 and "error" in body
        for bad in ("abc", "0", "-3", "1.5"):
            code, _, body = get(port, f"/api/ticket/{bad}")
            assert code == 400 and "error" in body
        assert get(port, "/api/ticket/")[0] == 404
        assert get(port, "/api/state")[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_ticket_endpoint_without_db_does_not_create_state(tmp_path: Path):
    server, port = serve(tmp_path)
    try:
        assert get(port, "/api/ticket/3")[0] == 404
        assert not (tmp_path / ".devflow").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_state_cards_gain_additive_fields(tmp_path: Path):
    seed(tmp_path).close()
    card = collect_state(tmp_path)["columns"]["In progress"][0]
    assert card["issue"] == 38 and card["stage"] == "gates" and card["lifecycle"] == "Gates"
    assert card["review_round"] == 1 and card["implement_attempts"] == 2


def test_template_markers():
    t = HTML_TEMPLATE
    for marker in ("640px", "max-width: 640px", "100vw", "transform 0.25s", "backdrop-filter: blur(4px)",
                   "#101512", "#161c18", "#222a24", "#2b352e", "#34d399",
                   "Overview", "Plan & Decisions", "Blast radius", "Verification & Gates",
                   'role="dialog"', 'aria-modal="true"', 'role="tablist"', "role", "tabIndex = 0",
                   "'Enter'", "'Escape'", "#ticket-", "hashchange", "location.hash", "/api/ticket/",
                   "setInterval(fetchState, 3000)", "scrollTop", "activeTab", "safeUrl", "drawer.focus()",
                   "target.focus()", "escapeHtml(item.stage)", "escapeHtml(item.priority)"):
        assert marker in t, marker
    assert t.index('id="drawer-overlay"') > t.index('id="board-container"')
    assert 'id="board-container"' in t and "board-container\').innerHTML" not in t.split("drawer-overlay")[1]
    for external in ("src=\"http", "rel=\"stylesheet\"", "@import"):
        assert external not in t


def test_markdown_is_inert():
    # Static guarantees: escape first, http(s)-only links.
    body = HTML_TEMPLATE[HTML_TEMPLATE.index("function renderMarkdown"):HTML_TEMPLATE.index("function chip(")]
    assert "escapeHtml(src" in body.split("for (const line")[0]
    assert "https?:" in HTML_TEMPLATE[HTML_TEMPLATE.index("function safeUrl"):HTML_TEMPLATE.index("function inlineMd")]
    node = shutil.which("node")
    if node:  # dynamic check only where Node is available
        start = HTML_TEMPLATE.index("function safeUrl")
        end = HTML_TEMPLATE.index("function chip(")
        esc_start = HTML_TEMPLATE.index("function escapeHtml")
        esc = HTML_TEMPLATE[esc_start:HTML_TEMPLATE.index("}\n", esc_start) + 2]
        script = esc + HTML_TEMPLATE[start:end] + (
            "console.log(renderMarkdown('[x](javascript:alert(1)) <img src=x onerror=alert(1)> "
            "[ok](https://e.com/a?b=1&c=2)'));")
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout
        assert "<img" not in out and 'href="javascript' not in out
        assert 'href="https://e.com/a?b=1&amp;c=2"' in out


def test_refresh_without_brief_does_not_call_host(tmp_path: Path):
    seed(tmp_path).close()
    calls = []

    class Counting(FakeIssueHost):
        def get_issue(self, number):
            calls.append(number)
            return super().get_issue(number)

    server, port = serve(tmp_path, Counting)
    try:
        assert get(port, "/api/ticket/38")[2]["brief"]["available"] is True
        assert len(calls) == 1
        for _ in range(2):
            code, _, data = get(port, "/api/ticket/38?brief=0")
            assert code == 200 and data["brief"]["skipped"] is True
        assert len(calls) == 1
        assert "?brief=0" in HTML_TEMPLATE
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- runs ledger

RUN_KEYS = {"issue", "title", "state", "outcome", "outcome_label", "pr_url", "pr_number", "started_at",
            "completed_at", "duration_s", "implement_attempts", "max_implement_attempts",
            "review_rounds", "agent", "blast"}


def finished(store: Store, issue: int, state: State, path: list[str], t0: float | None = 1000.0,
             t1: float | None = 1100.0, **kw) -> Ticket:
    """A terminal ticket whose transition events are `path` (last one terminal), t0..t1 apart."""
    t = Ticket(id=f"#{issue}", state=state, issue=issue, title=kw.pop("title", f"Ticket {issue}"), **kw)
    store.save_ticket(t)
    for i, to in enumerate(path):
        if t0 is None:
            break
        ts = t0 + (t1 - t0) * i / max(len(path) - 1, 1)
        store._db.execute("INSERT INTO events (ticket_id, kind, payload, ts) VALUES (?,?,?,?)",
                          (t.id, "transition", json.dumps({"to": to, "actor": "engine", "reason": ""}), ts))
    store._db.commit()
    return t


def test_classify_outcome_every_branch():
    def tk(state):
        return Ticket(id="#1", state=state)

    def tr(*to):
        return [{"to": x} for x in to]

    assert _classify_outcome(tk(State.MERGED), tr("gates", "merged")) == "merged"
    for stage in ("in_review", "changes_requested", "awaiting_merge_approval"):
        assert _classify_outcome(tk(State.ESCALATED), tr(stage, "escalated")) == "rejected_review"
    for stage in ("gates", "implementing", "test_design"):
        assert _classify_outcome(tk(State.ESCALATED), tr(stage, "escalated")) == "failed_gates"
    assert _classify_outcome(tk(State.ESCALATED), tr("refining", "escalated")) == "stalled"
    assert _classify_outcome(tk(State.ESCALATED), []) == "stalled"
    assert _classify_outcome(tk(State.CANCELLED), tr("gates", "cancelled")) == "cancelled"
    # last non-terminal stage wins; repeated calls are identical
    path = tr("gates", "in_review", "escalated")
    assert _classify_outcome(tk(State.ESCALATED), path) == _classify_outcome(tk(State.ESCALATED), path) \
        == "rejected_review"
    assert set(RUN_OUTCOME_LABELS.values()) == {"Merged", "Rejected (Review)", "Failed (Gates)",
                                                "Stalled", "Cancelled"}


def test_collect_runs_shape_order_and_filtering(tmp_path: Path):
    store = Store(state_db_path(tmp_path))
    finished(store, 1, State.MERGED, ["gates", "merged"], 100, 200)
    finished(store, 2, State.ESCALATED, ["in_review", "escalated"], 100, 400)
    finished(store, 3, State.ESCALATED, ["gates", "escalated"], 100, 300)
    finished(store, 4, State.ESCALATED, ["refining", "escalated"], 100, 250)
    finished(store, 5, State.CANCELLED, ["new", "cancelled"], 100, 150)
    store.save_ticket(Ticket(id="#6", state=State.IMPLEMENTING, issue=6))
    store.save_ticket(Ticket(id="#7", state=State.IN_REVIEW, issue=7))
    store.close()
    d = collect_runs(tmp_path)
    assert d["total"] == 5 and len(d["runs"]) == 5
    assert [r["issue"] for r in d["runs"]] == [2, 3, 4, 1, 5]
    assert all(RUN_KEYS <= set(r) for r in d["runs"])
    assert {r["issue"]: r["outcome"] for r in d["runs"]} == {
        1: "merged", 2: "rejected_review", 3: "failed_gates", 4: "stalled", 5: "cancelled"}
    assert d["runs"][0]["max_implement_attempts"] == 3
    assert set(d["runs"][0]["blast"]) == {"files", "callers", "symbols", "uncovered", "covered"}


def test_collect_runs_duration_and_missing_events(tmp_path: Path):
    store = Store(state_db_path(tmp_path))
    finished(store, 1, State.MERGED, ["new", "gates", "merged"], 1000.0, 1090.5)
    finished(store, 2, State.ESCALATED, ["gates"], 500.0, 500.0)
    finished(store, 3, State.ESCALATED, [], None, None,
             history=[{"from": "gates", "to": "escalated", "actor": "e", "reason": ""},
                      {"from": "in_review", "to": "escalated", "actor": "e", "reason": ""}])
    store.close()
    runs = {r["issue"]: r for r in collect_runs(tmp_path)["runs"]}
    assert runs[1]["started_at"] == 1000.0 and runs[1]["completed_at"] == 1090.5
    assert runs[1]["duration_s"] == 90.5
    assert runs[2]["duration_s"] == 0 and runs[2]["outcome"] == "failed_gates"
    assert runs[3]["duration_s"] is None and runs[3]["started_at"] is None
    assert runs[3]["completed_at"] is None and runs[3]["outcome"] == "rejected_review"


def test_collect_runs_pr_blast_and_agent(tmp_path: Path):
    store = Store(state_db_path(tmp_path))
    finished(store, 1, State.MERGED, ["gates", "merged"], sessions={"pr": "77", "coder_agent": "claude"},
             implement_attempts=2, review_round=1)
    finished(store, 2, State.MERGED, ["gates", "merged"], 2000, 2100)
    add(store, "gates", {"ok": True, "impact": IMPACT}, issue=1)
    add(store, "implement", {"agent": "codex"}, issue=1)
    store.close()
    runs = {r["issue"]: r for r in collect_runs(tmp_path)["runs"]}
    assert runs[1]["pr_number"] == 77 and runs[1]["pr_url"].endswith("/pull/77")
    assert runs[1]["agent"] == "codex"
    assert runs[1]["blast"] == {"files": 1, "callers": 2, "symbols": 1, "uncovered": 1, "covered": 0}
    assert runs[1]["implement_attempts"] == 2 and runs[1]["review_rounds"] == 1
    assert runs[2]["pr_url"] is None and runs[2]["pr_number"] is None and runs[2]["agent"] is None
    assert runs[2]["blast"] == {"files": 0, "callers": 0, "symbols": 0, "uncovered": 0, "covered": 0}


def test_collect_runs_agent_falls_back_to_session(tmp_path: Path):
    store = Store(state_db_path(tmp_path))
    finished(store, 1, State.MERGED, ["merged"], sessions={"coder_agent": "claude"})
    store.close()
    assert collect_runs(tmp_path)["runs"][0]["agent"] == "claude"


def test_collect_runs_without_db_creates_nothing(tmp_path: Path):
    assert collect_runs(tmp_path) == {"runs": [], "total": 0}
    assert not (tmp_path / ".devflow").exists()


def test_collect_runs_skips_a_broken_ticket(tmp_path: Path):
    store = Store(state_db_path(tmp_path))
    finished(store, 1, State.MERGED, ["merged"], 100, 200)
    finished(store, 2, State.MERGED, ["merged"], 300, 400)
    store._db.execute("INSERT INTO stage_results (ticket_id, stage, attempt, ok, payload, ts)"
                      " VALUES ('#1','gates',1,1,'{not json',1)")
    store._db.commit()
    store.close()
    d = collect_runs(tmp_path)
    assert d["total"] == 1 and d["runs"][0]["issue"] == 2


def test_runs_endpoint_live(tmp_path: Path, monkeypatch):
    store = Store(state_db_path(tmp_path))
    for i in range(1, 61):
        finished(store, i, State.MERGED, ["gates", "merged"], 1000 + i, 1100 + i)
    store.close()
    server, port = serve(tmp_path)
    try:
        code, headers, data = get(port, "/api/runs?x=1")
        assert code == 200 and "application/json" in headers["Content-Type"]
        assert data["total"] == 60 and len(data["runs"]) == 60
        assert [r["issue"] for r in data["runs"]][:2] == [60, 59]
        assert "columns" in get(port, "/api/state")[2]
        assert get(port, "/api/ticket/999")[0] == 404
        assert get(port, "/api/nope")[0] == 404
        from devflow import ui
        monkeypatch.setattr(ui, "collect_runs", lambda root: 1 / 0)
        code, _, data = get(port, "/api/runs")
        assert code == 500 and "error" in data
    finally:
        server.shutdown()
        server.server_close()


def test_runs_endpoint_without_db(tmp_path: Path):
    server, port = serve(tmp_path)
    try:
        code, _, data = get(port, "/api/runs")
        assert code == 200 and data == {"runs": [], "total": 0}
        assert not (tmp_path / ".devflow").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_ledger_template_markers():
    t = HTML_TEMPLATE
    for marker in ('role="tablist"', 'role="tab"', "aria-selected", "Board View", "Runs Ledger", "Analytics",
                   'id="panel-board"', 'id="panel-runs"', 'id="panel-analytics"',
                   "stats-bar", "board-container",
                   "/api/runs", "Export Runs JSON", "runs.json", "new Blob", "download",
                   "All Runs", "Merged Only", "Failed / Blocked", "Multi-round Reviews",
                   "Ticket/PR", "Outcome", "Duration &amp; Timeline", "Iterations", "Blast Radius", "Actions",
                   "Inspect", "RUNS_PAGE_SIZE = 25", "Showing", "runs-prev", "runs-next",
                   "No finished runs yet", "No runs match the current filters",
                   "Total runs", "Merged rate", "Median duration", "Avg review rounds",
                   "setInterval(fetchState, 3000)", "openTicket(parseInt(el.dataset.issue", "ledger.page = 1"):
        assert marker in t, marker
    assert t.index("board-container") < t.index('id="drawer-overlay"')
    assert "location.hash = 'runs" not in t and "location.hash = name" not in t
    assert "e.key !== 'Enter' && e.key !== ' '" in t
    assert "closest('a')) return;      // the PR link" in t


def test_ledger_renders_through_escape_helpers():
    a = HTML_TEMPLATE.index("// ledger-pure-start")
    b = HTML_TEMPLATE.index("// ledger-pure-end")
    rows = HTML_TEMPLATE[HTML_TEMPLATE.index("function renderLedgerRows", a):HTML_TEMPLATE.index("function summarizeRuns", a)]
    for field in ("r.title", "r.agent", "r.outcome_label", "r.issue", "r.pr_number"):
        for line in rows.splitlines():
            if "${" + field in line:
                assert "escapeHtml(" + field in line, line
    assert "safeUrl(r.pr_url)" in rows
    assert a < b


def test_template_has_no_external_resources_after_ledger():
    for external in ('src="http', "src='http", 'rel="stylesheet"', "@import", "fonts.googleapis"):
        assert external not in HTML_TEMPLATE


def _node_ledger(script_body: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    esc_start = HTML_TEMPLATE.index("function escapeHtml")
    esc = HTML_TEMPLATE[esc_start:HTML_TEMPLATE.index("}\n", esc_start) + 2]
    safe_start = HTML_TEMPLATE.index("function safeUrl")
    safe = HTML_TEMPLATE[safe_start:HTML_TEMPLATE.index("function inlineMd")]
    pure = HTML_TEMPLATE[HTML_TEMPLATE.index("// ledger-pure-start"):HTML_TEMPLATE.index("// ledger-pure-end")]
    return subprocess.run([node, "-e", esc + safe + pure + script_body], capture_output=True, text=True,
                          check=True).stdout


def test_ledger_pipeline_under_node():
    out = _node_ledger("""
const mk = (i, o, rounds, dur, title, agent, ts) => ({issue: i, title, outcome: o, review_rounds: rounds,
  duration_s: dur, agent, completed_at: ts, implement_attempts: 1, max_implement_attempts: 3, blast: {}});
const runs = [mk(12, 'merged', 0, 60, 'Fix Login', 'claude', 3), mk(13, 'rejected_review', 2, null, 'Other', 'codex', 2),
  mk(14, 'stalled', 1, 10, 'Third', null, 1), mk(15, 'cancelled', 3, 30, 'Fourth', null, 4)];
const st = (o) => Object.assign({filter: 'all', q: '', sort: 'date', dir: 'desc', page: 1}, o);
const ids = (o) => runsView(runs, st(o)).all.map(r => r.issue).join(',');
const res = {
  title: ids({q: 'LOGIN'}), hash: ids({q: '#12'}), plain: ids({q: '12'}), agent: ids({q: 'codex'}),
  failed: ids({filter: 'failed'}), multi: ids({filter: 'multi'}), combo: ids({filter: 'failed', q: 'o'}),
  date: ids({}), dateAsc: ids({dir: 'asc'}), dur: ids({sort: 'duration'}), durAsc: ids({sort: 'duration', dir: 'asc'}),
  rounds: ids({sort: 'rounds'}),
};
const big = Array.from({length: 60}, (_, i) => mk(i + 1, 'merged', 0, 1, 't', 'a', i));
const pages = [1, 2, 3].map(p => { const v = runsView(big, st({page: p})); return [v.items.length, v.label]; });
const empty = runsView([], st({}));
const sum = summarizeRuns(runs);
console.log(JSON.stringify({res, pages, emptyAll: renderLedgerRows([], 0), emptyMatch: renderLedgerRows([], 4),
  zero: summarizeRuns([]), emptyLabel: empty.label, sum}));
""")
    import json as _j
    d = _j.loads(out)
    r = d["res"]
    assert r["title"] == "12" and r["hash"] == "12" and r["plain"] == "12" and r["agent"] == "13"
    assert r["failed"] == "15,13,14" and r["multi"] == "15,13" and r["combo"] == "15,13"
    assert r["date"] == "15,12,13,14" and r["dateAsc"] == "14,13,12,15"
    assert r["dur"] == "12,15,14,13" and r["durAsc"] == "14,15,12,13"
    assert r["rounds"] == "15,13,14,12"
    assert d["pages"] == [[25, "Showing 1-25 of 60"], [25, "Showing 26-50 of 60"], [10, "Showing 51-60 of 60"]]
    assert d["emptyAll"] != d["emptyMatch"]
    assert d["zero"]["total"] == 0 and d["zero"]["mergedRate"] is None and d["zero"]["avgRounds"] is None
    s = d["sum"]
    assert s["total"] == 4 and s["mergedRate"] == 0.25 and s["medianDuration"] == 30
    assert s["avgRounds"] == 1.5 and s["counts"]["cancelled"] == 1


def test_ledger_is_inert_under_node():
    out = _node_ledger("""
console.log(renderLedgerRows([{issue: 1, title: '<img src=x onerror=alert(1)>', outcome: 'merged',
  outcome_label: '<b>M</b>', pr_url: 'javascript:alert(1)', pr_number: 3, agent: '<script>',
  review_rounds: 2, implement_attempts: 1, max_implement_attempts: 3, blast: {}}], 1));
""")
    assert "<img" not in out and "<script" not in out and "<b>" not in out and "javascript:" not in out
