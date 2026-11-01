"""The repo context pack every agent starts from.

`.devflow/context/repo-brief.md` is written once by `devflow init` and is meant to be edited: it
says what the repo is, how it is built and tested, and which files to read first. Lessons from
earlier tickets are added to each request separately (see learn/ledger.py).
"""

from __future__ import annotations

from pathlib import Path

from .config import _gates_summary
from .detect import Profile
from .graphify import GraphResult

BRIEF_LIMIT = 3500


def brief_path(root: Path) -> Path:
    return root / ".devflow" / "context" / "repo-brief.md"


def ledger_path(root: Path) -> Path:
    return root / ".devflow" / "learnings" / "ledger.jsonl"


def render_repo_brief(profile: Profile, graph: GraphResult | None) -> str:
    docs = "\n".join(f"- `{c}`" for c in profile.conventions) or "- (none found)"
    graph_line = graph.summary() if graph else "Code graph: not built."
    remote = profile.remote or "(no remote)"
    return (
        f"# {profile.name}: repository brief\n\n"
        "Written by `devflow init`; edit it freely. Every agent working on a ticket reads this.\n\n"
        f"- Remote: {remote}\n- Default branch: `{profile.default_branch or 'main'}`\n"
        f"- Layout: {'monorepo, ' + str(len(profile.projects)) + ' projects' if profile.monorepo else 'single project'}\n\n"
        f"## How it is built and tested\n{_gates_summary(profile)}\n\n"
        f"## Read these first\n{docs}\n\n"
        f"## Code graph\n{graph_line}\n\n"
        "## Notes for agents\n"
        "- Keep changes inside the ticket's scope; list adjacent problems as follow-ups.\n"
        "- Do not weaken, skip or delete tests to make a gate pass.\n"
    )


def load_brief(root: Path) -> str:
    path = brief_path(root)
    if not path.exists():
        return ""
    text = path.read_text().strip()
    return text[:BRIEF_LIMIT] + ("\n... (brief truncated)" if len(text) > BRIEF_LIMIT else "")
