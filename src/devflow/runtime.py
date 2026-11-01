"""Assemble a ready-to-run Engine from a repo's .devflow/config.toml."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .agents import make_agent
from .engine import Engine
from .host import DryRunHost, GhHost, Host, repo_slug
from .loadconfig import Config, load_config
from .notify import make_notifier
from .router import build_router
from .store import Store, state_db_path

ENGINE_ROLES = ("planner", "triage")


def build_engine(
    root: Path,
    *,
    dry_run: bool = False,
    host: Host | None = None,
    log: Callable[[str], None] = lambda message: None,
) -> tuple[Engine, Store, Config]:
    """Load config and wire up the store, host, router, agents and notifier.

    The caller owns the returned Store and should close it.
    """
    cfg = load_config(root)
    store = Store(state_db_path(root))
    real: Host = host or GhHost(repo_slug(cfg.remote))
    agents = {role: make_agent(cfg.agents[role]) for role in ENGINE_ROLES if role in cfg.agents}
    engine = Engine(
        cfg, store, DryRunHost(real) if dry_run else real, build_router(cfg, store), agents,
        make_notifier(cfg.notify_command), dry_run=dry_run, log=log,
    )
    return engine, store, cfg
