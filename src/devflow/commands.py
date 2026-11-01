"""Human commands, read from GitHub comments.

`/approve`, `/answer <text>`, `/reject <why>` and `/resume` are the only way a person moves a
ticket. Comments are untrusted input: a command counts only if its author is an approver, and
each comment is consumed at most once.
"""

from __future__ import annotations

from dataclasses import dataclass

from .host import STATUS_MARKER, Comment

KINDS = ("approve", "answer", "reject", "resume", "learn")


@dataclass(frozen=True)
class Command:
    kind: str
    text: str
    comment_id: int
    author: str


def parse(comment: Comment) -> Command | None:
    """A command is a comment whose first non-empty line starts with `/<kind>`."""
    if comment.body.startswith(STATUS_MARKER):
        return None
    lines = [ln for ln in comment.body.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    head = lines[0].strip()
    if not head.startswith("/"):
        return None
    word, _, rest = head[1:].partition(" ")
    kind = word.lower()
    if kind not in KINDS:
        return None
    text = "\n".join([rest.strip(), *lines[1:]]).strip()
    return Command(kind, text, comment.id, comment.author)


def pending(
    comments: list[Comment], approvers: set[str], consumed: set[int], kinds: tuple[str, ...] = KINDS
) -> list[Command]:
    """Unconsumed commands of the wanted kinds from approvers, oldest first."""
    out = []
    for c in sorted(comments, key=lambda c: (c.created_at, c.id)):
        if c.id in consumed or c.author not in approvers:
            continue
        cmd = parse(c)
        if cmd and cmd.kind in kinds:
            out.append(cmd)
    return out
