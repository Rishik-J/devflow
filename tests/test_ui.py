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
from devflow.ui import HTML_TEMPLATE, LIFECYCLE, UiHandler, collect_state, collect_ticket


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


def test_markdown_is_inert_in_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    start = HTML_TEMPLATE.index("function safeUrl")
    end = HTML_TEMPLATE.index("function chip(")
    esc_start = HTML_TEMPLATE.index("function escapeHtml")
    esc = HTML_TEMPLATE[esc_start:HTML_TEMPLATE.index("}\n", esc_start) + 2]
    script = esc + HTML_TEMPLATE[start:end] + (
        "console.log(renderMarkdown('[x](javascript:alert(1)) <img src=x onerror=alert(1)> "
        "[ok](https://e.com/a?b=1&c=2)'));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout
    assert "<img" not in out and "href=\"javascript" not in out
    assert 'href="https://e.com/a?b=1&amp;c=2"' in out
