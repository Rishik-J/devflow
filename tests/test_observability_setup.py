"""Portable bootstrap: credential reuse, explicit capture and shell argument boundaries."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "infra/langfuse/setup.py"


def run_setup(directory, *repos, capture=False):
    return subprocess.run([sys.executable, str(SETUP), "--data-dir", str(directory),
                           *(["--capture-content"] if capture else []), *map(str, repos)],
                          capture_output=True, text=True, check=True)


def test_setup_reuses_credentials_and_preserves_content_choice(tmp_path):
    repo = tmp_path / "repo with spaces"
    (repo / ".git").mkdir(parents=True)
    data = tmp_path / "credentials"
    run_setup(data, repo, capture=True)
    original = (data / ".env").read_bytes()
    target = repo / ".devflow/state/telemetry.json"
    cfg = json.loads(target.read_text())
    assert cfg["capture_content"] is True
    assert cfg["log_file"] == str(repo / ".devflow/state/traces.jsonl")
    run_setup(data, repo)
    assert (data / ".env").read_bytes() == original
    assert json.loads(target.read_text())["capture_content"] is True
    assert (target.stat().st_mode & 0o777) == 0o600
    assert ((data / ".env").stat().st_mode & 0o777) == 0o600


def test_two_repos_share_local_project_but_new_installations_do_not(tmp_path):
    repos = [tmp_path / name for name in ("first", "second", "third")]
    for repo in repos:
        (repo / ".git").mkdir(parents=True)
    first = run_setup(tmp_path / "stack-one", *repos[:2])
    second = run_setup(tmp_path / "stack-two", repos[2])
    configs = [json.loads((r / ".devflow/state/telemetry.json").read_text()) for r in repos]
    assert configs[0]["secret_key"] == configs[1]["secret_key"]
    assert configs[0]["secret_key"] != configs[2]["secret_key"]
    assert not configs[0].get("capture_content", False)
    assert all(c["secret_key"] not in first.stdout + second.stdout for c in configs)


def test_help_creates_no_credentials(tmp_path):
    data = tmp_path / "credentials"
    result = subprocess.run([sys.executable, str(SETUP), "--data-dir", str(data), "--help"],
                            capture_output=True, text=True, check=True)
    assert "--capture-content" in result.stdout
    assert not data.exists()


def test_wrapper_preserves_paths_flags_and_caller_uid(tmp_path):
    # Stub Docker's boundary; the real image/stack is exercised separately during development.
    commands = tmp_path / "commands.jsonl"
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                      'with open(os.environ["DOCKER_CALLS"], "a") as log:\n'
                      '    log.write(json.dumps(sys.argv[1:]) + "\\n")\n')
    docker.chmod(0o755)
    repo = tmp_path / "repo with spaces"
    (repo / ".git").mkdir(parents=True)
    env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
           "DOCKER_CALLS": str(commands)}
    subprocess.run([str(ROOT / "scripts/observability"), "connect", "--capture-content", str(repo)],
                   env=env, capture_output=True, text=True, check=True)
    calls = [json.loads(line) for line in commands.read_text().splitlines()]
    run = next(call for call in calls if call[0] == "run")
    assert run[-1] == str(repo)
    assert run[run.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert f"{repo}:{repo}" in run
    assert "--capture-content" in run
    assert not any(call[:2] == ["compose", "up"] for call in calls)


def test_initializer_build_context_explicitly_excludes_generated_credentials():
    patterns = (ROOT / "infra/langfuse/.dockerignore").read_text().splitlines()
    assert "*" in patterns
    assert {line for line in patterns if line.startswith("!")} == {"!Dockerfile", "!setup.py"}
