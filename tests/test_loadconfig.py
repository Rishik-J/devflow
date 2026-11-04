from pathlib import Path

import pytest

from devflow.graphify import GraphResult
from devflow.init import run_init
from devflow.loadconfig import ConfigError, config_path, load_config


def init(root: Path) -> None:
    run_init(root, graph_builder=lambda r: GraphResult(False, False, message="skip"),
             which={"claude": "/x", "codex": "/y"}.get)


def test_loads_what_init_wrote(py_repo: Path):
    init(py_repo)
    cfg = load_config(py_repo)
    assert cfg.name == "pyrepo" and cfg.default_branch == "main"
    assert cfg.limits.max_review_rounds == 3 and cfg.gate_timeout_minutes == 20
    assert cfg.agents["coder"] == "claude" and cfg.human_gates["merge_approval"] is True
    assert cfg.projects[0].gates["unit"] == ["python -m pytest"]
    assert "~" not in str(cfg.worktree_root)


def test_missing_config_points_at_init(tmp_path: Path):
    with pytest.raises(ConfigError, match="devflow init"):
        load_config(tmp_path)


def test_invalid_toml(py_repo: Path):
    init(py_repo)
    config_path(py_repo).write_text("[repo\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(py_repo)


def test_rejects_bad_limits_and_unknown_gates(py_repo: Path):
    init(py_repo)
    path = config_path(py_repo)
    good = path.read_text()
    path.write_text(good.replace("max_review_rounds = 3", "max_review_rounds = 0"))
    with pytest.raises(ConfigError, match="max_review_rounds"):
        load_config(py_repo)
    path.write_text(good.replace("[project.gates]", '[project.gates]\ndeploy = ["x"]', 1))
    with pytest.raises(ConfigError, match="unknown gates"):
        load_config(py_repo)


def test_defaults_when_optional_keys_missing(py_repo: Path):
    config_path(py_repo).parent.mkdir(parents=True)
    config_path(py_repo).write_text('[repo]\nname = "r"\n')
    cfg = load_config(py_repo)
    assert cfg.default_branch == "main" and cfg.projects == [] and cfg.limits.max_review_rounds == 3


def test_setup_commands_roundtrip(py_repo: Path):
    (py_repo / "pyproject.toml").write_text('[project]\nname = "x"\nrequires-python = ">=3.12"\n')
    init(py_repo)
    cfg = load_config(py_repo)
    assert cfg.projects[0].setup == [
        "python3.12 -m venv .venv", '.venv/bin/python -m pip install -e "."'
    ]


def test_bad_setup_type_is_rejected(py_repo: Path):
    init(py_repo)
    path = config_path(py_repo)
    path.write_text(path.read_text().replace('kind = "python"', 'kind = "python"\nsetup = [1]', 1))
    with pytest.raises(ConfigError, match="setup"):
        load_config(py_repo)


def test_ticket_budget_limits_parse_and_default_off(tmp_path):
    from devflow.loadconfig import load_config
    base = '[repo]\nname = "x"\n'
    (tmp_path / ".devflow").mkdir()
    (tmp_path / ".devflow/config.toml").write_text(base)
    assert load_config(tmp_path).limits.max_ticket_cost_usd == 0
    (tmp_path / ".devflow/config.toml").write_text(
        base + "[limits]\nmax_ticket_cost_usd = 7.5\nmax_ticket_tokens = 2000000\n")
    limits = load_config(tmp_path).limits
    assert limits.max_ticket_cost_usd == 7.5 and limits.max_ticket_tokens == 2_000_000


def test_agent_capabilities_parse_and_validate(tmp_path):
    import pytest

    from devflow.loadconfig import ConfigError, load_config
    (tmp_path / ".devflow").mkdir(exist_ok=True)
    cfg = tmp_path / ".devflow/config.toml"
    base = '[repo]\nname = "x"\n[routing.agents.codex]\npriority = 2\n'
    cfg.write_text(base)
    assert load_config(tmp_path).routing.agents["codex"].capabilities is None
    cfg.write_text(base + 'capabilities = ["sockets"]\n')
    assert load_config(tmp_path).routing.agents["codex"].capabilities == ("sockets",)
    cfg.write_text(base + 'capabilities = ["teleport"]\n')
    with pytest.raises(ConfigError, match="unknown capabilities"):
        load_config(tmp_path)
    cfg.write_text(base + 'capabilities = "sockets"\n')
    with pytest.raises(ConfigError, match="list of strings"):
        load_config(tmp_path)


def test_planning_lint_mode_parses_and_validates(tmp_path):
    import pytest

    from devflow.loadconfig import ConfigError, load_config
    (tmp_path / ".devflow").mkdir(exist_ok=True)
    cfg = tmp_path / ".devflow/config.toml"
    base = '[repo]\nname = "x"\n'
    cfg.write_text(base)
    assert load_config(tmp_path).planning.lint == "warn"
    cfg.write_text(base + '[planning]\nlint = "block"\nlint_retries = 3\n')
    planning = load_config(tmp_path).planning
    assert planning.lint == "block" and planning.lint_retries == 3
    cfg.write_text(base + '[planning]\nlint = "strict"\n')
    with pytest.raises(ConfigError, match="lint must be"):
        load_config(tmp_path)
