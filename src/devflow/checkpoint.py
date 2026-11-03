"""Persistent, repo-local three-merge pause for an autonomous queue."""
from __future__ import annotations

import json
import os
from pathlib import Path


def path(root: Path) -> Path:
    return root / ".devflow" / "state" / "checkpoint.json"


def read(root: Path) -> dict:
    try:
        data = json.loads(path(root).read_text())
        if isinstance(data, dict):
            result = {"pending": bool(data.get("pending")),
                      "completed": list(data.get("completed", []))}
            if isinstance(data.get("message"), str):
                result["message"] = data["message"]
                result["notified"] = bool(data.get("notified"))
            if data.get("needs_triage"):
                result["needs_triage"] = True
            return result
    except (OSError, ValueError, TypeError):
        pass
    return {"pending": False, "completed": []}


def write(root: Path, data: dict) -> None:
    target = path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, target)


def merged(root: Path, issue: int, pr: str, title: str, *, every: int = 3) -> dict | None:
    data = read(root)
    if data["pending"] or any(item["issue"] == issue for item in data["completed"]):
        return None
    data["completed"].append({"issue": issue, "pr": pr, "title": title})
    data["pending"] = len(data["completed"]) >= every
    write(root, data)
    return data if data["pending"] else None


def acknowledge(root: Path) -> bool:
    data = read(root)
    if not data["pending"]:
        return False
    write(root, {"pending": False, "completed": [], "needs_triage": True})
    return True
