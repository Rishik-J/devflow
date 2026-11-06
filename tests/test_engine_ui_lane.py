"""Engine wiring for the optional browser verification lane."""

from devflow import baseline, uilane
from devflow.gates import GateReport
from devflow.loadconfig import UiCfg
from tests.test_engine import Env

MATRIX = [{"id": "UC1", "title": "Board", "kind": "happy_path"}]


def test_ui_lane_is_not_called_for_backend_diff(tmp_path, monkeypatch):
    env = Env(tmp_path)
    try:
        env.cfg.ui = UiCfg(enabled=True, command="browser tests")
        engine = env.engine()
        monkeypatch.setattr("devflow.engine.gitops.diff_names", lambda *a: ["src/calc.py"])
        monkeypatch.setattr(uilane, "run_lane", lambda *a: (_ for _ in ()).throw(AssertionError("browser ran")))
        report, verdict = GateReport([]), baseline.Verdict(ok=True)
        engine._ui_lane(env.repo, "base", {"ui_use_cases": MATRIX}, report, verdict)
        assert report.ui is None and verdict.ok
    finally:
        env.store.close()


def test_ui_lane_failure_blocks_engine_verdict(tmp_path, monkeypatch):
    env = Env(tmp_path)
    try:
        env.cfg.ui = UiCfg(enabled=True, command="browser tests")
        engine = env.engine()
        monkeypatch.setattr("devflow.engine.gitops.diff_names", lambda *a: ["site/index.html"])
        failed = uilane.UiLaneResult(status="failed", command="browser tests",
                                      reasons=["case UC1 failed"], use_cases=MATRIX)
        monkeypatch.setattr(uilane, "run_lane", lambda *a: failed)
        report, verdict = GateReport([]), baseline.Verdict(ok=True)
        engine._ui_lane(env.repo, "base", {"ui_use_cases": MATRIX}, report, verdict)
        assert report.ui is failed and not verdict.ok
        assert verdict.new_failures[0].gate == "ui"
    finally:
        env.store.close()


def test_ui_evaluator_cannot_turn_missing_browser_case_into_pass(tmp_path, monkeypatch):
    env = Env(tmp_path)
    try:
        engine = env.engine()
        lane = uilane.UiLaneResult(status="passed", use_cases=MATRIX,
                                   report=uilane.UiLaneReport(cases=[]))
        report = GateReport([], ui=lane)
        monkeypatch.setattr(engine, "_ui_evaluation", lambda *a: {
            "use_cases": [{"id": "UC1", "status": "pass", "case_ids": [], "evidence": "claimed"}],
            "findings": [], "notes": ""})
        table, blocking, _ = engine._ui_verification(None, env.repo,
                                                       {"ui_use_cases": MATRIX}, report)
        assert table and "missing" in table["table"]
        assert blocking
    finally:
        env.store.close()
