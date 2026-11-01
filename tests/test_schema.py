from pathlib import Path

import pytest

from devflow import schema

READY = {
    "status": "ready",
    "goal": "g",
    "acceptance_criteria": [{"id": "AC1", "text": "t"}],
    "steps": [{"id": "S1", "description": "d", "files": ["a.py"]}],
}


def test_ready_plan_is_valid():
    assert schema.errors("plan", READY) == []


def test_ready_plan_requires_goal_and_steps():
    bad = {"status": "ready"}
    msg = " ".join(schema.errors("plan", bad))
    assert "goal" in msg and "steps" in msg


def test_needs_clarification_requires_questions_and_caps_them():
    assert schema.errors("plan", {"status": "needs_clarification"})
    many = {"status": "needs_clarification",
            "questions": [{"question": "q", "proposed_default": "d"}] * 6}
    assert schema.errors("plan", many)
    ok = {"status": "needs_clarification",
          "questions": [{"question": "q", "proposed_default": "d"}]}
    assert schema.errors("plan", ok) == []


def test_test_plan_must_have_no_uncovered_criteria():
    plan = {"tests": [{"id": "T1", "criterion_ids": ["AC1"], "kind": "unit",
                       "description": "d", "target_files": ["t.py"]}],
            "uncovered_criteria": []}
    assert schema.errors("test_plan", plan) == []
    plan["uncovered_criteria"] = ["AC2"]
    assert schema.errors("test_plan", plan)


def test_review_schema():
    ok = {"verdict": "approve", "findings": [],
          "integration": {"ran": True, "passed": True}}
    assert schema.errors("review", ok) == []
    bad = {"verdict": "maybe", "findings": [{"id": "1"}], "integration": {}}
    assert len(schema.errors("review", bad)) >= 3


def test_validate_raises_with_paths():
    with pytest.raises(schema.SchemaError, match="steps"):
        schema.validate("plan", {"status": "ready", "goal": "g"})


def test_repo_local_schema_overrides_shipped(tmp_path: Path):
    d = tmp_path / ".devflow" / "schemas"
    d.mkdir(parents=True)
    (d / "plan.schema.json").write_text('{"type": "object", "required": ["custom"]}')
    assert schema.errors("plan", READY, root=tmp_path)
    assert schema.errors("plan", READY) == []


def test_unknown_schema():
    with pytest.raises(schema.SchemaError):
        schema.load_schema("nope")


def test_request_changes_needs_a_blocking_finding():
    base = {"integration": {"ran": True, "passed": True}}
    none = {**base, "verdict": "request_changes", "findings": []}
    non_blocking = {**base, "verdict": "request_changes", "findings": [
        {"id": "1", "severity": "nit", "blocking": False, "file": "a", "summary": "s"}]}
    blocking = {**base, "verdict": "request_changes", "findings": [
        {"id": "1", "severity": "major", "blocking": True, "file": "a", "summary": "s"}]}
    assert schema.errors("review", none) and schema.errors("review", non_blocking)
    assert schema.errors("review", blocking) == []
    assert schema.errors("review", {**base, "verdict": "approve", "findings": []}) == []


def test_empty_summary_is_rejected():
    bad = {"verdict": "approve", "integration": {"ran": False, "passed": False},
           "findings": [{"id": "1", "severity": "nit", "blocking": False, "file": "a", "summary": ""}]}
    assert schema.errors("review", bad)
