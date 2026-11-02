"""The ticket queue: a GitHub Project (v2) whose Status column drives what gets worked.

Statuses are Backlog, Ready, In progress, In review, Done (names configurable). People move a
ticket to Ready; devflow moves it through In progress and In review to Done, and files new
tickets it discovers into Backlog, never Ready.

`GhBoard` uses the `gh project` commands (needs the `project` token scope:
`gh auth refresh -s project`). `FakeBoard` is the in-memory version for tests.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from . import telemetry
from .states import State


class BoardError(RuntimeError):
    pass


@dataclass
class BoardItem:
    issue: int
    status: str
    item_id: str = ""
    priority: str = ""
    title: str = ""
    labels: list[str] = field(default_factory=list)


class Board(Protocol):
    def items(self) -> list[BoardItem]: ...
    def set_status(self, issue: int, status: str) -> None: ...
    def add_issue(self, issue: int, url: str, status: str) -> None: ...


# How the engine's states show up on the board (keys of BoardCfg.statuses).
STATUS_FOR_STATE = {
    State.NEW: "in_progress", State.REFINING: "in_progress",
    State.AWAITING_CLARIFICATION: "in_progress", State.AWAITING_PLAN_APPROVAL: "in_progress",
    State.TEST_DESIGN: "in_progress", State.IMPLEMENTING: "in_progress",
    State.GATES: "in_progress", State.CHANGES_REQUESTED: "in_progress",
    State.ESCALATED: "in_progress", State.IN_REVIEW: "in_review",
    State.AWAITING_MERGE_APPROVAL: "in_review", State.MERGED: "done", State.CANCELLED: "backlog",
}


def status_for(state: State, statuses: dict[str, str]) -> str:
    return statuses[STATUS_FOR_STATE[state]]


Runner = Callable[..., subprocess.CompletedProcess]


def _run(cmd: list[str], input_text: str | None = None):
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=120,
                          check=False)


class GhBoard:
    def __init__(self, slug: str, owner: str, number: int, *, status_field: str = "Status",
                 priority_field: str = "Priority", runner: Runner = _run):
        self.slug, self.owner, self.number = slug, owner, number
        self.status_field, self.priority_field = status_field, priority_field
        self._run = runner
        self._meta: dict | None = None

    @telemetry.operation("tool.github_project")
    def _gh(self, *args: str) -> dict:
        proc = self._run(["gh", "project", *args])
        telemetry.current_content(output={"stdout": proc.stdout, "stderr": proc.stderr,
                                          "exit_code": proc.returncode})
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            hint = " (run `gh auth refresh -s project`)" if "scope" in err.lower() else ""
            raise BoardError(f"gh project {args[0]}: {err[:300]}{hint}")
        return json.loads(proc.stdout or "{}")

    def meta(self) -> dict:
        """Project id, the Status field id, and its option ids by name. Fetched once."""
        if self._meta is None:
            view = self._gh("view", str(self.number), "--owner", self.owner, "--format", "json")
            fields = self._gh("field-list", str(self.number), "--owner", self.owner,
                              "--format", "json", "--limit", "100").get("fields", [])
            status = next((f for f in fields if f.get("name") == self.status_field), None)
            if status is None:
                raise BoardError(f"project #{self.number} has no {self.status_field!r} field")
            self._meta = {
                "project_id": view["id"], "field_id": status["id"],
                "options": {o["name"]: o["id"] for o in status.get("options", [])},
            }
        return self._meta

    def items(self) -> list[BoardItem]:
        data = self._gh("item-list", str(self.number), "--owner", self.owner, "--format", "json",
                        "--limit", "500")
        out = []
        for raw in data.get("items", []):
            content = raw.get("content") or {}
            if content.get("type") != "Issue" or content.get("repository") != self.slug:
                continue
            out.append(BoardItem(
                issue=int(content["number"]), item_id=raw["id"],
                status=str(raw.get("status") or ""),
                priority=str(raw.get(self.priority_field.lower()) or ""),
                title=str(content.get("title") or raw.get("title") or ""),
                labels=list(raw.get("labels") or []),
            ))
        return out

    def _option(self, status: str) -> str:
        options = self.meta()["options"]
        if status not in options:
            raise BoardError(f"the Status field has no option {status!r}; it has "
                             f"{sorted(options)}")
        return options[status]

    def _edit(self, item_id: str, status: str) -> None:
        meta = self.meta()
        self._gh("item-edit", "--id", item_id, "--project-id", meta["project_id"],
                 "--field-id", meta["field_id"], "--single-select-option-id", self._option(status))

    def set_status(self, issue: int, status: str) -> None:
        item = next((i for i in self.items() if i.issue == issue), None)
        if item is None:
            raise BoardError(f"issue #{issue} is not on project #{self.number}")
        self._edit(item.item_id, status)

    def add_issue(self, issue: int, url: str, status: str) -> None:
        added = self._gh("item-add", str(self.number), "--owner", self.owner, "--url", url,
                         "--format", "json")
        self._edit(added["id"], status)


class FakeBoard:
    def __init__(self, statuses: tuple[str, ...] = ("Backlog", "Ready", "In progress",
                                                      "In review", "Done")):
        self.statuses = statuses
        self._items: dict[int, BoardItem] = {}
        self.history: list[tuple[int, str]] = []

    def put(self, issue: int, status: str, *, priority: str = "", title: str = "",
            labels: list[str] | None = None) -> None:
        self._items[issue] = BoardItem(issue, status, f"PVTI_{issue}", priority, title,
                                       list(labels or ["devflow"]))

    def items(self) -> list[BoardItem]:
        return [BoardItem(**vars(i)) for i in self._items.values()]

    def status_of(self, issue: int) -> str | None:
        return self._items[issue].status if issue in self._items else None

    def _check(self, status: str) -> None:
        if status not in self.statuses:
            raise BoardError(f"the Status field has no option {status!r}")

    def set_status(self, issue: int, status: str) -> None:
        self._check(status)
        if issue not in self._items:
            raise BoardError(f"issue #{issue} is not on the project")
        self._items[issue].status = status
        self.history.append((issue, status))

    def add_issue(self, issue: int, url: str, status: str) -> None:
        self._check(status)
        self.put(issue, status)
        self.history.append((issue, status))


class DryRunBoard:
    """Reads the real board, logs writes instead of performing them."""

    def __init__(self, inner: Board):
        self.inner, self.writes = inner, []

    def items(self) -> list[BoardItem]:
        return self.inner.items()

    def set_status(self, issue: int, status: str) -> None:
        self.writes.append(f"board: #{issue} -> {status}")

    def add_issue(self, issue: int, url: str, status: str) -> None:
        self.writes.append(f"board: add #{issue} as {status}")
