import json
import subprocess
from pathlib import Path

import pytest

from devflow import board_setup, cli
from devflow.board import BoardError
from devflow.loadconfig import load_config
from tests.test_engine import Env


class Gh:
    """Canned `gh` for the create flow. Shapes were captured from a real run."""

    def __init__(self, fail_on: str | None = None, err: str = "boom"):
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail_on, self.err = fail_on, err

    def __call__(self, cmd, input_text=None):
        self.calls.append((cmd, input_text))
        key = " ".join(cmd[1:3])
        if self.fail_on and key.startswith(self.fail_on):
            return subprocess.CompletedProcess(cmd, 1, "", self.err)
        out = {
            "project create": {"number": 6, "id": "PVT_x", "url": "https://github.com/users/me/projects/6"},
            "project field-list": {"fields": [{"id": "F_title", "name": "Title"},
                                              {"id": "F_status", "name": "Status"}]},
        }.get(key, {})
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")

    def steps(self):
        return [" ".join(c[1:3]) for c, _ in self.calls]


def test_create_runs_every_step_in_order():
    gh = Gh()
    made = board_setup.create_project("me", "My queue", "me/repo", gh)
    assert made.number == 6 and made.url.endswith("/projects/6")
    assert gh.steps() == ["project create", "project field-list", "api graphql",
                         "project field-create", "project link"]
    create = gh.calls[0][0]
    assert create[create.index("--title") + 1] == "My queue" and "--owner" in create


def test_status_options_are_replaced_through_graphql():
    gh = Gh()
    board_setup.create_project("me", "t", "me/repo", gh)
    cmd, stdin = gh.calls[2]
    sent = json.loads(stdin)
    assert sent["variables"]["fieldId"] == "F_status" and "--input" in cmd
    assert [o["name"] for o in sent["variables"]["options"]] == [
        "Backlog", "Ready", "In progress", "In review", "Done"]
    assert all(o["color"] and o["description"] for o in sent["variables"]["options"])


def test_priority_field_and_repo_link():
    gh = Gh()
    board_setup.create_project("me", "t", "me/repo", gh)
    field = gh.calls[3][0]
    assert field[field.index("--name") + 1] == "Priority"
    assert field[field.index("--single-select-options") + 1] == "P0,P1,P2,P3"
    link = gh.calls[4][0]
    assert link[link.index("--repo") + 1] == "me/repo" and link[link.index("--owner") + 1] == "me"


def test_a_failure_stops_the_flow_and_names_the_step():
    gh = Gh(fail_on="api graphql")
    with pytest.raises(BoardError, match="gh api graphql"):
        board_setup.create_project("me", "t", "me/repo", gh)
    assert "project link" not in gh.steps()


def test_missing_scope_gets_the_fix():
    gh = Gh(fail_on="project create", err="your token is missing required scopes [project]")
    with pytest.raises(BoardError, match="gh auth refresh -h github.com -s project"):
        board_setup.create_project("me", "t", "me/repo", gh)


def test_missing_status_field():
    class NoStatus(Gh):
        def __call__(self, cmd, input_text=None):
            if " ".join(cmd[1:3]) == "project field-list":
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"fields": []}), "")
            return super().__call__(cmd, input_text)

    with pytest.raises(BoardError, match="no Status field"):
        board_setup.create_project("me", "t", "me/repo", NoStatus())


def test_the_statuses_it_creates_are_the_ones_the_board_expects():
    from devflow.loadconfig import DEFAULT_STATUSES

    assert [n for n, _, _ in board_setup.STATUS_OPTIONS] == list(DEFAULT_STATUSES.values())


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.store.close()


def test_write_config_inserts_a_loadable_block(env: Env):
    board_setup.write_config(env.repo, 6, "me")
    cfg = load_config(env.repo)
    assert cfg.board.enabled and cfg.board.project == 6 and cfg.board.owner == "me"
    assert cfg.agents["coder"] == "claude"  # the rest of the file is untouched


def test_write_config_refuses_to_overwrite(env: Env):
    board_setup.write_config(env.repo, 6, "me")
    with pytest.raises(BoardError, match="already has a"):
        board_setup.write_config(env.repo, 7, "me")


def test_cli_create_end_to_end(env: Env, monkeypatch, capsys):
    gh = Gh()
    monkeypatch.setattr(board_setup, "_run", gh)
    monkeypatch.setattr(board_setup.create_project, "__defaults__", (gh,))
    cfg_path = env.repo / ".devflow" / "config.toml"
    cfg_path.write_text(cfg_path.read_text().replace('remote = ""', 'remote = "https://github.com/me/repo.git"'))
    assert cli.main(["board-create", "--title", "Q", str(env.repo)]) == 0
    out = capsys.readouterr().out
    assert "created project #6" in out and "linked to me/repo" in out
    assert load_config(env.repo).board.project == 6
    assert cli.main(["board-create", str(env.repo)]) == 2  # already configured
    assert "already uses project #6" in capsys.readouterr().err
