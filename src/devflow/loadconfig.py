"""Read and validate .devflow/config.toml.

Always load the config from the main checkout, never from a ticket worktree: a ticket branch can
edit its own copy, and gates must not be weakened by the code they are checking.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .detect import GATES
from .states import Limits


class ConfigError(ValueError):
    pass


@dataclass
class ProjectCfg:
    path: str
    kind: str
    gates: dict[str, list[str]] = field(default_factory=dict)
    setup: list[str] = field(default_factory=list)


@dataclass
class AgentBudget:
    """Local accounting for one coding agent. `limit_usd` of 0 means unlimited."""

    priority: int = 10
    window_hours: float = 5.0
    limit_usd: float = 0.0
    usd_per_mtok_in: float = 3.0
    usd_per_mtok_out: float = 15.0
    capabilities: tuple[str, ...] | None = None  # None = the driver default (see router)


@dataclass
class RoutingCfg:
    roles: tuple[str, ...] = ("coder", "reviewer", "test_planner", "ui_evaluator")
    strategy: str = "headroom"
    cooldown_minutes: float = 30.0
    cooldown_max_hours: float = 6.0
    agents: dict[str, AgentBudget] = field(default_factory=dict)


DEFAULT_STATUSES = {
    "backlog": "Backlog", "ready": "Ready", "in_progress": "In progress",
    "in_review": "In review", "done": "Done",
}


@dataclass
class BoardCfg:
    """The GitHub Project that holds the ticket queue. project = 0 means no board."""

    project: int = 0
    owner: str = ""
    label: str = "devflow"
    priority_field: str = "Priority"
    followups_per_ticket: int = 3
    max_active: int = 1
    statuses: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_STATUSES))

    @property
    def enabled(self) -> bool:
        return self.project > 0


@dataclass
class LearningCfg:
    """How lessons are kept. Retro runs only if an agent is configured for the `retro` role."""

    enabled: bool = True
    budget_chars: int = 4000
    promote_after: int = 2
    max_idle_runs: int = 40


@dataclass
class UiCfg:
    """The browser lane. `paths` None means the defaults in uilane.DEFAULT_UI_PATTERNS."""

    enabled: bool = False
    paths: list[str] | None = None
    command: str = ""
    timeout_minutes: float = 10.0


@dataclass
class Config:
    root: Path
    name: str
    default_branch: str
    remote: str
    worktree_root: Path
    limits: Limits
    stage_timeout_minutes: int
    gate_timeout_minutes: int
    human_gates: dict[str, bool]
    agents: dict[str, str]
    projects: list[ProjectCfg]
    github: dict[str, str]
    routing: RoutingCfg = field(default_factory=RoutingCfg)
    approvers: list[str] = field(default_factory=list)
    notify_command: str = ""
    require_integration: bool = False
    board: BoardCfg = field(default_factory=BoardCfg)
    learning: LearningCfg = field(default_factory=LearningCfg)
    ui: UiCfg = field(default_factory=UiCfg)


def config_path(root: Path) -> Path:
    return root / ".devflow" / "config.toml"


def _positive_int(section: dict, key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"[limits] {key} must be a positive integer, got {value!r}")
    return value


def _number(section: dict, key: str, default: float, where: str, *, minimum: float = 0.0) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < minimum:
        raise ConfigError(f"{where} {key} must be a number >= {minimum}, got {value!r}")
    return float(value)


def _str_list(section: dict, key: str) -> list[str]:
    value = section.get(key, [])
    if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        raise ConfigError(f"[human_gates] {key} must be a list of strings")
    return list(value)


CAPABILITIES = ("sockets", "browser", "network")


def _capabilities(entry: dict, where: str) -> tuple[str, ...] | None:
    if "capabilities" not in entry:
        return None
    value = entry["capabilities"]
    if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        raise ConfigError(f"{where} capabilities must be a list of strings")
    unknown = sorted(set(value) - set(CAPABILITIES))
    if unknown:
        raise ConfigError(f"{where} unknown capabilities {unknown}; choose from {list(CAPABILITIES)}")
    return tuple(value)


def _routing(data: dict) -> RoutingCfg:
    section = data.get("routing", {})
    strategy = section.get("strategy", "headroom")
    if strategy not in ("headroom", "priority"):
        raise ConfigError(f"[routing] strategy must be 'headroom' or 'priority', got {strategy!r}")
    roles = section.get("roles", list(RoutingCfg.roles))
    if not (isinstance(roles, list) and all(isinstance(r, str) for r in roles)):
        raise ConfigError("[routing] roles must be a list of role names")
    budgets: dict[str, AgentBudget] = {}
    for name, entry in section.get("agents", {}).items():
        where = f"[routing.agents.{name}]"
        budgets[name] = AgentBudget(
            priority=int(_number(entry, "priority", 10, where)),
            window_hours=_number(entry, "window_hours", 5.0, where, minimum=0.01),
            limit_usd=_number(entry, "limit_usd", 0.0, where),
            usd_per_mtok_in=_number(entry, "usd_per_mtok_in", 3.0, where),
            usd_per_mtok_out=_number(entry, "usd_per_mtok_out", 15.0, where),
            capabilities=_capabilities(entry, where),
        )
    return RoutingCfg(
        roles=tuple(roles),
        strategy=strategy,
        cooldown_minutes=_number(section, "cooldown_minutes", 30.0, "[routing]", minimum=0.1),
        cooldown_max_hours=_number(section, "cooldown_max_hours", 6.0, "[routing]", minimum=0.1),
        agents=budgets,
    )


def _board(data: dict) -> BoardCfg:
    section = data.get("github", {})
    statuses = dict(DEFAULT_STATUSES)
    custom = section.get("statuses", {})
    if not isinstance(custom, dict):
        raise ConfigError("[github.statuses] must be a table")
    unknown = set(custom) - set(DEFAULT_STATUSES)
    if unknown:
        raise ConfigError(f"[github.statuses] unknown keys {sorted(unknown)}; "
                          f"use {sorted(DEFAULT_STATUSES)}")
    statuses.update({k: str(v) for k, v in custom.items()})
    where = "[github]"
    return BoardCfg(
        project=int(_number(section, "project", 0, where)),
        owner=str(section.get("owner", "")),
        label=str(section.get("label", "devflow")),
        priority_field=str(section.get("priority_field", "Priority")),
        followups_per_ticket=int(_number(section, "followups_per_ticket", 3, where)),
        max_active=int(_number(section, "max_active", 1, where, minimum=1)),
        statuses=statuses,
    )


def _learning(data: dict) -> LearningCfg:
    section = data.get("learning", {})
    where = "[learning]"
    return LearningCfg(
        enabled=bool(section.get("enabled", True)),
        budget_chars=int(_number(section, "budget_chars", 4000, where, minimum=200)),
        promote_after=int(_number(section, "promote_after", 2, where, minimum=1)),
        max_idle_runs=int(_number(section, "max_idle_runs", 40, where, minimum=1)),
    )


UI_KEYS = {"enabled", "paths", "command", "timeout_minutes"}


def _ui(data: dict) -> UiCfg:
    section = data.get("ui")
    if section is None:
        return UiCfg()
    if not isinstance(section, dict):
        raise ConfigError("[ui] must be a table")
    unknown = set(section) - UI_KEYS
    if unknown:
        raise ConfigError(f"[ui] unknown keys {sorted(unknown)}; use {sorted(UI_KEYS)}")
    command = section.get("command", "")
    if not isinstance(command, str):
        raise ConfigError(f"[ui] command must be a string, got {command!r}")
    paths = section.get("paths")
    if paths is not None and not (
            isinstance(paths, list) and all(isinstance(p, str) and p for p in paths)):
        raise ConfigError("[ui] paths must be a list of non-empty glob strings")
    enabled = section.get("enabled", bool(command.strip()))
    if not isinstance(enabled, bool):
        raise ConfigError(f"[ui] enabled must be true or false, got {enabled!r}")
    timeout = section.get("timeout_minutes", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ConfigError(f"[ui] timeout_minutes must be a positive number, got {timeout!r}")
    return UiCfg(enabled=enabled, paths=list(paths) if paths is not None else None,
                 command=command.strip(), timeout_minutes=float(timeout))


def load_config(root: Path) -> Config:
    path = config_path(root)
    if not path.exists():
        raise ConfigError(f"{path} not found; run `devflow init` in {root} first")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

    repo = data.get("repo")
    if not isinstance(repo, dict) or not repo.get("name"):
        raise ConfigError(f"{path}: missing [repo] name")

    limits = data.get("limits", {})
    projects = []
    for entry in data.get("project", []):
        gates = entry.get("gates", {})
        unknown = set(gates) - set(GATES)
        if unknown:
            raise ConfigError(f"project {entry.get('path')!r}: unknown gates {sorted(unknown)}")
        for gate, cmds in gates.items():
            if not (isinstance(cmds, list) and all(isinstance(c, str) and c for c in cmds)):
                raise ConfigError(f"project {entry.get('path')!r}: {gate} must be a list of commands")
        setup = entry.get("setup", [])
        if not (isinstance(setup, list) and all(isinstance(c, str) and c for c in setup)):
            raise ConfigError(f"project {entry.get('path')!r}: setup must be a list of commands")
        projects.append(
            ProjectCfg(entry.get("path", "."), entry.get("kind", ""), dict(gates), list(setup))
        )

    worktree_root = Path(repo.get("worktree_root", f"~/.devflow/worktrees/{repo['name']}"))
    return Config(
        root=root,
        name=repo["name"],
        default_branch=repo.get("default_branch", "main"),
        remote=repo.get("remote", ""),
        worktree_root=worktree_root.expanduser(),
        limits=Limits(
            max_review_rounds=_positive_int(limits, "max_review_rounds", 3),
            max_implement_attempts=_positive_int(limits, "max_implement_attempts", 3),
            max_ticket_cost_usd=_number(limits, "max_ticket_cost_usd", 0.0, "[limits]"),
            max_ticket_tokens=int(_number(limits, "max_ticket_tokens", 0, "[limits]")),
        ),
        stage_timeout_minutes=_positive_int(limits, "stage_timeout_minutes", 45),
        gate_timeout_minutes=_positive_int(limits, "gate_timeout_minutes", 20),
        human_gates={k: bool(v) for k, v in data.get("human_gates", {}).items()
                     if isinstance(v, bool)},
        agents={k: str(v) for k, v in data.get("agents", {}).items()},
        projects=projects,
        github={k: str(v) for k, v in data.get("github", {}).items()
                if not isinstance(v, dict)},
        routing=_routing(data),
        approvers=_str_list(data.get("human_gates", {}), "approvers"),
        notify_command=str(data.get("human_gates", {}).get("notify_command", "")),
        require_integration=bool(data.get("review", {}).get("require_integration", False)),
        board=_board(data),
        learning=_learning(data),
        ui=_ui(data),
    )
