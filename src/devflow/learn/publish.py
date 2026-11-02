"""Publish the local context as an ordinary reviewable PR from an isolated checkout."""
from __future__ import annotations

import hashlib

from .. import gitops, worktree
from ..host import Host
from ..loadconfig import Config

ALLOWED = (".devflow/context/repo-brief.md", ".devflow/learnings/ledger.jsonl")


def publish(cfg: Config, host: Host):
    for rel in ALLOWED:
        source = cfg.root / rel
        if any(part.is_symlink() for part in (source, *source.parents)):
            raise ValueError(f"refusing symlink in context: {rel}")
    files = {p: (cfg.root / p).read_bytes() for p in ALLOWED if (cfg.root / p).is_file()}
    if not files:
        raise ValueError("no context to publish; initialise the repo or capture lessons first")
    fingerprint = hashlib.sha256(b"".join(p.encode() + b"\0" + files[p]
                                         for p in sorted(files))).hexdigest()[:12]
    title = f"Update repository context ({fingerprint})"
    wt = worktree.create(cfg.root, cfg.worktree_root, "context", fingerprint,
                         cfg.default_branch)
    try:
        for rel, content in files.items():
            target = wt.path / rel
            if target.is_symlink():
                raise ValueError(f"refusing symlink in context: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        changed = gitops.git(wt.path, "status", "--porcelain", "--untracked-files=all").splitlines()
        changed = [line[3:] for line in changed]
        if any(p not in ALLOWED for p in changed):
            raise ValueError("context worktree contains unexpected changes")
        sha = gitops.commit_all(wt.path, title)
        if sha is None and not gitops.git(wt.path, "diff", "--name-only", f"{wt.base_sha}...HEAD").strip():
            return None
        gitops.push_and_verify(wt.path, wt.branch)
        body = ("Update the repository brief and lesson ledger from completed devflow runs.\n\n"
                "Review candidate lessons and their ticket evidence before accepting them. "
                "Runtime databases, transcripts, credentials and graph caches are excluded.\n\n"
                f"Context snapshot: `{fingerprint}`. This PR requires human review.")
        return host.create_pr(wt.branch, cfg.default_branch, title, body)
    finally:
        worktree.remove(cfg.root, wt.path)
