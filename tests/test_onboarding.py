"""The setup view reports readiness without modifying the repository."""

import subprocess
from pathlib import Path

from devflow.onboarding import collect_setup


def test_setup_reports_missing_connections_without_writing(tmp_path: Path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setattr("devflow.onboarding.shutil.which", lambda name: None)
    before = list(tmp_path.rglob("*"))
    result = collect_setup(tmp_path)
    assert result["ready"] is False
    assert {s["name"] for s in result["steps"]} >= {"GitHub", "OpenClaw", "Claude Code", "Codex", "Cursor", "Project queue"}
    assert all("command" in s for s in result["steps"])
    assert list(tmp_path.rglob("*")) == before
