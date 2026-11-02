"""Git operations the engine performs itself, so agents never commit, push or merge."""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

from . import telemetry

BOT = ("-c", "user.name=devflow", "-c", "user.email=devflow@localhost")
MAX_FILE_BYTES = 5 * 1024 * 1024
SENSITIVE = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*credentials*.json",
             "*.p12", "*.pfx", ".npmrc", ".pypirc", "secrets.*")


class GitError(RuntimeError):
    pass


class UnsafeCommit(GitError):
    """The change contains something an automated run must not commit. Needs a human."""


@telemetry.operation("tool.git")
def git(path: Path, *args: str, check: bool = True, timeout: int = 120) -> str:
    proc = subprocess.run(
        ["git", *BOT, "-C", str(path), *args], capture_output=True, text=True, timeout=timeout,
        check=False,
    )
    telemetry.current_content(output={"stdout": proc.stdout, "stderr": proc.stderr,
                                      "exit_code": proc.returncode})
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout


def changed_paths(path: Path) -> list[str]:
    out = git(path, "status", "--porcelain", "-z")
    paths = []
    for entry in out.split("\0"):
        if len(entry) > 3:
            paths.append(entry[3:])
    return paths


def unsafe_paths(path: Path, paths: list[str]) -> list[str]:
    """Files that must never be committed by an automated run: secrets and oversized blobs."""
    bad = []
    for rel in paths:
        name = Path(rel).name
        full = path / rel
        if any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE) and name != ".env.example":
            bad.append(f"{rel} (looks like a secret)")
        elif full.is_file() and full.stat().st_size > MAX_FILE_BYTES:
            bad.append(f"{rel} (over {MAX_FILE_BYTES // (1024 * 1024)} MB)")
    return bad


def commit_all(path: Path, message: str) -> str | None:
    """Commit every change in the worktree. Returns the new sha, or None if nothing changed.

    Raises GitError if a changed file looks like a secret or is too large: those need a human.
    """
    paths = changed_paths(path)
    if not paths:
        return None
    bad = unsafe_paths(path, paths)
    if bad:
        raise UnsafeCommit("refusing to commit: " + "; ".join(bad))
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)
    return git(path, "rev-parse", "HEAD").strip()


def head_sha(path: Path) -> str:
    return git(path, "rev-parse", "HEAD").strip()


def diff_names(path: Path, base: str) -> list[str]:
    return [ln for ln in git(path, "diff", "--name-only", f"{base}...HEAD").splitlines() if ln]


def diff_stat(path: Path, base: str) -> str:
    return git(path, "diff", "--stat", f"{base}...HEAD").strip()


def has_changes_since(path: Path, base: str) -> bool:
    return bool(diff_names(path, base))


def push(path: Path, branch: str) -> None:
    git(path, "push", "-u", "origin", branch, timeout=300)


def remote_head(path: Path, branch: str) -> str:
    """The commit the remote really has for `branch` (empty if it has none)."""
    out = git(path, "ls-remote", "origin", f"refs/heads/{branch}", timeout=120).strip()
    return out.split()[0] if out else ""


def push_and_verify(path: Path, branch: str) -> str:
    """Push, then confirm the remote branch is exactly our HEAD. Returns that commit."""
    push(path, branch)
    head, remote = head_sha(path), remote_head(path, branch)
    if remote != head:
        raise GitError(f"after pushing, origin/{branch} is {remote[:8] or 'missing'} "
                       f"but the local branch is {head[:8]}")
    return head
