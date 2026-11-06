"""Deterministic browser scenarios for `devflow ui`.

Run: DEVFLOW_UI_LANE=1 python -m pytest tests/ui -p devflow.uirunner

No sleeps: every wait is an explicit locator or condition. Playwright is used through the
`ui_page` fixture only, so a missing browser reports "UI lane unavailable" instead of a
collection error.
"""

from devflow.uirunner import ui_case

CARD = '.item-card[data-issue="38"]'
OPEN = "#drawer-overlay.open"
LOADED = "document.getElementById('dr-title').textContent !== 'Loading…'"


@ui_case("UC1")
def test_board_renders_the_ticket_card(ui_page, ui_server):
    ui_page.goto(ui_server)
    ui_page.locator(CARD).wait_for(state="visible")
    assert "Ticket #38" in ui_page.locator(CARD).inner_text()
    assert ui_page.locator("#stat-working").inner_text() == "1"


@ui_case("UC2", "UC3")
def test_drawer_opens_and_survives_reload(ui_page, ui_server):
    ui_page.goto(ui_server)
    ui_page.locator(CARD).click()
    ui_page.locator(OPEN).wait_for()
    ui_page.wait_for_function(LOADED)
    assert ui_page.locator("#dr-title").inner_text() == "Add <b>thing</b>"
    assert ui_page.url.endswith("#ticket-38")
    ui_page.reload()
    ui_page.locator(OPEN).wait_for()
    ui_page.wait_for_function(LOADED)
    assert ui_page.locator("#dr-title").inner_text() == "Add <b>thing</b>"


@ui_case("UC3")
def test_deep_link_opens_the_drawer(ui_page, ui_server):
    ui_page.goto(ui_server + "/#ticket-38")
    ui_page.locator(OPEN).wait_for()
    ui_page.wait_for_function(LOADED)
    assert "38" in ui_page.locator("#dr-badges").inner_text()


@ui_case("UC4")
def test_unknown_ticket_shows_not_found_without_uncaught_errors(ui_page, ui_server, ui_collector):
    ui_collector.expect_http_error("/api/ticket/999", 404)
    ui_page.goto(ui_server + "/#ticket-999")
    ui_page.locator(OPEN).wait_for()
    ui_page.wait_for_function("document.getElementById('dr-body').textContent.includes('not found')")
    assert ui_collector.page_errors == []


@ui_case("UC5")
def test_narrow_viewport_drawer_fills_the_screen(ui_page, ui_server):
    ui_page.set_viewport_size({"width": 375, "height": 700})
    ui_page.goto(ui_server)
    ui_page.locator(CARD).click()
    ui_page.locator(OPEN).wait_for()
    width = ui_page.evaluate("document.getElementById('drawer').getBoundingClientRect().width")
    assert abs(width - 375) < 0.01


@ui_case("UC6")
def test_analytics_view_reads_local_metrics(ui_page, ui_server):
    ui_page.goto(ui_server + "/#analytics")
    ui_page.locator("#analytics-view").wait_for(state="visible")
    ui_page.locator('[data-view="analytics"]').wait_for(state="visible")
    assert ui_page.locator('[data-view="analytics"]').get_attribute("aria-current") == "page"
    ui_page.locator("#stage-panel").wait_for(state="visible")


@ui_case("UC7")
def test_setup_view_rechecks_readiness(ui_page, ui_server, monkeypatch):
    from devflow import onboarding

    monkeypatch.setattr(onboarding, "collect_setup", lambda root: {
        "repo": "sample", "ready": False,
        "steps": [{"name": "GitHub", "ready": False, "required": True,
                   "detail": "Authenticate gh", "command": "gh auth login"}],
    })
    ui_page.goto(ui_server + "/#setup")
    ui_page.locator("#setup-view").wait_for(state="visible")
    ui_page.get_by_text("gh auth login").wait_for(state="visible")
    ui_page.locator("#setup-refresh").click()
    ui_page.get_by_text("0 of 1 required checks ready for sample.").wait_for(state="visible")


@ui_case("UC8")
def test_state_endpoint_accepts_query_string(ui_page, ui_server):
    response = ui_page.goto(ui_server + "/api/state?source=board")
    assert response.status == 200
    assert "columns" in ui_page.locator("body").inner_text()


@ui_case("UC9")
def test_state_endpoint_accepts_cache_buster(ui_page, ui_server):
    response = ui_page.goto(ui_server + "/api/state?_=123&source=board")
    assert response.status == 200
    assert "columns" in ui_page.locator("body").inner_text()


@ui_case("UC10")
def test_board_loads_after_query_string_state_request(ui_page, ui_server):
    ui_page.route("**/api/state", lambda route: route.continue_(url=route.request.url + "?_=123"), times=1)
    ui_page.goto(ui_server)
    ui_page.locator(CARD).wait_for(state="visible")
