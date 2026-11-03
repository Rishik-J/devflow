"""Tests for devflow ui."""

import http.server
import json
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from devflow.ui import HTML_TEMPLATE, UiHandler, collect_state


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


def test_board_cache_and_local_fallback_use_real_state(tmp_path: Path, monkeypatch):
    from devflow import ui
    from devflow.states import State, Ticket
    from devflow.store import Store, state_db_path

    db = state_db_path(tmp_path)
    db.parent.mkdir(parents=True)
    with Store(db) as store:
        for n, state in ((1, State.MERGED), (2, State.AWAITING_MERGE_APPROVAL), (3, State.IMPLEMENTING)):
            store.save_ticket(Ticket(id=f"#{n}", issue=n, title=f"T{n}", state=state))

    class Cfg:
        name = "r"
        class board:  # noqa: N801
            owner, project = "o", 7

    monkeypatch.setattr(ui, "load_config", lambda root: Cfg)
    calls = []

    class Boom:
        def __init__(self, *a):
            calls.append(a)

        def items(self):
            raise RuntimeError("GraphQL: API rate limit exceeded")

    import devflow.board
    monkeypatch.setattr(devflow.board, "GhBoard", Boom)
    ui._BOARD_CACHE.clear()
    first = ui.collect_state(tmp_path)
    for _ in range(5):
        ui.collect_state(tmp_path)
    assert len(calls) == 1  # failure is cached; polling does not hammer GitHub
    cols = {k: [i["issue"] for i in v] for k, v in first["columns"].items()}
    assert cols["Done"] == [1] and cols["In review"] == [2] and cols["In progress"] == [3]
    assert first["source"] == "local" and "rate limit" in first["board_error"]


def test_board_cache_serves_last_good_items_when_github_fails(monkeypatch):
    from devflow import ui

    class Item:
        issue, title, status, priority, labels = 5, "x", "Ready", None, []

    state = {"fail": False}

    class Fake:
        def __init__(self, *a):
            pass

        def items(self):
            if state["fail"]:
                raise RuntimeError("down")
            return [Item]

    import devflow.board
    monkeypatch.setattr(devflow.board, "GhBoard", Fake)
    ui._BOARD_CACHE.clear()
    items, err = ui._board_items("o/r", "o", 1, now=0)
    assert len(items) == 1 and err is None
    state["fail"] = True
    assert ui._board_items("o/r", "o", 1, now=30) == (items, None)  # within TTL: cached, no call
    items2, err2 = ui._board_items("o/r", "o", 1, now=100)  # TTL expired and GitHub is down
    assert items2 == items and err2 == "down"
