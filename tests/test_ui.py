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
