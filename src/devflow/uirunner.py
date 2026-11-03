"""Pytest plugin for the UI lane: drive a page, and fail on anything the browser complains about.

Use it with `-p devflow.uirunner`. A test asks for the `ui_page` fixture (a headless Chromium
page with a fixed viewport) and tags the use cases it covers with `@ui_case("UC1", ...)`. The
runner records console warnings and errors, uncaught page errors, failed requests and responses
with status >= 400. Any of them fails the case at teardown, even if the test's own assertions
passed. When `DEVFLOW_UI_REPORT` is set, each case is written to that JSON file (see uilane).

Playwright is imported lazily, so importing this module and testing the collector with a fake
page object needs neither Playwright nor a browser.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from .uilane import UiCase, UiLaneReport

REPORT_ENV, ARTIFACTS_ENV = "DEVFLOW_UI_REPORT", "DEVFLOW_UI_ARTIFACTS"
VIEWPORT = {"width": 1280, "height": 800}
BAD_CONSOLE = ("warning", "error")
# A request the page cancelled itself (reload or navigation while a poll was in flight) is not
# a failure of the app.
BENIGN_FAILURES = ("ERR_ABORTED",)

_STATE: dict[str, Any] = {"cases": [], "unavailable": ""}


def _val(obj: Any, name: str) -> Any:
    """Playwright exposes these as properties; fakes may use methods. Accept both."""
    value = getattr(obj, name, None)
    return value() if callable(value) else value


class ConsoleCollector:
    """Subscribes to a page-like object (anything with `.on(event, handler)`)."""

    def __init__(self, page: Any):
        self.console: list[dict] = []
        self.page_errors: list[str] = []
        self.network_failures: list[dict] = []
        self._expected: list[tuple[str, int]] = []
        page.on("console", self._on_console)
        page.on("pageerror", self._on_pageerror)
        page.on("requestfailed", self._on_requestfailed)
        page.on("response", self._on_response)

    def _on_console(self, msg: Any) -> None:
        kind = str(_val(msg, "type"))
        if kind in BAD_CONSOLE:
            self.console.append({"type": kind, "text": str(_val(msg, "text"))})

    def _on_pageerror(self, exc: Any) -> None:
        self.page_errors.append(str(_val(exc, "message") or exc))

    def _on_requestfailed(self, request: Any) -> None:
        failure = _val(request, "failure")
        failure = getattr(failure, "error_text", failure)
        text = str(failure or "request failed")
        if any(b in text for b in BENIGN_FAILURES):
            return
        self.network_failures.append(
            {"url": str(_val(request, "url")), "status": None, "failure": text})

    def _on_response(self, response: Any) -> None:
        status = _val(response, "status")
        if isinstance(status, int) and status >= 400:
            self.network_failures.append(
                {"url": str(_val(response, "url")), "status": status, "failure": ""})

    def expect_http_error(self, url_part: str, status: int) -> None:
        """Declare that this test deliberately provokes `status` responses from `url_part`.

        It removes those responses and the browser's matching "Failed to load resource"
        console errors (a poll may repeat the request). Everything else still fails the case.
        """
        self._expected.append((url_part, status))

    def _unexpected(self) -> tuple[list[dict], list[dict]]:
        network, console = list(self.network_failures), list(self.console)
        for url_part, status in self._expected:
            hits = [n for n in network if n["status"] == status and url_part in n["url"]]
            network = [n for n in network if n not in hits]
            if hits:
                console = [c for c in console if not (
                    c["type"] == "error" and "Failed to load resource" in c["text"]
                    and f"status of {status}" in c["text"])]
        return console, network

    def errors(self) -> list[str]:
        console, network = self._unexpected()
        out = [f"console {c['type']}: {c['text']}" for c in console]
        out += [f"page error: {e}" for e in self.page_errors]
        for n in network:
            out.append(f"HTTP {n['status']} {n['url']}" if n["status"] is not None
                       else f"request failed: {n['url']} ({n['failure']})")
        return out


# ---------------------------------------------------------------- marker and report


def ui_case(*use_case_ids: str) -> pytest.MarkDecorator:
    """Tag a scenario with the plan use-case ids it covers."""
    if not use_case_ids or not all(isinstance(i, str) and i for i in use_case_ids):
        raise ValueError("ui_case needs one or more non-empty use case ids")
    return pytest.mark.ui_case(*use_case_ids)


def case_ids_for(node: Any) -> list[str]:
    """Use-case ids from every `ui_case` marker on a test, in order; [] when untagged."""
    return [str(i) for mark in node.iter_markers("ui_case") for i in mark.args]


def _report_path() -> Path | None:
    raw = os.environ.get(REPORT_ENV)
    return Path(raw) if raw else None


def write_report(path: Path | None = None) -> None:
    path = path or _report_path()
    if path is None:
        return
    report = UiLaneReport(list(_STATE["cases"]), _STATE["unavailable"])
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(report.to_dict(), indent=2))
    os.replace(tmp, path)


def reset() -> None:
    _STATE["cases"], _STATE["unavailable"] = [], ""


def mark_unavailable(reason: str) -> None:
    _STATE["unavailable"] = reason
    write_report()


def _snapshot(page: Any, case_id: str) -> tuple[str | None, str | None]:
    """Save a screenshot and the DOM to the artifacts dir. Evidence only; never compared."""
    raw = os.environ.get(ARTIFACTS_ENV)
    if not raw:
        return None, None
    folder = Path(raw)
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in case_id)[-120:]
    shot, dom = folder / f"{stem}.png", folder / f"{stem}.html"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shot))
        dom.write_text(page.content())
    except Exception:  # noqa: BLE001 - a closed or fake page just has no artifacts
        return (str(shot) if shot.exists() else None), (str(dom) if dom.exists() else None)
    return str(shot), str(dom)


def finish_case(case_id: str, use_case_ids: list[str], collector: ConsoleCollector, *,
                test_ok: bool, duration_s: float, page: Any = None,
                failure: str = "") -> UiCase:
    """Build the case record, add it to the report, and write the report if one is wanted."""
    errors = collector.errors()
    if not test_ok:
        errors = [f"test failed: {failure[:500] or 'assertion or exception in the test'}", *errors]
    console, network = collector._unexpected()
    shot, dom = _snapshot(page, case_id) if page is not None else (None, None)
    case = UiCase(
        id=case_id, use_case_ids=list(use_case_ids), ok=test_ok and not collector.errors(),
        duration_s=round(duration_s, 3), errors=errors if not test_ok or errors else [],
        console=console, page_errors=list(collector.page_errors), network_failures=network,
        screenshot=shot, dom=dom)
    _STATE["cases"].append(case)
    write_report()
    return case


@contextmanager
def observe(page: Any, case_id: str, use_case_ids: list[str],
            failed: Callable[[], str | bool] = lambda: False) -> Iterator[ConsoleCollector]:
    """Watch `page` for the body of one test; fail afterwards if the browser saw any error."""
    collector = ConsoleCollector(page)
    started = time.monotonic()
    body_error = ""
    try:
        yield collector
    except BaseException as exc:
        body_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        outside = failed()
        reason = body_error or (outside if isinstance(outside, str) else "")
        broken = bool(body_error or outside)
        case = finish_case(case_id, use_case_ids, collector, test_ok=not broken,
                           duration_s=time.monotonic() - started, page=page, failure=reason)
    if collector.errors() and not broken:
        raise AssertionError(f"{case.id} saw browser errors:\n  " + "\n  ".join(case.errors))


# ---------------------------------------------------------------- pytest plugin hooks


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "ui_case(*ids): the plan use-case ids this UI scenario covers")


def pytest_sessionstart(session: pytest.Session) -> None:
    reset()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    write_report()  # a run with no cases still leaves a report; the lane then names the gap


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Iterator[None]:
    outcome = yield
    setattr(item, f"rep_{call.when}", outcome.get_result())


def _unavailable(reason: str) -> None:
    mark_unavailable(reason)
    pytest.fail(f"UI lane unavailable: {reason}", pytrace=False)


@pytest.fixture(scope="session")
def ui_browser() -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        _unavailable(f"Playwright is not installed ({exc}); run `pip install playwright` and "
                     "`playwright install chromium`")
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001 - any launch failure means no usable browser
        playwright.stop()
        _unavailable(f"could not launch chromium ({str(exc).splitlines()[0] if str(exc) else exc}); "
                     "run `playwright install chromium`")
    try:
        yield browser
    finally:
        browser.close()
        playwright.stop()


@pytest.fixture
def ui_page(request: pytest.FixtureRequest, ui_browser: Any) -> Iterator[Any]:
    context = ui_browser.new_context(viewport=dict(VIEWPORT))
    page = context.new_page()

    def test_failed() -> str:
        rep = getattr(request.node, "rep_call", None)
        if rep is None or rep.passed:
            return ""
        return str(getattr(rep, "longreprtext", "") or "test failed")[-500:]

    try:
        with observe(page, request.node.nodeid, case_ids_for(request.node), test_failed) as col:
            request.node._ui_collector = col
            yield page
    finally:
        context.close()


@pytest.fixture
def ui_collector(request: pytest.FixtureRequest, ui_page: Any) -> ConsoleCollector:
    """The collector attached to `ui_page`, e.g. to call `expect_http_error`."""
    return request.node._ui_collector
