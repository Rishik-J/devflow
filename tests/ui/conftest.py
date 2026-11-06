"""Fixtures for the dashboard browser scenarios (collected only when DEVFLOW_UI_LANE=1)."""

import http.server
import socketserver
import threading
from pathlib import Path

import pytest

from devflow.states import State
from devflow.ui import UiHandler
from tests.test_ui import add, seed


def _offline(slug):
    raise RuntimeError("offline")


@pytest.fixture
def ui_server(tmp_path: Path):
    """The dashboard on an ephemeral port, over a seeded store. Yields the base URL."""
    store = seed(tmp_path, issue=38, state=State.GATES)
    add(store, "plan", {"goal": "G", "acceptance_criteria": [{"id": "AC1", "text": "x"}],
                        "steps": [{"id": "S1", "description": "d", "files": ["a.py"]}]})
    add(store, "gates", {"ok": True, "summary": "all gates pass", "unconfigured": [],
                         "findings": [], "results": [], "failures": []})
    store.close()

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    class Bound(UiHandler):
        root = tmp_path
        host_factory = staticmethod(_offline)

    server = Server(("127.0.0.1", 0), Bound)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
