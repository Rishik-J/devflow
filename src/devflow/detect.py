"""Detect what a repo is and how it is built and tested. Read-only, deterministic."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

GATES = ("lint", "typecheck", "unit", "integration", "build")

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "dist", "build", ".next", ".turbo",
    "__pycache__", "graphify-out", ".devflow", "target", ".mypy_cache", ".ruff_cache",
}
MAX_DEPTH = 3
MAX_PROJECTS = 25

CONVENTION_FILES = (
    "CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md", ".cursorrules", ".cursor/rules",
    "README.md", "docs",
)

NODE_SCRIPTS = {
    "lint": ("lint",),
    "typecheck": ("typecheck", "type-check", "check-types", "tsc"),
    "unit": ("test:unit", "test"),
    "integration": ("test:integration", "test:e2e", "e2e", "integration"),
    "build": ("build",),
}
NODE_LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"),
    ("bun.lock", "bun"), ("package-lock.json", "npm"),
)
CI_KEYWORDS = ("pytest", "ruff", "mypy", "pyright", "vitest", "jest", "tsc", "lint",
               "test", "build", "cargo", "go test", "go vet")
_RUN_LINE = re.compile(r"^\s*-?\s*run:\s*(?P<cmd>[^|>].*)$")


@dataclass
class Project:
    path: str
    kind: str
    gates: dict[str, list[str]] = field(default_factory=dict)
    setup: list[str] = field(default_factory=list)

    def missing_gates(self) -> list[str]:
        return [g for g in GATES if g not in self.gates]


@dataclass
class Profile:
    root: str
    name: str
    default_branch: str | None
    remote: str | None
    projects: list[Project]
    conventions: list[str]
    ci_workflows: list[str]
    ci_hints: list[str]
    monorepo: bool


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=20, check=True
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return out.stdout.strip() or None


def repo_root(start: Path) -> Path | None:
    top = _git(start, "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def default_branch(root: Path) -> str | None:
    ref = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if ref:
        return ref.split("/", 1)[1] if "/" in ref else ref
    # symbolic-ref also works on an unborn branch (a repo with no commits yet)
    return _git(root, "symbolic-ref", "--short", "HEAD")


def _find_markers(root: Path) -> list[tuple[Path, str]]:
    names = {"pyproject.toml": "python", "package.json": "node", "Cargo.toml": "rust",
             "go.mod": "go"}
    found: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        if depth >= MAX_DEPTH:
            dirnames[:] = []
        for fname, kind in names.items():
            if fname in filenames:
                found.append((Path(dirpath), kind))
    return found[:MAX_PROJECTS]


def _load_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _py_deps(data: dict) -> list[str]:
    deps = [str(x) for x in data.get("project", {}).get("dependencies", [])]
    for group in data.get("project", {}).get("optional-dependencies", {}).values():
        deps += [str(x) for x in group]
    return deps


def _dep_name(spec: str) -> str:
    return re.split(r"[\s<>=!~\[;(]", spec.strip(), maxsplit=1)[0].lower()


def _min_python(data: dict) -> str | None:
    m = re.search(r">=\s*(\d+\.\d+)", str(data.get("project", {}).get("requires-python", "")))
    return m.group(1) if m else None


def ci_python_version(root: Path) -> str | None:
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return None
    for wf in sorted([*wf_dir.glob("*.yml"), *wf_dir.glob("*.yaml")]):
        m = re.search(r"python-version:\s*[\"']?(\d+\.\d+)", wf.read_text(errors="replace"))
        if m:
            return m.group(1)
    return None


def _python_setup(d: Path, data: dict, ci_python: str | None, have_uv: bool) -> list[str]:
    """Commands that make a fresh checkout able to run its own gates."""
    if not data.get("project") or ((d / "uv.lock").exists() and have_uv):
        return ["uv sync --all-extras"] if (d / "uv.lock").exists() and have_uv else []
    version = ci_python or _min_python(data)
    target = ".[dev]" if "dev" in data["project"].get("optional-dependencies", {}) else "."
    if have_uv:
        py = f" --python {version}" if version else ""
        return [f"uv venv{py} .venv",
                f'uv pip install --python .venv/bin/python -e "{target}"']
    interp = f"python{version}" if version else "python3"
    return [f"{interp} -m venv .venv", f'.venv/bin/python -m pip install -e "{target}"']


def _node_setup(pm: str, d: Path, root: Path) -> list[str]:
    locked = any((base / lock).exists() for base in (d, root) for lock, _ in NODE_LOCKFILES)
    if pm == "npm":
        return ["npm ci" if locked else "npm install"]
    return [f"{pm} install --frozen-lockfile" if locked and pm != "bun" else f"{pm} install"]


def _python_gates(d: Path, have_uv: bool = False) -> dict[str, list[str]]:
    data = _load_toml(d / "pyproject.toml")
    tool = data.get("tool", {})
    runner = "uv run " if (d / "uv.lock").exists() else ""
    gates: dict[str, list[str]] = {}

    if "ruff" in tool or (d / "ruff.toml").exists() or (d / ".ruff.toml").exists():
        ruff_declared = any(_dep_name(x) == "ruff" for x in _py_deps(data))
        # A project that does not install ruff would fail with "command not found".
        ruff = "uvx ruff" if have_uv and not runner and not ruff_declared else "ruff"
        gates["lint"] = [f"{runner}{ruff} check ."]
    if "mypy" in tool or (d / "mypy.ini").exists():
        gates["typecheck"] = [f"{runner}mypy ."]
    elif "pyright" in tool or (d / "pyrightconfig.json").exists():
        gates["typecheck"] = [f"{runner}pyright"]

    ini = tool.get("pytest", {}).get("ini_options", {})
    deps = [str(x) for x in data.get("project", {}).get("dependencies", [])]
    for group in data.get("project", {}).get("optional-dependencies", {}).values():
        deps += [str(x) for x in group]
    has_pytest = bool(ini) or any(x.lower().startswith("pytest") for x in deps) or (
        (d / "tests").is_dir()
    )
    if has_pytest:
        pytest = f"{runner}pytest" if runner else "python -m pytest"
        markers = [str(m) for m in ini.get("markers", [])]
        marker_integration = any(m.split(":")[0].strip() == "integration" for m in markers)
        int_dir = next((p for p in ("tests/integration", "tests/e2e") if (d / p).is_dir()), None)
        if marker_integration:
            gates["unit"] = [f'{pytest} -m "not integration"']
            gates["integration"] = [f"{pytest} -m integration"]
        elif int_dir:
            gates["unit"] = [f"{pytest} --ignore={int_dir}"]
            gates["integration"] = [f"{pytest} {int_dir}"]
        else:
            gates["unit"] = [pytest]
    return gates


def _node_pm(d: Path, root: Path) -> str:
    for base in (d, root):
        for lock, pm in NODE_LOCKFILES:
            if (base / lock).exists():
                return pm
    return "npm"


def _node_gates(d: Path, root: Path) -> dict[str, list[str]]:
    try:
        scripts = json.loads((d / "package.json").read_text()).get("scripts", {})
    except (OSError, json.JSONDecodeError):
        return {}
    pm = _node_pm(d, root)
    gates: dict[str, list[str]] = {}
    for gate, candidates in NODE_SCRIPTS.items():
        script = next((c for c in candidates if c in scripts), None)
        if script:
            gates[gate] = [f"{pm} run {script}"]
    return gates


def _other_gates(kind: str) -> dict[str, list[str]]:
    if kind == "rust":
        return {"lint": ["cargo clippy -- -D warnings"], "unit": ["cargo test"],
                "build": ["cargo build"]}
    if kind == "go":
        return {"lint": ["go vet ./..."], "unit": ["go test ./..."], "build": ["go build ./..."]}
    return {}


def detect_projects(root: Path, have_uv: bool | None = None) -> list[Project]:
    if have_uv is None:
        have_uv = shutil.which("uv") is not None
    ci_python = ci_python_version(root)
    projects = []
    for d, kind in _find_markers(root):
        setup: list[str] = []
        if kind == "python":
            gates = _python_gates(d, have_uv)
            setup = _python_setup(d, _load_toml(d / "pyproject.toml"), ci_python, have_uv)
        elif kind == "node":
            gates = _node_gates(d, root)
            setup = _node_setup(_node_pm(d, root), d, root)
        else:
            gates = _other_gates(kind)
        rel = str(d.relative_to(root)) or "."
        projects.append(Project(path=rel, kind=kind, gates=gates, setup=setup))
    return projects


def ci_hints(root: Path) -> tuple[list[str], list[str]]:
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return [], []
    workflows, hints = [], []
    for wf in sorted([*wf_dir.glob("*.yml"), *wf_dir.glob("*.yaml")]):
        workflows.append(str(wf.relative_to(root)))
        for line in wf.read_text(errors="replace").splitlines():
            m = _RUN_LINE.match(line)
            if m and any(k in m["cmd"] for k in CI_KEYWORDS):
                cmd = m["cmd"].strip()
                if cmd not in hints:
                    hints.append(cmd)
    return workflows, hints


def detect_profile(root: Path, have_uv: bool | None = None) -> Profile:
    projects = detect_projects(root, have_uv)
    workflows, hints = ci_hints(root)
    conventions = [c for c in CONVENTION_FILES if (root / c).exists()]
    return Profile(
        root=str(root),
        name=root.name,
        default_branch=default_branch(root),
        remote=_git(root, "remote", "get-url", "origin"),
        projects=projects,
        conventions=conventions,
        ci_workflows=workflows,
        ci_hints=hints,
        monorepo=len(projects) > 1,
    )
