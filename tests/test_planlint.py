"""Plan linter: unit checks, and a replay over the plans devflow really produced."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from devflow import planlint
from devflow.planlint import blocking, lint

GATES = {"lint": ["ruff check ."], "unit": ["python -m pytest"],
         "integration": ["python -m pytest tests/test_engine.py tests/test_telemetry.py"]}


def cfg(gates=None):
    return SimpleNamespace(projects=[SimpleNamespace(path=".", gates=GATES if gates is None else gates)])


def plan(files=("src/calc.py",), *, goal="Add multiply", steps=None, criteria=None, risks=(), out=()):
    return {"status": "ready", "goal": goal,
            "acceptance_criteria": criteria or [{"id": "AC1", "text": "multiply works"}],
            "steps": steps or [{"id": "S1", "description": "edit it", "files": list(files)}],
            "risks": list(risks), "out_of_scope": list(out)}


def kinds(findings):
    return sorted((f.kind, f.severity) for f in findings)


def test_a_clean_plan_has_no_findings():
    assert lint(plan(), cfg()) == []


def test_clarification_plans_are_not_linted():
    assert lint({"status": "needs_clarification", "questions": []}, cfg()) == []


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", ".devflow/config.toml", ".github/actions/x/a.yml"])
def test_protected_paths_block(path):
    found = lint(plan((path,)), cfg())
    assert kinds(found) == [("protected_path", "block")] and "follow_ups" in found[0].detail


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.py", "src/../../x.py"])
def test_paths_must_stay_inside_the_repo(path):
    assert kinds(lint(plan((path,)), cfg())) == [("bad_path", "block")]


@pytest.mark.parametrize("text", [
    "The repo configures only lint and unit gates.",
    "No separate integration gate exists in the repo.",
    "There is no integration gate, so unit tests must cover this.",
    "Verification is unit-only gate coverage.",
])
def test_claiming_there_is_no_integration_gate_blocks_when_one_is_configured(text):
    found = lint(plan(risks=[text]), cfg())
    assert ("gate_claim", "block") in kinds(found)


def test_the_same_claim_is_fine_when_no_integration_gate_is_configured():
    only_unit = {"unit": ["python -m pytest"]}
    assert lint(plan(risks=["No separate integration gate exists in the repo."]), cfg(only_unit)) == []


def test_calling_an_unconfigured_command_the_integration_gate_blocks():
    text = "Run `python -m pytest tests/test_other.py` as the configured integration gate."
    assert ("gate_invented", "block") in kinds(lint(plan(risks=[text]), cfg()))
    ok = "The integration gate (`python -m pytest tests/test_engine.py tests/test_telemetry.py`) must pass."
    assert lint(plan(risks=[ok]), cfg()) == []


def test_skipping_tests_is_only_a_warning():
    found = lint(plan(steps=[{"id": "S1", "description": "Add pytest.mark.skip to the flaky test",
                              "files": ["tests/test_a.py"]}]), cfg())
    assert kinds(found) == [("skip_wording", "warn")] and not blocking(found)
    assert lint(plan(steps=[{"id": "S1", "description": "Never skip tests; run them all",
                             "files": ["tests/test_a.py"]}]), cfg()) == []


def test_new_trace_keys_without_telemetry_py_warn():
    steps = [{"id": "S1", "description": "emit a new trace metadata key", "files": ["src/engine.py"]}]
    assert kinds(lint(plan(steps=steps), cfg())) == [("telemetry_registration", "warn")]
    steps[0]["files"].append("src/devflow/telemetry.py")
    assert lint(plan(steps=steps), cfg()) == []


def test_missing_paths_warn_only_when_the_step_does_not_create_them(tmp_path: Path):
    (tmp_path / "src").mkdir()
    steps = [{"id": "S1", "description": "tweak the parser", "files": ["lib/parse.py"]}]
    assert kinds(lint(plan(steps=steps), cfg(), tmp_path)) == [("missing_path", "warn")]
    steps[0]["description"] = "create the parser"
    assert lint(plan(steps=steps), cfg(), tmp_path) == []
    steps[0] = {"id": "S1", "description": "tweak", "files": ["src/new_module.py"]}
    assert lint(plan(steps=steps), cfg(), tmp_path) == []  # directory exists: a new file is plausible


def test_negative_control_disabling_a_check_changes_the_result(monkeypatch):
    bad = plan((".github/workflows/ci.yml",))
    assert blocking(lint(bad, cfg()))
    monkeypatch.setattr(planlint, "is_protected", lambda path: False)
    assert not blocking(lint(bad, cfg()))  # the test above really depends on is_protected


FIXTURE = Path(__file__).parent / "fixtures" / "recorded_plans.json"


def test_replay_over_the_plans_devflow_really_produced():
    """Every blocking finding on a recorded plan is accounted for.

    Recorded from the devflow repo's own run history (24 plans, 17 approved). The blocked ones:
      - #3: three plans repeat the 'no integration gate' claim that the reviewer rejected, caused by
        a since-fixed bug that hid the gate config from the planner.
      - #12, #17, #66: plans edit .github/workflows/ci.yml; the protected_path gate then blocked
        all three after coding (seen in their gate results).
      - #3 plan 3 lists .devflow/config.toml: a true positive by rule that did not cost anything.
    Nothing else may block, so the false-positive rate on approved-and-merged plans is bounded.
    """
    recorded = json.loads(FIXTURE.read_text())
    assert len(recorded) == 24
    blocked = {}
    for item in recorded:
        found = blocking(lint(item["plan"], cfg()))
        if found:
            blocked.setdefault(item["ticket"], []).append(sorted({f.kind for f in found}))
    assert blocked == {
        "#3": [["gate_claim"], ["protected_path"], ["gate_claim"]],
        "#12": [["protected_path"]],
        "#17": [["protected_path"]],
        "#66": [["protected_path"]],
    }


def test_replay_catches_what_the_reviewer_rejected_for_mechanical_reasons():
    recorded = json.loads(FIXTURE.read_text())
    rejected = [i for i in recorded if (i["review"] or {}).get("decision") == "reject"]
    caught = [i for i in rejected if blocking(lint(i["plan"], cfg()))]
    # 3 rejections were recorded; one was the invented/denied integration gate (mechanical). The other
    # two (a wrong stage.new assertion, an unallowlisted trace key) need judgement, not a linter.
    assert [i["ticket"] for i in caught] == ["#3"]
    assert len(rejected) == 3
