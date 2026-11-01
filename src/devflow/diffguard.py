"""Catch the ways a coding agent can turn a red gate green without fixing anything.

Deterministic checks over the ticket's diff against its base: deleted tests, newly skipped
tests, fewer assertions, and edits to files that control the gates themselves.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

TEST_PATH_PATTERNS = (
    "tests/*", "test/*", "*/tests/*", "*/test/*", "*/__tests__/*", "test_*.py", "*/test_*.py",
    "*_test.py", "*_test.go", "*.test.ts", "*.test.tsx", "*.test.js", "*.spec.ts",
    "*.spec.tsx", "*.spec.js",
)
PROTECTED_PATTERNS = (".devflow/*", ".github/workflows/*", ".github/actions/*")
SKIP_MARKERS = re.compile(
    r"(@pytest\.mark\.(skip|xfail)|pytest\.(skip|xfail)\(|\b(it|test|describe)\.skip\b|"
    r"\bx(it|describe)\(|@unittest\.skip|\bt\.Skip\(|#\[ignore\]|@Ignore\b)"
)
ASSERTION = re.compile(r"(\bassert\b|\bexpect\(|\.should\b|\bassert[A-Z_]\w*\(|\bt\.(Error|Fatal)f?\()")
MIN_NET_ASSERTIONS_REMOVED = 3


@dataclass
class Finding:
    kind: str
    severity: str  # "block" or "warn"
    file: str
    detail: str


def is_test_file(path: str) -> bool:
    return any(fnmatch(path, pat) for pat in TEST_PATH_PATTERNS)


def is_protected(path: str) -> bool:
    return any(fnmatch(path, pat) for pat in PROTECTED_PATTERNS)


def _git(worktree: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(worktree), *args], capture_output=True, text=True, timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def analyze(worktree: Path, base: str) -> list[Finding]:
    findings: list[Finding] = []
    for line in _git(worktree, "diff", "--name-status", f"{base}...HEAD").splitlines():
        status, _, rest = line.partition("\t")
        path = rest.split("\t")[-1]
        if status.startswith("D") and is_test_file(path):
            findings.append(Finding("deleted_test", "block", path, "test file deleted"))
        if is_protected(path):
            findings.append(Finding(
                "protected_path", "block", path,
                "changes gate or workflow configuration; needs human review",
            ))

    removed = added = 0
    current = ""
    for line in _git(worktree, "diff", "-U0", f"{base}...HEAD").splitlines():
        if line.startswith("+++ "):
            current = line[4:].removeprefix("b/") if line != "+++ /dev/null" else ""
            continue
        if line.startswith("--- ") or not current or not is_test_file(current):
            continue
        body = line[1:]
        if line.startswith("+") and SKIP_MARKERS.search(body):
            findings.append(Finding("skipped_test", "block", current,
                                    f"skip/xfail marker added: {body.strip()[:80]}"))
        if line.startswith("-") and ASSERTION.search(body):
            removed += 1
        elif line.startswith("+") and ASSERTION.search(body):
            added += 1

    if removed - added >= MIN_NET_ASSERTIONS_REMOVED:
        findings.append(Finding(
            "fewer_assertions", "warn", "(tests)",
            f"{removed} assertion lines removed vs {added} added in test files",
        ))
    return findings


def blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "block"]
