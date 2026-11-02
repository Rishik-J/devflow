"""Assemble a ready-to-run Engine from a repo's .devflow/config.toml."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .agents import make_agent
from .board import Board, DryRunBoard, GhBoard
from .engine import Engine
from .host import DryRunHost, GhHost, Host, repo_slug
from .loadconfig import Config, load_config
from .notify import make_notifier
from .router import build_router
from .store import Store, reset_dry_run_db, state_db_path

ENGINE_ROLES = ("planner", "triage", "retro", "reviewer")


def build_engine(
    root: Path,
    *,
    dry_run: bool = False,
    host: Host | None = None,
    board: Board | None = None,
    log: Callable[[str], None] = lambda message: None,
) -> tuple[Engine, Store, Config]:
    """Load config and wire up the store, host, router, agents and notifier.

    The caller owns the returned Store and should close it.
    """
    cfg = load_config(root)
    # A dry run starts from nothing every time and never touches the real ticket database.
    store = Store(reset_dry_run_db(root) if dry_run else state_db_path(root))
    real: Host = host or GhHost(repo_slug(cfg.remote))
    queue = board or make_board(cfg)
    if queue is not None and dry_run:
        queue = DryRunBoard(queue)
    agents = {role: make_agent(cfg.agents[role]) for role in ENGINE_ROLES
              if role in cfg.agents and (role != "reviewer" or
                                         cfg.agents[role].startswith("openclaw"))}
    engine = Engine(
        cfg, store, DryRunHost(real) if dry_run else real, build_router(cfg, store), agents,
        make_notifier(cfg.notify_command), board=queue, dry_run=dry_run, log=log,
    )
    return engine, store, cfg


def make_board(cfg: Config) -> Board | None:
    """The configured GitHub Project, or None when [github] project is not set."""
    if not cfg.board.enabled:
        return None
    slug = repo_slug(cfg.remote)
    return GhBoard(slug, cfg.board.owner or slug.split("/")[0], cfg.board.project,
                   priority_field=cfg.board.priority_field)
