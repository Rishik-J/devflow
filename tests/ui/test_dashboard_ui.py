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
    assert width == 375
