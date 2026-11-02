"""The code host (GitHub): issues, comments, pull requests, checks, merge.

The engine talks to a `Host`, never to `gh` directly, so every GitHub write goes through one
place that can be dry-run, faked in tests, or swapped. `GhHost` shells out to the `gh` CLI.
Comments are the human interface: the engine reads commands from them (see commands.py) and
keeps one editable status comment per issue.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from . import telemetry

STATUS_MARKER = "<!-- devflow:status -->"


class HostError(RuntimeError):
    pass


@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    state: str = "open"
    author: str = ""


@dataclass
class Comment:
    id: int
    author: str
    body: str
    created_at: float = 0.0


@dataclass
class PullRequest:
    number: int
    url: str
    head: str
    base: str
    state: str = "open"  # open | merged | closed


class Host(Protocol):
    def user(self) -> str: ...
    def get_issue(self, number: int) -> Issue: ...
    def comments(self, number: int) -> list[Comment]: ...
    def comment(self, number: int, body: str) -> int: ...
    def update_comment(self, comment_id: int, body: str) -> None: ...
    def create_pr(self, head: str, base: str, title: str, body: str) -> PullRequest: ...
    def get_pr(self, number: int) -> PullRequest: ...
    def checks(self, pr_number: int) -> str: ...  # success | failure | pending | none
    def merge_pr(self, pr_number: int, expected_sha: str = "") -> None: ...
    def create_issue(self, title: str, body: str, labels: list[str]) -> Issue: ...
    def list_open_issues(self, label: str | None = None) -> list[Issue]: ...
    def ensure_label(self, name: str, color: str = "ededed", description: str = "") -> None: ...


def upsert_status(host: Host, number: int, body: str) -> int:
    """Create or update the single devflow status comment on an issue or PR."""
    marked = f"{STATUS_MARKER}\n{body}"
    me = host.user()
    for c in host.comments(number):
        if c.author == me and c.body.startswith(STATUS_MARKER):
            host.update_comment(c.id, marked)
            return c.id
    return host.comment(number, marked)


Runner = Callable[..., subprocess.CompletedProcess]


def _run(cmd: list[str], input_text: str | None = None, cwd: str | None = None):
    return subprocess.run(
        cmd, input=input_text, capture_output=True, text=True, timeout=120, check=False, cwd=cwd
    )


def repo_slug(remote: str) -> str:
    """owner/repo from an https or ssh remote URL."""
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", remote.strip())
    if not m:
        raise HostError(f"cannot parse a GitHub owner/repo from remote {remote!r}")
    return f"{m.group(1)}/{m.group(2)}"


class GhHost:
    """GitHub through the `gh` CLI. Needs `gh auth login` (and `project` scope for Projects)."""

    def __init__(self, slug: str, runner: Runner = _run):
        self.slug = slug
        self._run = runner
        self._user: str | None = None

    @telemetry.operation("tool.github")
    def _gh(self, *args: str, input_text: str | None = None) -> str:
        proc = self._run(["gh", *args], input_text)
        if proc.returncode != 0:
            raise HostError(f"gh {' '.join(args[:3])}: {(proc.stderr or proc.stdout).strip()[:300]}")
        return proc.stdout

    def user(self) -> str:
        if self._user is None:
            self._user = self._gh("api", "user", "--jq", ".login").strip()
        return self._user

    def get_issue(self, number: int) -> Issue:
        raw = json.loads(self._gh(
            "issue", "view", str(number), "-R", self.slug,
            "--json", "number,title,body,labels,state,author"))
        return Issue(
            number=raw["number"], title=raw["title"], body=raw.get("body") or "",
            labels=[label["name"] for label in raw.get("labels", [])],
            state=raw.get("state", "OPEN").lower(), author=(raw.get("author") or {}).get("login", ""),
        )

    def comments(self, number: int) -> list[Comment]:
        out = self._gh("api", f"repos/{self.slug}/issues/{number}/comments", "--paginate")
        # --paginate concatenates JSON arrays; parse them one by one
        decoder, text, pos, items = json.JSONDecoder(), out.strip(), 0, []
        while pos < len(text):
            value, end = decoder.raw_decode(text, pos)
            items.extend(value if isinstance(value, list) else [value])
            pos = end
            while pos < len(text) and text[pos].isspace():
                pos += 1
        return [
            Comment(
                id=c["id"], author=(c.get("user") or {}).get("login", ""), body=c.get("body") or "",
                created_at=datetime.fromisoformat(c["created_at"]).timestamp(),
            )
            for c in items
        ]

    def comment(self, number: int, body: str) -> int:
        out = self._gh("api", f"repos/{self.slug}/issues/{number}/comments", "-X", "POST",
                       "--input", "-", "--jq", ".id", input_text=json.dumps({"body": body}))
        return int(out.strip())

    def update_comment(self, comment_id: int, body: str) -> None:
        self._gh("api", f"repos/{self.slug}/issues/comments/{comment_id}", "-X", "PATCH",
                 "--input", "-", input_text=json.dumps({"body": body}))

    def create_pr(self, head: str, base: str, title: str, body: str) -> PullRequest:
        existing = self._gh("pr", "list", "-R", self.slug, "--head", head, "--state", "open",
                            "--json", "number,url,headRefName,baseRefName")
        found = json.loads(existing or "[]")
        if found:
            pr = found[0]
            return PullRequest(pr["number"], pr["url"], pr["headRefName"], pr["baseRefName"])
        url = self._gh("pr", "create", "-R", self.slug, "--head", head, "--base", base,
                       "--title", title, "--body-file", "-", input_text=body).strip()
        return self.get_pr(int(url.rstrip("/").rsplit("/", 1)[1]))

    def get_pr(self, number: int) -> PullRequest:
        raw = json.loads(self._gh("pr", "view", str(number), "-R", self.slug, "--json",
                                  "number,url,headRefName,baseRefName,state,mergedAt"))
        state = "merged" if raw.get("mergedAt") else raw.get("state", "OPEN").lower()
        return PullRequest(raw["number"], raw["url"], raw["headRefName"], raw["baseRefName"], state)

    def checks(self, pr_number: int) -> str:
        raw = json.loads(self._gh("pr", "view", str(pr_number), "-R", self.slug,
                                  "--json", "statusCheckRollup"))
        rollup = raw.get("statusCheckRollup") or []
        if not rollup:
            return "none"
        states = {(c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
                  for c in rollup}
        if states & {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
            return "failure"
        if states - {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            return "pending"
        return "success"

    def merge_pr(self, pr_number: int, expected_sha: str = "") -> None:
        args = ["pr", "merge", str(pr_number), "-R", self.slug, "--squash"]
        if expected_sha:
            # GitHub refuses the merge if the branch moved since this commit was reviewed
            args += ["--match-head-commit", expected_sha]
        self._gh(*args)

    def create_issue(self, title: str, body: str, labels: list[str]) -> Issue:
        args = ["issue", "create", "-R", self.slug, "--title", title, "--body-file", "-"]
        for label in labels:
            args += ["--label", label]
        url = self._gh(*args, input_text=body).strip().splitlines()[-1]
        return self.get_issue(int(url.rstrip("/").rsplit("/", 1)[1]))

    def list_open_issues(self, label: str | None = None) -> list[Issue]:
        args = ["issue", "list", "-R", self.slug, "--state", "open", "--limit", "200",
                "--json", "number,title,body,labels,state,author"]
        if label:
            args += ["--label", label]
        return [
            Issue(number=raw["number"], title=raw["title"], body=raw.get("body") or "",
                  labels=[lb["name"] for lb in raw.get("labels", [])], state="open",
                  author=(raw.get("author") or {}).get("login", ""))
            for raw in json.loads(self._gh(*args) or "[]")
        ]

    def ensure_label(self, name: str, color: str = "ededed", description: str = "") -> None:
        self._gh("label", "create", name, "-R", self.slug, "--color", color,
                 "--description", description, "--force")


class DryRunHost:
    """Reads from a real host, but logs every write instead of performing it."""

    def __init__(self, inner: Host):
        self.inner = inner
        self.writes: list[str] = []
        self._next = 9_000_000

    def user(self) -> str:
        return self.inner.user()

    def get_issue(self, number: int) -> Issue:
        return self.inner.get_issue(number)

    def comments(self, number: int) -> list[Comment]:
        return self.inner.comments(number)

    def comment(self, number: int, body: str) -> int:
        self.writes.append(f"comment on #{number}: {body[:80]!r}")
        self._next += 1
        return self._next

    def update_comment(self, comment_id: int, body: str) -> None:
        self.writes.append(f"update comment {comment_id}: {body[:80]!r}")

    def create_pr(self, head: str, base: str, title: str, body: str) -> PullRequest:
        self.writes.append(f"create PR {head} -> {base}: {title!r}")
        return PullRequest(0, "(dry-run)", head, base)

    def get_pr(self, number: int) -> PullRequest:
        return PullRequest(number, "(dry-run)", "", "")

    def checks(self, pr_number: int) -> str:
        return "none"

    def merge_pr(self, pr_number: int, expected_sha: str = "") -> None:
        self.writes.append(f"merge PR #{pr_number} at {expected_sha[:8] or 'head'}")

    def create_issue(self, title: str, body: str, labels: list[str]) -> Issue:
        self.writes.append(f"create issue {title!r} labels={labels}")
        return Issue(0, title, body, list(labels))

    def list_open_issues(self, label: str | None = None) -> list[Issue]:
        return self.inner.list_open_issues(label)

    def ensure_label(self, name: str, color: str = "ededed", description: str = "") -> None:
        self.writes.append(f"ensure label {name!r}")


class FakeHost:
    """An in-memory host for tests. Humans are simulated with `say`."""

    def __init__(self, me: str = "devflow-bot"):
        self.me = me
        self.issues: dict[int, Issue] = {}
        self._comments: dict[int, list[Comment]] = {}
        self.prs: dict[int, PullRequest] = {}
        self.pr_checks: dict[int, str] = {}
        self.merged: list[int] = []
        self._ids = 0
        self._clock = 1_000.0

    def add_issue(self, number: int, title: str, body: str = "", **kw) -> Issue:
        self.issues[number] = Issue(number, title, body, **kw)
        return self.issues[number]

    def say(self, number: int, author: str, body: str) -> int:
        return self._add(number, author, body)

    def _add(self, number: int, author: str, body: str) -> int:
        self._ids += 1
        self._clock += 1
        self._comments.setdefault(number, []).append(Comment(self._ids, author, body, self._clock))
        return self._ids

    def user(self) -> str:
        return self.me

    def get_issue(self, number: int) -> Issue:
        if number not in self.issues:
            raise HostError(f"no issue #{number}")
        return self.issues[number]

    def comments(self, number: int) -> list[Comment]:
        return list(self._comments.get(number, []))

    def comment(self, number: int, body: str) -> int:
        return self._add(number, self.me, body)

    def update_comment(self, comment_id: int, body: str) -> None:
        for items in self._comments.values():
            for c in items:
                if c.id == comment_id:
                    c.body = body
                    return
        raise HostError(f"no comment {comment_id}")

    def create_pr(self, head: str, base: str, title: str, body: str) -> PullRequest:
        for pr in self.prs.values():
            if pr.head == head and pr.state == "open":
                return pr
        number = 1000 + len(self.prs) + 1
        self.prs[number] = PullRequest(number, f"https://example.test/pull/{number}", head, base)
        return self.prs[number]

    def get_pr(self, number: int) -> PullRequest:
        return self.prs[number]

    def checks(self, pr_number: int) -> str:
        return self.pr_checks.get(pr_number, "none")

    def merge_pr(self, pr_number: int, expected_sha: str = "") -> None:
        self.prs[pr_number].state = "merged"
        self.merged.append(pr_number)
        self.merged_at = getattr(self, "merged_at", {}) | {pr_number: expected_sha}

    def create_issue(self, title: str, body: str, labels: list[str]) -> Issue:
        number = max([99, *self.issues]) + 1
        self.issues[number] = Issue(number, title, body, list(labels))
        return self.issues[number]

    def list_open_issues(self, label: str | None = None) -> list[Issue]:
        return [i for i in self.issues.values()
                if i.state == "open" and (label is None or label in i.labels)]

    def ensure_label(self, name: str, color: str = "ededed", description: str = "") -> None:
        self.labels = getattr(self, "labels", set()) | {name}
