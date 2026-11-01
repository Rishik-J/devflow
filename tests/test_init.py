import json
import tomllib
from pathlib import Path

import pytest

from devflow.graphify import GraphResult
from devflow.init import InitError, run_init

BOTH = {"claude": "/bin/claude", "codex": "/bin/codex"}  # no uv: deterministic detection


def fake_graph(root: Path) -> GraphResult:
    return GraphResult(True, True, nodes=12, edges=30)


def init(root: Path, **kw):
    kw.setdefault("graph_builder", fake_graph)
    kw.setdefault("which", BOTH.get)
    return run_init(root, **kw)


def test_init_writes_valid_config_prompts_and_schemas(py_repo: Path):
    r = init(py_repo)
    cfg = tomllib.loads((py_repo / ".devflow/config.toml").read_text())
    assert cfg["repo"]["name"] == "pyrepo"
    assert cfg["agents"]["coder"] == "claude" and cfg["agents"]["reviewer"] == "codex"
    assert cfg["graphify"] == {"enabled": True, "nodes": 12, "edges": 30}
    assert cfg["project"][0]["gates"]["unit"] == ["python -m pytest"]
    for name in ("planner", "test_planner", "coder", "reviewer", "triage"):
        assert (py_repo / f".devflow/prompts/{name}.md").exists()
    for name in ("plan", "test_plan", "review"):
        json.loads((py_repo / f".devflow/schemas/{name}.schema.json").read_text())
    assert any(".devflow/config.toml" == f for f in r.created)


def test_prompts_are_filled_with_repo_facts_and_have_no_placeholders(py_repo: Path):
    init(py_repo)
    text = (py_repo / ".devflow/prompts/reviewer.md").read_text()
    assert "python -m pytest" in text and "CLAUDE.md" in text and "12 nodes" in text
    for f in (py_repo / ".devflow/prompts").glob("*.md"):
        assert "$" not in f.read_text(), f.name


def test_init_is_idempotent_and_preserves_edits(py_repo: Path):
    init(py_repo)
    cfg = py_repo / ".devflow/config.toml"
    cfg.write_text(cfg.read_text() + "# my edit\n")
    r = init(py_repo)
    assert r.created == [] and ".devflow/config.toml" in r.skipped
    assert "# my edit" in cfg.read_text()


def test_force_overwrites(py_repo: Path):
    init(py_repo)
    cfg = py_repo / ".devflow/config.toml"
    cfg.write_text("garbage")
    init(py_repo, force=True)
    assert tomllib.loads(cfg.read_text())["repo"]["name"] == "pyrepo"


def test_needs_input_flags_missing_integration_tests(py_repo: Path):
    r = init(py_repo)
    assert any("no integration-test command" in item for item in r.needs_input)


def test_single_agent_warns_about_independent_review(py_repo: Path):
    r = init(py_repo, which={"claude": "/bin/claude"}.get)
    cfg = tomllib.loads((py_repo / ".devflow/config.toml").read_text())
    assert cfg["agents"]["coder"] == cfg["agents"]["reviewer"] == "claude"
    assert any("same model" in item for item in r.needs_input)


def test_graph_failure_is_reported_not_fatal(py_repo: Path):
    r = init(py_repo, graph_builder=lambda root: GraphResult(False, False, message="no graphify"))
    cfg = tomllib.loads((py_repo / ".devflow/config.toml").read_text())
    assert cfg["graphify"]["enabled"] is False
    assert any("not built" in item for item in r.needs_input)


def test_no_graph_flag_skips_builder(py_repo: Path):
    def boom(root: Path) -> GraphResult:
        raise AssertionError("should not run")

    r = init(py_repo, use_graph=False, graph_builder=boom)
    assert r.graph is None


def test_generated_files_are_excluded_locally_not_in_gitignore(py_repo: Path):
    init(py_repo)
    exclude = (py_repo / ".git/info/exclude").read_text()
    assert "graphify-out/" in exclude and ".devflow/state/" in exclude
    assert not (py_repo / ".gitignore").exists()
    init(py_repo)
    assert (py_repo / ".git/info/exclude").read_text().count("graphify-out/") == 1


def test_node_repo(node_repo: Path):
    init(node_repo)
    cfg = tomllib.loads((node_repo / ".devflow/config.toml").read_text())
    assert cfg["project"][0]["gates"]["integration"] == ["pnpm run test:e2e"]


def test_not_a_git_repo_errors(tmp_path: Path):
    with pytest.raises(InitError):
        init(tmp_path)
