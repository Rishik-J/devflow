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


@dataclass
class RoutingCfg:
    roles: tuple[str, ...] = ("coder", "reviewer", "test_planner")
    strategy: str = "headroom"
    cooldown_minutes: float = 30.0
    cooldown_max_hours: float = 6.0
    agents: dict[str, AgentBudget] = field(default_factory=dict)


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
        )
    return RoutingCfg(
        roles=tuple(roles),
        strategy=strategy,
        cooldown_minutes=_number(section, "cooldown_minutes", 30.0, "[routing]", minimum=0.1),
        cooldown_max_hours=_number(section, "cooldown_max_hours", 6.0, "[routing]", minimum=0.1),
        agents=budgets,
    )


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
        ),
        stage_timeout_minutes=_positive_int(limits, "stage_timeout_minutes", 45),
        gate_timeout_minutes=_positive_int(limits, "gate_timeout_minutes", 20),
        human_gates={k: bool(v) for k, v in data.get("human_gates", {}).items()
                     if isinstance(v, bool)},
        agents={k: str(v) for k, v in data.get("agents", {}).items()},
        projects=projects,
        github={k: str(v) for k, v in data.get("github", {}).items()},
        routing=_routing(data),
    )
