"""`devflow init`: tune the workflow to one repo. Idempotent; never clobbers edits."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import (
    PROMPTS,
    SCHEMAS,
    needs_input,
    pick_agents,
    read_schema,
    render_config,
    render_prompt,
)
from .detect import Profile, detect_profile, repo_root
from .graphify import GraphResult, build_graph

AGENT_BINARIES = {"claude": ("claude",), "codex": ("codex",), "cursor": ("agent", "cursor-agent")}
LOCAL_EXCLUDES =("graphify-out/", ".devflow/state/", ".venv/", "node_modules/")


class InitError(RuntimeError):
    pass


@dataclass
class InitResult:
    root: Path
    profile: Profile
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    needs_input: list[str] = field(default_factory=list)
    graph: GraphResult | None = None


def _write(root: Path, rel: str, content: str, force: bool, result: InitResult) -> None:
    path = root / rel
    if path.exists() and not force:
        result.skipped.append(rel)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    result.created.append(rel)


def _local_excludes(root: Path) -> None:
    """Keep generated artifacts out of `git status` without touching tracked files."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True, text=True, check=True, timeout=20,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return
    path = Path(out) if Path(out).is_absolute() else root / out
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text().splitlines() if path.exists() else []
    missing = [e for e in LOCAL_EXCLUDES if e not in existing]
    if missing:
        with path.open("a") as fh:
            fh.write("\n".join(missing) + "\n")


def run_init(
    path: Path,
    *,
    force: bool = False,
    use_graph: bool = True,
    graph_builder: Callable[[Path], GraphResult] = build_graph,
    which: Callable[[str], str | None] = shutil.which,
) -> InitResult:
    root = repo_root(path)
    if root is None:
        raise InitError(f"{path} is not inside a git repository (devflow needs git worktrees)")

    profile = detect_profile(root, have_uv=bool(which("uv")))
    result = InitResult(root=root, profile=profile)

    if use_graph:
        result.graph = graph_builder(root)
    available = [name for name, bins in AGENT_BINARIES.items() if any(which(b) for b in bins)]
    coder, reviewer, warnings = pick_agents(available)

    _write(root, ".devflow/config.toml",
           render_config(profile, coder, reviewer, result.graph, available), force, result)
    for name in PROMPTS:
        _write(root, f".devflow/prompts/{name}.md",
               render_prompt(name, profile, result.graph), force, result)
    for name in SCHEMAS:
        _write(root, f".devflow/schemas/{name}.schema.json", read_schema(name), force, result)

    _local_excludes(root)
    result.needs_input = needs_input(profile, warnings)
    if result.graph and not result.graph.ok:
        result.needs_input.append(result.graph.summary())
    return result
