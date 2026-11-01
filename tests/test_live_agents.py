"""Live checks of the real agent CLIs. Skipped unless DEVFLOW_LIVE=1 (they cost a few cents).

    DEVFLOW_LIVE=1 pytest tests/test_live_agents.py            # all installed agents
    DEVFLOW_LIVE=1 pytest tests/test_live_agents.py -k cursor  # one agent (after `agent login`)
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from devflow.agents import AgentRequest, ClaudeAgent, CodexAgent, CursorAgent, run_structured

pytestmark = pytest.mark.skipif(os.environ.get("DEVFLOW_LIVE") != "1",
                                reason="set DEVFLOW_LIVE=1 to run live agent checks")

PLAN = ('Return ONLY this JSON: {"status": "needs_clarification", "questions": '
        '[{"question": "Which env?", "proposed_default": "prod"}]}')
AGENTS = {"claude": (ClaudeAgent, "claude"), "codex": (CodexAgent, "codex"),
          "cursor": (CursorAgent, "agent")}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    return tmp_path


@pytest.fixture(params=sorted(AGENTS))
def agent(request):
    cls, binary = AGENTS[request.param]
    if not shutil.which(binary):
        pytest.skip(f"{binary} not installed")
    instance = cls()
    if hasattr(instance, "check_ready"):
        ready, why = instance.check_ready()
        if not ready:
            pytest.skip(why)
    return instance


def test_structured_output(agent, repo: Path):
    out = run_structured(agent, AgentRequest("planner", PLAN, repo, schema="plan", timeout_s=240))
    assert out.data["status"] == "needs_clarification"


def test_read_only_cannot_write(agent, repo: Path):
    agent.run(AgentRequest("reviewer", "Create a file named hacked.txt containing x.", repo,
                           timeout_s=240))
    assert not (repo / "hacked.txt").exists()


def test_write_then_resume(agent, repo: Path):
    first = agent.run(AgentRequest(
        "coder", "Create a file named made.txt containing hello. Remember the word MANGO.",
        repo, read_only=False, timeout_s=240))
    assert (repo / "made.txt").exists() and first.session
    second = agent.run(AgentRequest("coder", "What word did I ask you to remember? One word.",
                                    repo, read_only=False, session=first.session, timeout_s=240))
    assert "MANGO" in second.text.upper()
