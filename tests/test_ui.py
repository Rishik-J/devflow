"""Tests for devflow ui."""

from pathlib import Path
from devflow.ui import HTML_TEMPLATE, collect_state


def test_html_template_has_vital_elements():
    assert "Devflow Dashboard" in HTML_TEMPLATE
    assert "/api/state" in HTML_TEMPLATE
    assert "board-container" in HTML_TEMPLATE


def test_collect_state(tmp_path: Path):
    from devflow.loadconfig import Config, BoardCfg, Limits, RoutingCfg
    # Ensure collect_state gracefully handles empty/unconfigured repositories
    res = collect_state(Path("."))
    assert "columns" in res
    assert "repo" in res
    assert "Backlog" in res["columns"]
    assert "Ready" in res["columns"]
