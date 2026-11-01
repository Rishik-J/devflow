import json
from pathlib import Path

from devflow.detect import Project, detect_profile, repo_root


def test_python_gates(py_repo: Path):
    p = detect_profile(py_repo, have_uv=False).projects[0]
    assert p.kind == "python" and p.path == "."
    assert p.gates["lint"] == ["ruff check ."]
    assert p.gates["unit"] == ["python -m pytest"]
    assert "integration" not in p.gates
    assert p.missing_gates() == ["typecheck", "integration", "build"]


def test_python_uses_uv_when_locked(py_repo: Path):
    (py_repo / "uv.lock").write_text("")
    assert detect_profile(py_repo, have_uv=False).projects[0].gates["unit"] == ["uv run pytest"]


def test_python_integration_dir_splits_unit_and_integration(py_repo: Path):
    (py_repo / "tests" / "integration").mkdir()
    g = detect_profile(py_repo, have_uv=False).projects[0].gates
    assert g["unit"] == ["python -m pytest --ignore=tests/integration"]
    assert g["integration"] == ["python -m pytest tests/integration"]


def test_python_integration_marker(py_repo: Path):
    (py_repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers = ["integration: needs services"]\n'
    )
    g = detect_profile(py_repo, have_uv=False).projects[0].gates
    assert g["unit"] == ['python -m pytest -m "not integration"']
    assert g["integration"] == ["python -m pytest -m integration"]


def test_node_gates_use_lockfile_manager(node_repo: Path):
    g = detect_profile(node_repo, have_uv=False).projects[0].gates
    assert g == {
        "lint": ["pnpm run lint"], "typecheck": ["pnpm run typecheck"],
        "unit": ["pnpm run test"], "integration": ["pnpm run test:e2e"],
        "build": ["pnpm run build"],
    }


def test_node_without_lockfile_defaults_to_npm(node_repo: Path):
    (node_repo / "pnpm-lock.yaml").unlink()
    assert detect_profile(node_repo, have_uv=False).projects[0].gates["unit"] == ["npm run test"]


def test_monorepo_finds_nested_projects_and_skips_node_modules(py_repo: Path):
    svc = py_repo / "services" / "api"
    svc.mkdir(parents=True)
    (svc / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    junk = py_repo / "node_modules" / "dep"
    junk.mkdir(parents=True)
    (junk / "package.json").write_text("{}")
    prof = detect_profile(py_repo, have_uv=False)
    assert prof.monorepo
    assert sorted(p.path for p in prof.projects) == [".", "services/api"]


def test_ci_hints_and_conventions(py_repo: Path):
    wf = py_repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "jobs:\n  t:\n    steps:\n      - uses: actions/checkout@v4\n"
        "      - run: pip install -e .\n      - run: pytest -q\n      - run: ruff check .\n"
    )
    prof = detect_profile(py_repo, have_uv=False)
    assert prof.ci_workflows == [".github/workflows/ci.yml"]
    assert "pytest -q" in prof.ci_hints and "pip install -e ." not in prof.ci_hints
    assert "CLAUDE.md" in prof.conventions


def test_repo_root_and_default_branch(py_repo: Path):
    sub = py_repo / "tests"
    assert repo_root(sub) == py_repo.resolve() or repo_root(sub) == py_repo
    assert detect_profile(py_repo, have_uv=False).default_branch == "main"


def test_not_a_repo(tmp_path: Path):
    assert repo_root(tmp_path) is None


def test_project_missing_gates_helper():
    assert Project(".", "go", {"unit": ["go test ./..."]}).missing_gates() == [
        "lint", "typecheck", "integration", "build"
    ]


def test_python_setup_without_uv_uses_venv_and_pip(py_repo: Path):
    (py_repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.11"\n'
        '[project.optional-dependencies]\ndev = ["pytest", "ruff"]\n'
    )
    p = detect_profile(py_repo, have_uv=False).projects[0]
    assert p.setup == ["python3.11 -m venv .venv", '.venv/bin/python -m pip install -e ".[dev]"']


def test_python_setup_with_uv_pins_the_ci_python(py_repo: Path):
    wf = py_repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text('steps:\n  - with:\n      python-version: "3.12"\n')
    (py_repo / "pyproject.toml").write_text('[project]\nname = "x"\nrequires-python = ">=3.11"\n')
    p = detect_profile(py_repo, have_uv=True).projects[0]
    assert p.setup == ["uv venv --python 3.12 .venv",
                       'uv pip install --python .venv/bin/python -e "."']


def test_python_setup_uv_lock(py_repo: Path):
    (py_repo / "pyproject.toml").write_text('[project]\nname = "x"\n')
    (py_repo / "uv.lock").write_text("")
    assert detect_profile(py_repo, have_uv=True).projects[0].setup == ["uv sync --all-extras"]


def test_no_project_table_means_no_setup(py_repo: Path):
    (py_repo / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    assert detect_profile(py_repo, have_uv=False).projects[0].setup == []


def test_lint_uses_uvx_when_ruff_is_not_a_dependency(py_repo: Path):
    declared = '[project]\nname="x"\n[project.optional-dependencies]\ndev=["ruff>=0.9"]\n'
    (py_repo / "pyproject.toml").write_text(declared + "[tool.ruff]\nline-length = 100\n")
    assert detect_profile(py_repo, have_uv=True).projects[0].gates["lint"] == ["ruff check ."]
    (py_repo / "pyproject.toml").write_text('[project]\nname="x"\n[tool.ruff]\nline-length=100\n')
    assert detect_profile(py_repo, have_uv=True).projects[0].gates["lint"] == ["uvx ruff check ."]
    assert detect_profile(py_repo, have_uv=False).projects[0].gates["lint"] == ["ruff check ."]


def test_node_setup_commands(node_repo: Path):
    assert detect_profile(node_repo, have_uv=False).projects[0].setup == [
        "pnpm install --frozen-lockfile"
    ]
    (node_repo / "pnpm-lock.yaml").unlink()
    assert detect_profile(node_repo, have_uv=False).projects[0].setup == ["npm install"]
