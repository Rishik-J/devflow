"""`devflow board create`: make the GitHub Project a repo's queue needs, once.

Creates a private Project, gives its Status field the five options devflow drives, adds a
Priority field, links the Project to the repo, and writes the `[github]` block into the repo's
config. The Status options are replaced through GraphQL because `gh` cannot edit them.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .board import BoardError
from .loadconfig import DEFAULT_STATUSES, config_path

STATUS_OPTIONS = [
    ("Backlog", "GRAY", "Not yet approved for work"),
    ("Ready", "GREEN", "Approved: devflow may pick this up"),
    ("In progress", "YELLOW", "devflow is working on it"),
    ("In review", "PURPLE", "Reviewed or awaiting your merge approval"),
    ("Done", "BLUE", "Merged"),
]
PRIORITIES = "P0,P1,P2,P3"
UPDATE_STATUS = (
    "mutation($fieldId: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {"
    " updateProjectV2Field(input: {fieldId: $fieldId, singleSelectOptions: $options}) {"
    " projectV2Field { ... on ProjectV2SingleSelectField { id name options { id name } } } } }"
)

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]


def _run(cmd: list[str], input_text: str | None = None):
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=120,
                          check=False)


@dataclass
class Created:
    number: int
    url: str
    owner: str


def _gh(run: Runner, *args: str, input_text: str | None = None) -> str:
    proc = run(["gh", *args], input_text)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        hint = " (run `gh auth refresh -h github.com -s project`)" if "scope" in err.lower() else ""
        raise BoardError(f"gh {' '.join(args[:2])}: {err[:300]}{hint}")
    return proc.stdout


def create_project(owner: str, title: str, repo_slug: str, run: Runner = _run) -> Created:
    made = json.loads(_gh(run, "project", "create", "--owner", owner, "--title", title,
                          "--format", "json"))
    number = int(made["number"])
    fields = json.loads(_gh(run, "project", "field-list", str(number), "--owner", owner,
                            "--format", "json")).get("fields", [])
    status = next((f for f in fields if f.get("name") == "Status"), None)
    if status is None:
        raise BoardError(f"the new project #{number} has no Status field")
    payload = {"query": UPDATE_STATUS, "variables": {
        "fieldId": status["id"],
        "options": [{"name": n, "color": c, "description": d} for n, c, d in STATUS_OPTIONS]}}
    _gh(run, "api", "graphql", "--input", "-", input_text=json.dumps(payload))
    _gh(run, "project", "field-create", str(number), "--owner", owner, "--name", "Priority",
        "--data-type", "SINGLE_SELECT", "--single-select-options", PRIORITIES, "--format", "json")
    _gh(run, "project", "link", str(number), "--owner", owner, "--repo", repo_slug)
    return Created(number, made.get("url", ""), owner)


def github_block(number: int, owner: str) -> str:
    return (
        "# The GitHub Project that holds the queue.\n[github]\n"
        f"project = {number}\nowner = {json.dumps(owner)}\nlabel = \"devflow\"\n"
        "followups_per_ticket = 3\nmax_active = 1\n\n"
    )


def write_config(root: Path, number: int, owner: str) -> None:
    path = config_path(root)
    text = path.read_text()
    if "\n[github]" in text or text.startswith("[github]"):
        raise BoardError(f"{path} already has a [github] section; edit it by hand")
    if "[agents]" not in text:
        raise BoardError(f"{path} has no [agents] section to place [github] before")
    path.write_text(text.replace("[agents]", github_block(number, owner) + "[agents]", 1))


def required_statuses() -> list[str]:
    return list(DEFAULT_STATUSES.values())
