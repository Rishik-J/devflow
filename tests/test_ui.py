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
    UiHandler,
    collect_live,
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


# ---------------------------------------------------------------- live flight deck

def live_ticket(store: Store, issue: int, state: State, **kw) -> Ticket:
    t = Ticket(id=f"#{issue}", state=state, issue=issue, title=f"T{issue}", **kw)
    store.save_ticket(t)
    return t


def write_spans(root: Path, name: str, durations: list[float], status: str = "ok") -> None:
    path = root / ".devflow" / "state" / "traces.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for d in durations:
            fh.write(json.dumps({"name": name, "status": status, "start_ns": 0,
                                 "end_ns": int(d * 1e9)}) + "\n")


def test_live_no_db(tmp_path: Path):
    res = collect_live(tmp_path)
    assert res["runs"] == [] and set(res["runner"]) == {"running", "pid"}
    assert "now" in res and not state_db_path(tmp_path).exists()


def test_live_endpoint(tmp_path: Path):
    server, port = serve(tmp_path)
    try:
        for path in ("/api/live", "/api/live?x=1"):
            code, _, data = get(port, path)
            assert code == 200 and {"now", "runner", "runs"} <= set(data) and data["runs"] == []
        assert not state_db_path(tmp_path).exists()
        assert get(port, "/api/state")[0] == 200
    finally:
        server.shutdown(); server.server_close()


def test_live_endpoint_error(tmp_path: Path, monkeypatch):
    from devflow import ui

    def boom(root):
        raise RuntimeError("nope")
    monkeypatch.setattr(ui, "collect_live", boom)
    server, port = serve(tmp_path)
    try:
        code, _, data = get(port, "/api/live")
        assert code == 500 and data["error"] == "nope"
        assert get(port, "/api/state")[0] == 200
    finally:
        server.shutdown(); server.server_close()


def test_runner_status(tmp_path: Path):
    import os

    from devflow.runner import ownership, runner_status
    assert runner_status(tmp_path) == {"running": False, "pid": None}  # not a git repo
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    lock = tmp_path / ".git" / "devflow-runner.lock"
    assert runner_status(tmp_path) == {"running": False, "pid": None} and not lock.exists()
    lock.write_text("garbage")
    assert runner_status(tmp_path)["running"] is False
    lock.write_text("999999999")
    assert runner_status(tmp_path)["running"] is False
    lock.write_text(str(os.getpid()))
    assert runner_status(tmp_path) == {"running": True, "pid": os.getpid()}
    with ownership(tmp_path):
        assert runner_status(tmp_path)["pid"] == os.getpid()


def test_live_filters_orders_and_maps(tmp_path: Path):
    store = Store(state_db_path(tmp_path))
    for i, st in enumerate(State, start=1):
        live_ticket(store, i, st)
        time.sleep(0.01)
    store.close()
    runs = collect_live(tmp_path)["runs"]
    got = [r["state"] for r in runs]
    assert set(got) == {"refining", "test_design", "implementing", "gates", "in_review"}
    ids = [r["issue"] for r in runs]
    assert ids == sorted(ids, reverse=True)
    by = {r["state"]: r for r in runs}
    assert by["refining"]["node"] == "refine" and by["refining"]["completed_nodes"] == []
    assert by["test_design"]["node"] == "plan"
    assert by["in_review"]["node"] == "review"
    assert by["in_review"]["completed_nodes"] == ["refine", "plan", "implement", "gates"]
    for r in runs:
        assert {"issue", "title", "state", "node", "completed_nodes", "stage_started_at", "elapsed_s",
                "agent", "branch", "worktree", "commit", "attempt", "review_round",
                "benchmark", "feed"} <= set(r)


def test_live_retry_from_gates(tmp_path: Path):
    store = Store(state_db_path(tmp_path))
    t = live_ticket(store, 5, State.GATES)
    store.transition(t, State.IMPLEMENTING, actor="engine", reason="retry")
    store.close()
    r = collect_live(tmp_path)["runs"][0]
    assert r["node"] == "implement" and r["completed_nodes"] == ["refine", "plan"]


def test_live_elapsed(tmp_path: Path):
    store = Store(state_db_path(tmp_path))
    live_ticket(store, 1, State.GATES)
    t = store.get_ticket("#1")
    store.add_event("#1", "transition", {"to": "gates"})
    ts = store.latest_transition_ts("#1", State.GATES)
    assert store.latest_transition_ts("#1") == ts and store.latest_transition_ts("#1", State.MERGED) is None
    live_ticket(store, 2, State.GATES)
    upd = store.ticket_updated_at("#2")
    store.close()
    assert t is not None
    runs = {r["issue"]: r for r in collect_live(tmp_path, now=ts + 30)["runs"]}
    assert runs[1]["elapsed_s"] == pytest.approx(30)
    assert runs[2]["elapsed_s"] == pytest.approx(max(0, ts + 30 - upd), abs=1e-2)
    assert collect_live(tmp_path, now=0)["runs"][0]["elapsed_s"] == 0


def test_live_benchmark(tmp_path: Path):
    from devflow import stall
    store = Store(state_db_path(tmp_path))
    live_ticket(store, 1, State.GATES)
    store.close()
    ts = Store(state_db_path(tmp_path)).ticket_updated_at("#1")
    b = collect_live(tmp_path, now=ts + 1000)["runs"][0]["benchmark"]
    assert b == {"p50": None, "p99": None, "samples": 0, "stalled": False}  # missing log
    write_spans(tmp_path, "stage.gates", [10, 11, 12, 13])
    write_spans(tmp_path, "stage.gates", [99], status="error")
    write_spans(tmp_path, "stage.implementing", [500] * 6)
    log = tmp_path / ".devflow" / "state" / "traces.jsonl"
    with log.open("a") as fh:
        fh.write("not json\n")
    b = collect_live(tmp_path, now=ts + 1000)["runs"][0]["benchmark"]
    assert b["samples"] == 4 and b["p99"] is None and b["stalled"] is False
    write_spans(tmp_path, "stage.gates", [14])
    vals = [10, 11, 12, 13, 14]
    for elapsed, stalled in ((1000, True), (5, False)):
        b = collect_live(tmp_path, now=ts + elapsed)["runs"][0]["benchmark"]
        assert b["p50"] == pytest.approx(stall.percentile(vals, 50))
        assert b["p99"] == pytest.approx(stall.percentile(vals, 99), abs=1e-3)
        assert b["stalled"] is stalled
        assert (stall.check_stall("g", elapsed, vals) is not None) is stalled
    log.write_text("")
    assert collect_live(tmp_path)["runs"][0]["benchmark"]["stalled"] is False


def test_live_feed(tmp_path: Path):
    store = seed(tmp_path)
    for i in range(25):
        store.add_event("#38", "transition", {"to": "gates", "actor": "engine", "reason": f"r{i}"})
    store.add_event("#38", "error", {"state": "gates", "error": "boom", "reply": "SECRET-EVENT"})
    add(store, "plan", {"goal": "x", "reply": "SECRET-PLAN"})
    add(store, "gates", {"results": [{"command": "ruff check .", "exit_code": 0, "duration_s": 1.4},
                                     {"command": "pytest", "exit_code": 1, "duration_s": 2.0}],
                         "reply": "SECRET-GATES"})
    add(store, "review", {"verdict": "approve", "reply": "SECRET-REVIEW"})
    store.close()
    feed = collect_live(tmp_path)["runs"][0]["feed"]
    assert len(feed) == 20
    assert [e["ts"] for e in feed] == sorted(e["ts"] for e in feed)
    assert all(set(e) == {"ts", "level", "text"} and e["level"] in ("info", "ok", "fail") for e in feed)
    texts = [e["text"] for e in feed]
    assert "Gate: ruff check . ran in 1.4s (PASS)" in texts
    assert "Gate: pytest ran in 2.0s (FAIL)" in texts
    assert any(e["level"] == "fail" and "boom" in e["text"] for e in feed)
    assert any("Review verdict: approve" in t for t in texts)
    assert "SECRET" not in json.dumps(feed)


def test_live_agent_resolution(tmp_path: Path):
    store = seed(tmp_path, state=State.IMPLEMENTING)
    r = collect_live(tmp_path)["runs"][0]
    assert r["agent"] == "Claude Code"  # sessions coder_agent
    add(store, "implement", {"agent": "codex"})
    assert collect_live(tmp_path)["runs"][0]["agent"] == "Codex"
    store.record_usage("openclaw", "planner", "#38", cost_usd=0)
    assert collect_live(tmp_path)["runs"][0]["agent"] == "OpenClaw Planner"
    store.record_usage("mystery", "coder", "#38", cost_usd=0)
    assert collect_live(tmp_path)["runs"][0]["agent"] == "mystery"
    assert store.latest_usage("#38")["agent"] == "mystery"
    live_ticket(store, 2, State.REFINING)
    assert {x["issue"]: x["agent"] for x in collect_live(tmp_path)["runs"]}[2] is None
    store.close()


def test_live_commit_and_robustness(tmp_path: Path, monkeypatch):
    store = Store(state_db_path(tmp_path))
    live_ticket(store, 1, State.GATES, worktree=str(tmp_path / "missing"), base_sha="abcdef0123")
    live_ticket(store, 2, State.GATES)
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=a", "-c", "user.email=a@b", "commit",
                    "-q", "--allow-empty", "-m", "x"], check=True)
    live_ticket(store, 3, State.GATES, worktree=str(wt), base_sha="ffffffff")
    store.close()
    runs = {r["issue"]: r for r in collect_live(tmp_path)["runs"]}
    assert runs[1]["commit"] == "abcdef0" and runs[2]["commit"] is None
    head = subprocess.run(["git", "-C", str(wt), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert runs[3]["commit"] == head
    from devflow import ui

    def bad(*a, **k):
        raise subprocess.TimeoutExpired("git", 2)
    monkeypatch.setattr(subprocess, "run", bad)
    assert collect_live(tmp_path)["runs"]
    real = ui._live_run

    def flaky(root, st, t, now):
        if t.issue == 1:
            raise ValueError("corrupt")
        return real(root, st, t, now)
    monkeypatch.setattr(ui, "_live_run", flaky)
    assert sorted(r["issue"] for r in collect_live(tmp_path)["runs"]) == [2, 3]


def test_live_json_is_raw_data(tmp_path: Path):
    store = Store(state_db_path(tmp_path))
    live_ticket(store, 1, State.GATES, branch="<img src=x>", sessions={"coder_agent": "<b>x</b>"})
    store.close()
    server, port = serve(tmp_path)
    try:
        run = get(port, "/api/live")[2]["runs"][0]
        assert run["branch"] == "<img src=x>" and run["agent"] == "<b>x</b>"
    finally:
        server.shutdown(); server.server_close()


def test_flight_deck_template_markers():
    t = HTML_TEMPLATE
    assert t.index('id="flight-deck"') < t.index('class="stats-bar"')
    for m in ("['refine', '01', 'Refine']", "['plan', '02', 'Plan']", "['implement', '03', 'Implement']",
              "['gates', '04', 'Verify Gates']", "['review', '05', 'Independent Review']",
              "' more'", "'Copied'", "navigator.clipboard",
              "document.execCommand", "/api/live", "performance.now()", "p50 ", "p99 ",
              "Stall Warning: Stage exceeding 99th percentile", "FD_ACTIVE_MS = 2500",
              "FD_IDLE_MS = 10000", "fd.busy", "setTimeout(pollLive", "aria-live=\"polite\"",
              "min-height", "setInterval(fetchState, 3000)", "@media (max-width: 640px)",
              "@media (prefers-reduced-motion: reduce)", "@keyframes fd-glow", "@keyframes fd-flow",
              "keep the last good view", "scrollTop = feed.scrollHeight", "feedSig"):
        assert m in t, m
    deck_js = t[t.index("live flight deck", t.index("<script>")):]
    assert "innerHTML" not in deck_js


# ---------------------------------------------------------------- analytics endpoint & template
ANALYTICS_KEYS = {"window", "generated_at", "telemetry", "kpis", "stages", "bottleneck", "stalls",
                  "friction", "blast"}


def _raw_get(port: int, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
            return r.status, r.headers, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


def test_analytics_endpoint_windows_and_errors(tmp_path: Path):
    log = tmp_path / ".devflow/state/traces.jsonl"
    log.parent.mkdir(parents=True)
    now = time.time_ns()
    rec = {"name": "stage.gates", "start_ns": now - 5_000_000_000, "end_ns": now, "status": "ok",
           "attributes": {}}
    log.write_text(json.dumps(rec) + "\n" + json.dumps({**rec, "start_ns": float("nan")}) + "\n")
    server, port = serve(tmp_path)
    try:
        code, headers, body = _raw_get(port, "/api/analytics")
        data = json.loads(body, parse_constant=lambda c: pytest.fail("non-strict JSON: " + c))
        assert code == 200 and ANALYTICS_KEYS <= set(data) and data["window"] == "all"
        assert headers["Cache-Control"] == "no-cache" and "application/json" in headers["Content-Type"]
        gates = next(s for s in data["stages"] if s["stage"] == "Gates")
        assert gates["count"] == 1 and data["bottleneck"] == "Gates"
        for window in ("24h", "7d", "all"):
            code, _, data = get(port, f"/api/analytics?window={window}")
            assert code == 200 and data["window"] == window
        code, _, data = get(port, "/api/analytics?window=")
        assert code == 200 and data["window"] == "all"
        code, _, data = get(port, "/api/analytics?window=bogus")
        assert code == 400 and "error" in data
    finally:
        server.shutdown()
        server.server_close()


def test_analytics_endpoint_no_db_no_traces_creates_nothing(tmp_path: Path):
    server, port = serve(tmp_path)
    try:
        code, _, data = get(port, "/api/analytics")
    finally:
        server.shutdown()
        server.server_close()
    assert code == 200 and data["telemetry"] == {"available": False, "spans": 0}
    assert all(k["value"] is None for k in data["kpis"].values()) and data["bottleneck"] is None
    assert not (tmp_path / ".devflow").exists() and not state_db_path(tmp_path).exists()


def test_analytics_endpoint_unexpected_failure_is_500(tmp_path: Path, monkeypatch):
    from devflow import analytics

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(analytics, "collect_analytics", boom)
    server, port = serve(tmp_path)
    try:
        code, _, data = get(port, "/api/analytics")
    finally:
        server.shutdown()
        server.server_close()
    assert code == 500 and data == {"error": "kaboom"}


def test_other_endpoints_keep_their_shape(tmp_path: Path):
    full_store(tmp_path)
    server, port = serve(tmp_path, FakeIssueHost)
    try:
        assert {"columns", "repo"} <= set(get(port, "/api/state")[2])
        assert set(get(port, "/api/live")[2]) == {"now", "runner", "runs"}
        assert KEYS <= set(get(port, "/api/ticket/38")[2])
    finally:
        server.shutdown()
        server.server_close()


def test_analytics_template_markers():
    t = HTML_TEMPLATE
    for marker in ('data-view="board"', 'data-view="analytics"', 'id="analytics-view"',
                   "Last 24 Hours", "Past 7 Days", "All Time", 'id="kpi-cycle"', 'id="kpi-firstpass"',
                   'id="kpi-short"', 'id="kpi-stalls"', 'id="stage-chart"', 'id="stall-radar"',
                   'id="stage-table"', 'id="friction-review"', 'id="friction-gates"', 'id="blast-avg"',
                   "function fmtDuration", "function fmtPct", "function escapeHtml", "createElementNS",
                   "const DASH = '—'", "/api/analytics?window=", "No telemetry recorded", "No stage spans",
                   "prefers-reduced-motion", "Bottleneck", "ELEVATED", "#analytics"):
        assert marker in t, marker
    assert "setInterval(loadAnalytics" not in t and "setInterval(fetchAnalytics" not in t


def _analytics_js() -> str:
    return HTML_TEMPLATE.split("// ---------------------------------------------------------------- analytics view")[1]


def test_analytics_template_is_offline_and_inert():
    import re
    t = HTML_TEMPLATE
    assert "<script src" not in t and "<link" not in t and "@import" not in t
    assert "<img" not in t and "url(http" not in t
    urls = set(re.findall(r"https?://[^\s'\"<>)]+", t))
    assert all(u.startswith(("https://github.com/", "http://www.w3.org/2000/svg")) for u in urls), urls
    js = _analytics_js()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js
    assert "textContent" in js


def test_fmt_duration_contract():
    js = _analytics_js().split("function fmtDuration")[1].split("function fmtPct")[0]
    # the formatter's literal rules: seconds below a minute, then "Xm Ys"
    assert "t + 's'" in js and "m + 'm ' + sec + 's'" in js


def test_hash_routing_keeps_ticket_links_and_board_machinery():
    t = HTML_TEMPLATE
    sync = t.split("function syncFromHash()")[1].split("window.addEventListener('hashchange'")[0]
    assert "#ticket-(\\d+)" in sync and "openTicket(parseInt(m[1], 10), true)" in sync
    assert "location.hash === '#analytics'" in sync and "closeDrawer(false)" in sync
    # board/flight-deck are hidden by a body class, so listeners and polling stay intact
    assert "body.view-analytics #flight-deck" in t and "body.view-analytics #board-container" in t
    for kept in ("setInterval(fetchState, 3000)", "pollLive();", "boardEl.addEventListener('click'",
                 "e.key === 'Tab' && overlay.classList.contains('open')"):
        assert kept in t, kept
