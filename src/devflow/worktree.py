"""One git worktree per ticket, so agents never touch the main checkout.

Branches are always `devflow/<issue>-<slug>` and worktrees always live under the configured
worktree_root. Creation is idempotent so a crashed run can resume.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

BRANCH_PREFIX = "devflow/"


class WorktreeError(RuntimeError):
    pass


@dataclass
class Worktree:
    path: Path
    branch: str
    base_sha: str


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120, check=False
    )
    if check and proc.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].strip("-") or "ticket"


def branch_name(issue: int | str, title: str) -> str:
    return f"{BRANCH_PREFIX}{issue}-{slugify(title)}"


def _has_ref(root: Path, ref: str) -> bool:
    return _git(root, "rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0


def base_ref(root: Path, default_branch: str) -> str:
    remote = f"origin/{default_branch}"
    return remote if _has_ref(root, remote) else default_branch


def create(
    root: Path, worktree_root: Path, issue: int | str, title: str, default_branch: str,
    *, fetch: bool = True,
) -> Worktree:
    """Create (or reuse) the worktree for a ticket, branched from the latest default branch."""
    branch = branch_name(issue, title)
    path = worktree_root / branch.removeprefix(BRANCH_PREFIX)
    resolved_root = worktree_root.resolve()
    if resolved_root not in path.resolve().parents:
        raise WorktreeError(f"{path} is outside {worktree_root}")

    if fetch and _git(root, "remote", check=False).stdout.strip():
        _git(root, "fetch", "--quiet", "origin", default_branch, check=False)
    base = base_ref(root, default_branch)
    if not _has_ref(root, base):
        raise WorktreeError(f"base ref {base!r} does not exist (does the repo have commits?)")
    base_sha = _git(root, "rev-parse", base).stdout.strip()

    if path.exists():
        current = _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if current != branch:
            raise WorktreeError(f"{path} exists on branch {current!r}, expected {branch!r}")
        return Worktree(path, branch, _merge_base(path, base) or base_sha)

    worktree_root.mkdir(parents=True, exist_ok=True)
    if _has_ref(root, f"refs/heads/{branch}"):
        _git(root, "worktree", "add", str(path), branch)
        return Worktree(path, branch, _merge_base(path, base) or base_sha)
    _git(root, "worktree", "add", "-b", branch, str(path), base)
    return Worktree(path, branch, base_sha)


def _merge_base(path: Path, base: str) -> str:
    proc = _git(path, "merge-base", base, "HEAD", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def create_detached(root: Path, path: Path, sha: str) -> Path:
    """A clean, detached checkout of exactly `sha` (for baselines and independent review).

    Reused if it exists: it is hard-reset to `sha` and any leftover files are removed, so gates
    always run against a pristine copy of that commit.
    """
    # `rev-parse --verify` accepts any well-formed 40-hex string; cat-file really looks it up
    if _git(root, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
        raise WorktreeError(f"commit {sha!r} does not exist in {root}")
    if path.exists():
        _git(path, "checkout", "--detach", "--force", sha)
        _git(path, "clean", "-fdq", "-e", ".venv", "-e", "node_modules")
        _git(path, "reset", "--hard", "-q", sha)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "--detach", str(path), sha)
    return path


def head_sha(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def remove(root: Path, path: Path, *, delete_branch: bool = False) -> None:
    """Remove a worktree. Only branches in the devflow/ namespace can be deleted."""
    branch = ""
    if path.exists():
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
        _git(root, "worktree", "remove", "--force", str(path))
    _git(root, "worktree", "prune", check=False)
    if delete_branch and branch.startswith(BRANCH_PREFIX):
        _git(root, "branch", "-D", branch, check=False)


def list_worktrees(root: Path) -> list[Path]:
    out = _git(root, "worktree", "list", "--porcelain").stdout
    return [Path(line.split(" ", 1)[1]) for line in out.splitlines() if line.startswith("worktree ")]
