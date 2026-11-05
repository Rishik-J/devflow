"""Read-only readiness checks for the local setup guide."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .graphctx import graph_path, is_fresh
from .loadconfig import ConfigError, load_config


def _ok(argv: list[str]) -> bool:
    try:
        return subprocess.run(argv, capture_output=True, timeout=5, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _has_openclaw_agent(binary: str | None) -> bool:
    if not binary:
        return False
    try:
        result = subprocess.run([binary, "agents", "list", "--json"], capture_output=True,
                                text=True, timeout=5, check=False)
        return result.returncode == 0 and any(
            isinstance(agent, dict) and agent.get("id") == "devflow"
            for agent in json.loads(result.stdout)
        )
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return False


def collect_setup(root: Path) -> dict[str, Any]:
    """Report setup without writing files, launching agents, or returning credentials."""
    root = root.resolve()
    config = root / ".devflow" / "config.toml"
    try:
        cfg = load_config(root) if config.exists() else None
    except ConfigError:
        cfg = None

    def step(name: str, ready: bool, detail: str, command: str, required: bool = True) -> dict[str, Any]:
        return {"name": name, "ready": ready, "required": required, "detail": detail, "command": command}

    gh = shutil.which("gh")
    claude = shutil.which("claude")
    codex = shutil.which("codex")
    cursor = shutil.which("agent") or shutil.which("cursor-agent")
    openclaw = shutil.which("openclaw")
    graphify = shutil.which("graphify")
    origin = _ok(["git", "-C", str(root), "remote", "get-url", "origin"])
    project = bool(cfg and cfg.board.enabled and cfg.board.project)
    claude_ready = bool(claude and _ok([claude, "auth", "status"]))
    codex_ready = bool(codex and _ok([codex, "login", "status"]))
    cursor_ready = bool(cursor and _ok([cursor, "status"]))
    agent_ready = claude_ready or codex_ready or cursor_ready
    openclaw_ready = _has_openclaw_agent(openclaw)
    graph_exists = graph_path(root).exists()
    steps = [
        step("Repository", origin, "Git origin configured" if origin else "Add a GitHub origin remote", "git remote -v"),
        step("GitHub", bool(gh and _ok([gh, "auth", "status"])),
             "GitHub CLI authenticated" if gh and _ok([gh, "auth", "status"]) else "Install and authenticate gh", "gh auth login"),
        step("Devflow files", config.exists() and cfg is not None,
             "Configuration loads" if cfg else "Initialize or repair repository configuration", "devflow init"),
        step("Claude Code", claude_ready, "Authenticated" if claude_ready else "Install and sign in with Claude Code",
             "claude auth login", required=False),
        step("Codex", codex_ready, "Authenticated" if codex_ready else "Install and sign in with Codex",
             "codex login", required=False),
        step("Cursor", cursor_ready, "Authenticated" if cursor_ready else "Install and sign in with Cursor Agent",
             "agent login", required=False),
        step("Coding agent", agent_ready, "At least one coding CLI is connected" if agent_ready else "Connect Claude Code, Codex, or Cursor",
             "devflow doctor"),
        step("OpenClaw", openclaw_ready, "Devflow planner agent exists; verify gateway separately" if openclaw_ready else "Start OpenClaw and create the devflow planner",
             "devflow setup-openclaw --model YOUR_PROVIDER/YOUR_MODEL"),
        step("Graphify", bool(graphify and graph_exists and is_fresh(root)),
             "Local graph fresh" if graphify and graph_exists and is_fresh(root) else "Install Graphify and refresh local context",
             "devflow graph refresh" if graphify else "uv tool install graphifyy && devflow graph refresh"),
        step("Project queue", project, f"Project #{cfg.board.project} configured" if project else "Create or connect a GitHub Project",
             "devflow board-create"),
    ]
    return {"repo": root.name, "steps": steps,
            "ready": all(s["ready"] for s in steps if s["required"])}
