"""The collector and report plumbing, tested with a fake page and no Playwright."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from devflow import uilane, uirunner
from devflow.uirunner import ConsoleCollector, case_ids_for, observe, ui_case

ROOT = Path(__file__).resolve().parent.parent


class FakePage:
    def __init__(self):
        self.handlers: dict[str, list] = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, payload):
        for h in self.handlers.get(event, []):
            h(payload)


def console(kind, text):
    return SimpleNamespace(type=kind, text=text)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.delenv(uirunner.REPORT_ENV, raising=False)
    monkeypatch.delenv(uirunner.ARTIFACTS_ENV, raising=False)
    uirunner.reset()
    yield
    uirunner.reset()


# ---------------------------------------------------------------- the ui_case marker


class Node:
    def __init__(self, *marks):
        self.marks = marks

    def iter_markers(self, name):
        return [SimpleNamespace(args=m) for m in self.marks]


def test_ui_case_marker_records_ids():
    mark = ui_case("UC1", "UC2")
    assert mark.name == "ui_case" and mark.args == ("UC1", "UC2")
    with pytest.raises(ValueError):
        ui_case()
    with pytest.raises(ValueError):
        ui_case("")


def test_case_ids_default_empty_and_repeated_markers_are_preserved():
    assert case_ids_for(Node()) == []
    assert case_ids_for(Node(("UC1", "UC2"), ("UC3",))) == ["UC1", "UC2", "UC3"]


# ---------------------------------------------------------------- what is captured (AC5)


def collect(event, payload):
    page = FakePage()
    collector = ConsoleCollector(page)
    page.emit(event, payload)
    return collector


@pytest.mark.parametrize("kind", ["warning", "error"])
def test_console_warning_and_error_are_errors(kind):
    errors = collect("console", console(kind, "bad thing")).errors()
    assert errors == [f"console {kind}: bad thing"]


@pytest.mark.parametrize("kind", ["log", "info", "debug"])
def test_console_info_is_not_an_error(kind):
    assert collect("console", console(kind, "hello")).errors() == []


def test_page_error_is_an_error():
    errors = collect("pageerror", SimpleNamespace(message="TypeError: x is undefined")).errors()
    assert errors == ["page error: TypeError: x is undefined"]
    assert collect("pageerror", ValueError("plain")).errors() == ["page error: plain"]


def test_failed_request_names_url_and_reason():
    req = SimpleNamespace(url="http://x/api", failure="net::ERR_CONNECTION_REFUSED")
    assert collect("requestfailed", req).errors() == [
        "request failed: http://x/api (net::ERR_CONNECTION_REFUSED)"]


def test_a_request_the_page_aborted_itself_is_not_an_error():
    req = SimpleNamespace(url="http://x/api", failure="net::ERR_ABORTED")
    assert collect("requestfailed", req).errors() == []


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_error_responses_name_status_and_url(status):
    resp = SimpleNamespace(url="http://x/a", status=status)
    assert collect("response", resp).errors() == [f"HTTP {status} http://x/a"]


@pytest.mark.parametrize("status", [200, 204, 304])
def test_ok_responses_are_fine(status):
    assert collect("response", SimpleNamespace(url="http://x/a", status=status)).errors() == []


def test_methods_work_as_well_as_properties():
    resp = SimpleNamespace(url=lambda: "http://x/m", status=lambda: 502)
    assert collect("response", resp).errors() == ["HTTP 502 http://x/m"]


def test_expected_http_error_removes_only_that_response_and_its_console_noise():
    page = FakePage()
    c = ConsoleCollector(page)
    c.expect_http_error("/api/ticket/9", 404)
    page.emit("response", SimpleNamespace(url="http://h/api/ticket/9", status=404))
    page.emit("console", console("error", "Failed to load resource: the server responded "
                                          "with a status of 404 (Not Found)"))
    assert c.errors() == []
    page.emit("response", SimpleNamespace(url="http://h/api/other", status=404))
    page.emit("console", console("error", "real problem"))
    assert c.errors() == ["console error: real problem", "HTTP 404 http://h/api/other"]


# ---------------------------------------------------------------- failing the case


def test_case_fails_at_teardown_even_when_the_test_assertions_passed():
    page = FakePage()
    with (pytest.raises(AssertionError, match="console error: oops"),
          observe(page, "t::a", ["UC1"])):
        page.emit("console", console("error", "oops"))
        assert True  # the test's own assertions pass
    case = uirunner._STATE["cases"][0]
    assert case.ok is False and case.use_case_ids == ["UC1"]
    assert case.console == [{"type": "error", "text": "oops"}]


def test_clean_page_passes_and_records_the_case():
    page = FakePage()
    with observe(page, "t::b", ["UC2"]):
        page.emit("console", console("log", "fine"))
    case = uirunner._STATE["cases"][0]
    assert case.ok and case.errors == [] and case.use_case_ids == ["UC2"]


def test_a_failing_test_body_is_recorded_as_failed_and_reraised():
    with pytest.raises(RuntimeError), observe(FakePage(), "t::c", []):
        raise RuntimeError("boom")
    case = uirunner._STATE["cases"][0]
    assert not case.ok and "boom" in case.errors[0] and case.use_case_ids == []


def test_each_error_kind_fails_the_case():
    events = [("console", console("warning", "w")), ("pageerror", ValueError("e")),
              ("requestfailed", SimpleNamespace(url="u", failure="net::ERR_FAILED")),
              ("response", SimpleNamespace(url="u", status=500))]
    for event, payload in events:
        page = FakePage()
        with pytest.raises(AssertionError), observe(page, "t::d", []):
            page.emit(event, payload)


def test_report_is_written_when_requested_and_loads_strictly(tmp_path, monkeypatch):
    report = tmp_path / "r.json"
    art = tmp_path / "art"
    monkeypatch.setenv(uirunner.REPORT_ENV, str(report))
    monkeypatch.setenv(uirunner.ARTIFACTS_ENV, str(art))

    class ShotPage(FakePage):
        def screenshot(self, path):
            Path(path).write_bytes(b"png")

        def content(self):
            return "<html></html>"

    page = ShotPage()
    with observe(page, "tests/ui/x.py::y", ["UC1"]):
        pass
    loaded = uilane.load_report(report)
    assert loaded.cases[0].id == "tests/ui/x.py::y" and loaded.cases[0].ok
    assert Path(loaded.cases[0].screenshot).read_bytes() == b"png"
    assert Path(loaded.cases[0].dom).read_text() == "<html></html>"


def test_unavailable_is_written_to_the_report(tmp_path, monkeypatch):
    report = tmp_path / "r.json"
    monkeypatch.setenv(uirunner.REPORT_ENV, str(report))
    uirunner.mark_unavailable("no chromium")
    assert uilane.load_report(report).unavailable == "no chromium"


# ---------------------------------------------------------------- a real browser (AC5)


def test_real_browser_catches_console_error_pageerror_and_http_500(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/boom":
                self.send_response(500)
                self.end_headers()
                return
            body = (b"<html><body><script>console.error('bad');"
                    b"setTimeout(function(){throw new Error('uncaught')},0);"
                    b"fetch('/boom');</script></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report = tmp_path / "r.json"
    art = tmp_path / "art"
    os.environ[uirunner.REPORT_ENV], os.environ[uirunner.ARTIFACTS_ENV] = str(report), str(art)
    try:
        try:
            pw = sync_api.sync_playwright().start()
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"browser unavailable: {exc}")
        try:
            page = browser.new_page()
            with pytest.raises(AssertionError) as info, observe(page, "t::real", ["UC1"]):
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
                page.wait_for_function("true")
                page.wait_for_timeout(300)
            message = str(info.value)
            assert "console error: bad" in message
            assert "page error: " in message and "uncaught" in message
            assert "HTTP 500" in message and "/boom" in message
        finally:
            browser.close()
            pw.stop()
    finally:
        os.environ.pop(uirunner.REPORT_ENV, None)
        os.environ.pop(uirunner.ARTIFACTS_ENV, None)
        server.shutdown()
    case = uilane.load_report(report).cases[0]
    assert not case.ok and case.screenshot and case.dom
    assert Path(case.screenshot).exists() and Path(case.dom).exists()


def test_json_of_plugin_report_is_stable(tmp_path):
    """Run the plugin in a subprocess with a fake-free, browser-free test: report still exists."""
    (tmp_path / "test_nothing.py").write_text("def test_x():\n    pass\n")
    report = tmp_path / "r.json"
    env = {**os.environ, uirunner.REPORT_ENV: str(report)}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path / "test_nothing.py"), "-q",
         "-p", "devflow.uirunner", "-p", "no:cacheprovider"],
        check=False, capture_output=True, text=True, env=env, cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout
    assert json.loads(report.read_text()) == {"cases": []}
