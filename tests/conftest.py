import json
import os
import subprocess
from pathlib import Path

import pytest

# The browser scenarios need Playwright and a browser, which the default gates do not have.
# Run them on purpose with: DEVFLOW_UI_LANE=1 python -m pytest tests/ui -p devflow.uirunner
collect_ignore_glob = [] if os.environ.get("DEVFLOW_UI_LANE") == "1" else ["ui/*"]


def git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    return path


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(path), *args],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def commit_all(path: Path, message: str = "commit") -> str:
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)
    return git(path, "rev-parse", "HEAD")


@pytest.fixture
def base_repo(tmp_path: Path) -> Path:
    """A repo with one commit on main containing a test file and a source file."""
    root = git_init(tmp_path / "base")
    (root / "src").mkdir()
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
        "    assert add(0, 0) == 0\n    assert add(-1, 1) == 0\n    assert add(2, 2) == 4\n"
    )
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: ci\n")
    commit_all(root, "base")
    return root


@pytest.fixture
def py_repo(tmp_path: Path) -> Path:
    root = git_init(tmp_path / "pyrepo")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n'
        "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n"
        "[tool.ruff]\nline-length = 100\n"
    )
    (root / "tests").mkdir()
    (root / "CLAUDE.md").write_text("# conventions\n")
    return root


@pytest.fixture
def node_repo(tmp_path: Path) -> Path:
    root = git_init(tmp_path / "noderepo")
    (root / "package.json").write_text(json.dumps({
        "scripts": {"test": "vitest run", "lint": "oxlint", "typecheck": "tsc --noEmit",
                    "build": "next build", "test:e2e": "playwright test"}
    }))
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
    return root
