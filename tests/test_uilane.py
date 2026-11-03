import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from devflow import uilane
from devflow.loadconfig import Config, UiCfg
from devflow.schema import SchemaError
from devflow.schema import errors as schema_errors
from devflow.states import Limits

ROOT = Path(__file__).resolve().parent.parent


def make_cfg(root: Path, **ui) -> Config:
    return Config(
        root=root, name="t", default_branch="main", remote="", worktree_root=root / "wts",
        limits=Limits(), stage_timeout_minutes=5, gate_timeout_minutes=1, human_gates={},
        agents={}, projects=[], github={}, ui=UiCfg(**ui))


def case(id="c1", uc=("UC1",), ok=True, **kw) -> dict:
    return {"id": id, "use_case_ids": list(uc), "ok": ok, "duration_s": 0.1, "errors": [],
            "console": [], "page_errors": [], "network_failures": [], "screenshot": None,
            "dom": None, **kw}


def stub(tmp_path: Path, body: str, name: str = "stub.py") -> str:
    path = tmp_path / name
    path.write_text(body)
    return f'"{sys.executable}" {path}'


WRITE = ("import json, os\n"
         "json.dump({data}, open(os.environ['DEVFLOW_UI_REPORT'], 'w'))\n")


def writes(tmp_path: Path, data: object, extra: str = "") -> str:
    return stub(tmp_path, WRITE.format(data=repr(data)) + extra)


# ---------------------------------------------------------------- which files are UI (AC1)


@pytest.mark.parametrize("path", [
    "src/devflow/ui.py", "site/index.md", "site/a/b.png", "web/static/app.dat", "static/x.txt",
    "index.html", "a/b/c.css", "x.js", "y.jsx", "deep/z.ts", "w.tsx", "v.vue", "s.svelte"])
def test_touches_ui_default_matches(path: str):
    assert uilane.touches_ui([path]) == [path]


@pytest.mark.parametrize("path", [
    "src/devflow/engine.py", "tests/test_gates.py", "src/devflow/templates/coder.md",
    "src/devflow/templates/ui_evaluator.md", "README.md", "src/devflow/uilane.py",
    "pyproject.toml"])
def test_touches_ui_default_ignores_backend(path: str):
    assert uilane.touches_ui([path]) == []


def test_touches_ui_returns_exactly_the_matches_and_handles_empty():
    files = ["src/devflow/engine.py", "a.css", "tests/test_gates.py", "site/x"]
    assert uilane.touches_ui(files) == ["a.css", "site/x"]
    assert uilane.touches_ui([]) == []


def test_custom_patterns_override_the_defaults():
    assert uilane.touches_ui(["a.css", "web/app.py"], ["web/**"]) == ["web/app.py"]
    assert uilane.touches_ui(["a.css"], []) == []


# ---------------------------------------------------------------- plan validation (AC3)

CRITERIA = [{"id": "AC1", "text": "a"}, {"id": "AC2", "text": "b"}]


def uc(i, **kw):
    return {"id": f"UC{i}", "title": f"t{i}", "kind": "happy_path", "description": "d",
            "criterion_ids": ["AC1"], **kw}


def plan(n=3, files=("site/index.html",), **kw):
    p = {"status": "ready", "goal": "g", "acceptance_criteria": CRITERIA,
         "steps": [{"id": "S1", "description": "d", "files": list(files)}],
         "ui_use_cases": [uc(i) for i in range(1, n + 1)]}
    p.update(kw)
    return p


@pytest.fixture
def ui_cfg(tmp_path: Path) -> Config:
    return make_cfg(tmp_path, enabled=True, command="x")


@pytest.mark.parametrize("n", [3, 6])
def test_validate_accepts_three_to_six(ui_cfg, n):
    assert uilane.validate_use_cases(plan(n), ui_cfg) == []


@pytest.mark.parametrize("n", [0, 2, 7])
def test_validate_rejects_wrong_counts(ui_cfg, n):
    assert "3-6" in " ".join(uilane.validate_use_cases(plan(n), ui_cfg))


def test_validate_rejects_bad_members(ui_cfg):
    bad = plan(3)
    bad["ui_use_cases"][0]["criterion_ids"] = ["AC9"]
    assert "unknown criteria AC9" in " ".join(uilane.validate_use_cases(bad, ui_cfg))
    bad = plan(3)
    bad["ui_use_cases"][1]["id"] = " "
    assert "non-empty id" in " ".join(uilane.validate_use_cases(bad, ui_cfg))
    bad = plan(3)
    bad["ui_use_cases"][1]["description"] = ""
    assert "non-empty description" in " ".join(uilane.validate_use_cases(bad, ui_cfg))
    bad = plan(3)
    bad["ui_use_cases"][2]["kind"] = "weird"
    assert "kind" in " ".join(uilane.validate_use_cases(bad, ui_cfg))
    bad = plan(3)
    bad["ui_use_cases"][2]["id"] = "UC1"
    assert "duplicated" in " ".join(uilane.validate_use_cases(bad, ui_cfg))


def test_validate_does_not_require_use_cases_for_backend_plans_or_when_disabled(
        ui_cfg, tmp_path):
    backend = plan(0, files=("src/devflow/engine.py",))
    assert uilane.validate_use_cases(backend, ui_cfg) == []
    assert uilane.validate_use_cases(plan(0), make_cfg(tmp_path)) == []
    assert uilane.validate_use_cases({"status": "needs_clarification"}, ui_cfg) == []


def test_plan_schema_accepts_with_and_without_use_cases_and_checks_shape():
    base = {k: v for k, v in plan().items() if k != "ui_use_cases"}
    assert schema_errors("plan", base) == []
    assert schema_errors("plan", plan(3)) == []
    bad = plan(3)
    bad["ui_use_cases"][0]["kind"] = "nope"
    assert schema_errors("plan", bad)
    bad = plan(3)
    del bad["ui_use_cases"][0]["description"]
    assert schema_errors("plan", bad)


def test_templates_teach_the_ui_case_convention():
    for name in ("planner", "coder"):
        text = (ROOT / "src/devflow/templates" / f"{name}.md").read_text()
        assert "ui_use_cases" in text or "ui_case" in text
    assert "ui_case" in (ROOT / "src/devflow/templates/coder.md").read_text()


# ---------------------------------------------------------------- report parsing (AC6)


def test_report_round_trips(tmp_path: Path):
    data = {"cases": [case(console=[{"type": "error", "text": "x"}], page_errors=["boom"],
                           network_failures=[{"url": "/a", "status": 500}],
                           screenshot="/s.png", dom="/d.html")]}
    path = tmp_path / "r.json"
    path.write_text(json.dumps(data))
    report = uilane.load_report(path)
    assert report.to_dict() == data
    assert report.cases[0].network_failures[0]["status"] == 500


@pytest.mark.parametrize("text, message", [
    ("not json", "not valid JSON"),
    ("[]", "JSON object"),
    ("{}", "missing 'cases'"),
    ('{"cases": 3}', "must be a list"),
    ('{"cases": [3]}', "must be an object"),
    ('{"cases": [{"id": "x"}]}', "missing"),
])
def test_report_rejects_bad_shapes(text, message):
    with pytest.raises(uilane.ReportError, match=message):
        uilane.parse_report(text)


@pytest.mark.parametrize("field, value, message", [
    ("ok", "yes", "ok must be a boolean"),
    ("errors", "bad", "errors must be a list"),
    ("duration_s", True, "duration_s"),
    ("id", "", "id"),
    ("use_case_ids", [1], "use_case_ids"),
    ("console", ["x"], "console"),
    ("network_failures", [{"status": 1}], "network_failures"),
    ("screenshot", 5, "screenshot"),
])
def test_report_rejects_bad_case_fields(field, value, message):
    with pytest.raises(uilane.ReportError, match=message):
        uilane.parse_report(json.dumps({"cases": [case(**{field: value})]}))


def test_load_report_missing_file(tmp_path: Path):
    with pytest.raises(uilane.ReportError, match="cannot read"):
        uilane.load_report(tmp_path / "nope.json")


# ---------------------------------------------------------------- running the lane (AC4, AC6)

USE_CASES = [{"id": "UC1", "title": "t"}]


def run(tmp_path: Path, command: str, use_cases=USE_CASES, **ui) -> uilane.UiLaneResult:
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    return uilane.run_lane(make_cfg(tmp_path, enabled=True, command=command, **ui), wt,
                           use_cases, timeout_s=ui.get("timeout_s"))


def test_free_port_is_bindable():
    import socket
    port = uilane.free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_lane_env_cwd_and_scrubbing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "sekret")
    monkeypatch.setenv("PLAIN", "ok")
    wt = tmp_path / "wt"
    wt.mkdir()
    out = tmp_path / "seen.json"
    body = ("import json, os\n"
            f"json.dump({{'cwd': os.getcwd(), 'key': os.environ.get('MY_API_KEY'),"
            " 'plain': os.environ.get('PLAIN'), 'rep': os.environ['DEVFLOW_UI_REPORT'],"
            " 'art': os.environ['DEVFLOW_UI_ARTIFACTS'], 'port': os.environ['DEVFLOW_UI_PORT']},"
            f" open({str(out)!r}, 'w'))\n"
            "json.dump({'cases': []}, open(os.environ['DEVFLOW_UI_REPORT'], 'w'))\n")
    uilane.run_lane(make_cfg(tmp_path, enabled=True, command=stub(tmp_path, body)), wt, [])
    seen = json.loads(out.read_text())
    assert Path(seen["cwd"]).resolve() == wt.resolve()
    assert seen["key"] is None and seen["plain"] == "ok"
    for key in ("rep", "art"):
        assert not Path(seen[key]).resolve().is_relative_to(wt.resolve())
    assert Path(seen["art"]).is_dir() and 0 < int(seen["port"]) < 65536


def test_lane_passes_when_all_cases_ok_and_use_cases_covered(tmp_path):
    result = run(tmp_path, writes(tmp_path, {"cases": [case()]}))
    assert result.ok and result.status == "passed" and result.reasons == []
    assert result.report.cases[0].id == "c1"


def test_lane_without_a_command_is_unconfigured(tmp_path):
    result = uilane.run_lane(make_cfg(tmp_path, enabled=True), tmp_path, USE_CASES)
    assert result.status == "unconfigured" and not result.ran and not result.ok


def test_lane_fails_on_nonzero_exit(tmp_path):
    cmd = writes(tmp_path, {"cases": [case()]}, "print('kaput')\nraise SystemExit(3)\n")
    result = run(tmp_path, cmd)
    assert not result.ok and "exited with code 3" in " ".join(result.reasons)
    assert "kaput" in result.details


def test_lane_fails_on_timeout(tmp_path):
    marker = tmp_path / "child-alive"
    child = tmp_path / "child.py"
    child.write_text(f"import time, pathlib\ntime.sleep(1.5)\n"
                     f"pathlib.Path({str(marker)!r}).write_text('x')\n")
    body = (f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(child)!r}])\n"
            "time.sleep(30)\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    result = uilane.run_lane(make_cfg(tmp_path, enabled=True, command=stub(tmp_path, body)),
                             wt, USE_CASES, timeout_s=0.5)
    assert result.timed_out and "timed out" in " ".join(result.reasons)
    time.sleep(2)
    assert not marker.exists()  # the whole process group was killed


def test_lane_fails_without_a_report(tmp_path):
    result = run(tmp_path, stub(tmp_path, "pass\n"))
    assert "wrote no report" in " ".join(result.reasons)


def test_lane_fails_on_invalid_report(tmp_path):
    result = run(tmp_path, stub(
        tmp_path, "import os\nopen(os.environ['DEVFLOW_UI_REPORT'], 'w').write('{bad')\n"))
    assert "invalid report" in " ".join(result.reasons)


def test_lane_fails_naming_the_failing_case_and_errors(tmp_path):
    bad = case("tests/x.py::bad", ok=False, errors=["test failed: nope"],
               console=[{"type": "error", "text": "kaboom"}])
    result = run(tmp_path, writes(tmp_path, {"cases": [bad, case("fine", uc=["UC2"])]}))
    joined = " ".join(result.reasons)
    assert "case tests/x.py::bad failed" in joined
    assert "kaboom" in result.details and "nope" in result.details


def test_a_case_marked_ok_but_with_captured_errors_still_fails(tmp_path):
    sneaky = case(ok=True, page_errors=["uncaught"])
    result = run(tmp_path, writes(tmp_path, {"cases": [sneaky]}))
    assert not result.ok and "uncaught" in result.details


def test_lane_fails_on_uncovered_use_case(tmp_path):
    result = run(tmp_path, writes(tmp_path, {"cases": [case(uc=["UC1"])]}),
                 use_cases=[{"id": "UC1"}, {"id": "UC9"}])
    assert "covered by no case: UC9" in " ".join(result.reasons)


def test_lane_fails_loudly_when_the_browser_is_unavailable(tmp_path):
    result = run(tmp_path, writes(tmp_path, {"cases": [], "unavailable": "chromium missing"}))
    assert not result.ok
    assert "UI lane unavailable: chromium missing" in " ".join(result.reasons)


def test_lane_with_no_cases_never_passes(tmp_path):
    assert not run(tmp_path, writes(tmp_path, {"cases": []}), use_cases=[]).ok


# ---------------------------------------------------------------- table, digest, veto (AC9, AC10)


def report_of(*cases) -> uilane.UiLaneReport:
    return uilane.parse_report(json.dumps({"cases": list(cases)}))


MATRIX = [{"id": "UC1", "title": "Board", "kind": "happy_path"},
          {"id": "UC2", "title": "Drawer", "kind": "error_state"}]


def test_format_table_columns_and_status():
    table = uilane.format_table(MATRIX, report_of(case(uc=["UC1"])))
    assert "Feature Use-Case Verification" in table
    assert "| Use case | Title | Kind | Cases | Status | Evidence |" in table
    assert "| UC1 | Board | happy_path | c1 | pass |" in table
    assert "| UC2 | Drawer | error_state | - | missing |" in table


def test_format_table_uses_evaluation_and_escapes_cells():
    evaluation = {"use_cases": [{"id": "UC1", "status": "fail", "case_ids": ["a|b"],
                                 "evidence": "line1\nline2 | pipe " + "x" * 400}]}
    table = uilane.format_table(MATRIX[:1], report_of(case()), evaluation)
    row = table.splitlines()[-1]
    assert "\n" not in row and "a\\|b" in row and "pipe" in row and row.endswith("... |")
    assert "| fail |" in row


def test_digest_is_bounded():
    huge = "A" * 10_000
    many = [case(f"c{i}", uc=[f"UC{i}"], ok=False, errors=[huge] * 20,
                 console=[{"type": "error", "text": huge}] * 20, dom=huge,
                 page_errors=[huge] * 20, network_failures=[{"url": huge, "status": 500}] * 20)
            for i in range(60)]
    digest = uilane.bounded_digest(report_of(*many))
    assert len(digest) <= uilane.DIGEST_CHARS + 40
    assert uilane.bounded_digest(None) == "(no report)"
    short = uilane.bounded_digest(report_of(case()))
    assert "case c1 PASS" in short and "UC1" in short


def evaluation(*pairs, findings=()):
    return {"use_cases": [{"id": i, "status": s, "case_ids": ["c1"], "evidence": "e"}
                          for i, s in pairs], "findings": list(findings), "notes": ""}


def test_reconcile_blocks_fail_missing_and_blocking_findings():
    rep = report_of(case(uc=["UC1", "UC2"]))
    fixed, blocking = uilane.reconcile(MATRIX, rep, evaluation(("UC1", "pass"), ("UC2", "fail")))
    assert [b["id"] for b in blocking] == ["ui-use-case-UC2"]
    assert all(b["blocking"] for b in blocking)
    _, blocking = uilane.reconcile(MATRIX, rep, evaluation(("UC1", "pass")))
    assert any("UC2" in b["summary"] and "missing" in b["summary"] for b in blocking)
    finding = {"id": "F1", "severity": "major", "blocking": True, "summary": "layout broken"}
    _, blocking = uilane.reconcile(
        MATRIX, rep, evaluation(("UC1", "pass"), ("UC2", "pass"), findings=[finding]))
    assert [b["summary"] for b in blocking] == ["layout broken"]
    nit = {**finding, "blocking": False}
    _, blocking = uilane.reconcile(
        MATRIX, rep, evaluation(("UC1", "pass"), ("UC2", "pass"), findings=[nit]))
    assert blocking == []


def test_reconcile_corrects_an_unsupported_pass_to_missing():
    rep = report_of(case(uc=["UC1"]), case("bad", uc=["UC2"], ok=False))
    fixed, blocking = uilane.reconcile(MATRIX, rep, evaluation(("UC1", "pass"), ("UC2", "pass")))
    statuses = {u["id"]: u["status"] for u in fixed["use_cases"]}
    assert statuses == {"UC1": "pass", "UC2": "missing"}
    assert len(blocking) == 1 and "UC2" in blocking[0]["summary"]
    fixed, _ = uilane.reconcile(MATRIX[:1], report_of(), evaluation(("UC1", "pass")))
    assert fixed["use_cases"][0]["status"] == "missing"


def test_ui_evaluation_schema():
    good = evaluation(("UC1", "pass"), findings=[
        {"id": "F", "severity": "nit", "blocking": False, "summary": "s"}])
    assert schema_errors("ui_evaluation", good) == []
    assert schema_errors("ui_evaluation", {**good, "notes": "n"}) == []
    bad_status = evaluation(("UC1", "maybe"))
    assert schema_errors("ui_evaluation", bad_status)
    assert schema_errors("ui_evaluation", {"use_cases": []})
    assert schema_errors("ui_evaluation", {"use_cases": [{"id": "x"}], "findings": []})
    assert SchemaError  # imported for the failure type used by callers


# ---------------------------------------------------------------- Playwright-free imports (AC11)


def test_modules_import_without_playwright():
    code = ("import sys\nsys.modules['playwright'] = None\n"
            "sys.modules['playwright.sync_api'] = None\n"
            "import devflow.uilane, devflow.uirunner\nprint('ok')\n")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=ROOT)
    assert proc.returncode == 0 and "ok" in proc.stdout, proc.stderr


def test_ui_directory_is_ignored_by_default_and_collected_on_request():
    conftest = ROOT / "tests" / "conftest.py"
    code = ("import os, runpy, sys\n"
            f"ns = runpy.run_path({str(conftest)!r})\n"
            "print(ns['collect_ignore_glob'])\n")
    for flag, expected in (("", ["ui/*"]), ("1", [])):
        env = {**os.environ, "DEVFLOW_UI_LANE": flag}
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              env=env, cwd=ROOT)
        assert proc.returncode == 0, proc.stderr
        assert eval(proc.stdout.strip()) == expected


# ---------------------------------------------------------------- the shipped scenarios (AC4)


def test_dashboard_scenarios_produce_a_valid_report(tmp_path: Path):
    pytest.importorskip("playwright.sync_api")
    report = tmp_path / "report.json"
    art = tmp_path / "art"
    art.mkdir()
    env = {**os.environ, "DEVFLOW_UI_LANE": "1", "DEVFLOW_UI_REPORT": str(report),
           "DEVFLOW_UI_ARTIFACTS": str(art)}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/ui", "-p", "devflow.uirunner", "-q",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=300)
    if "UI lane unavailable" in proc.stdout:
        pytest.skip("Playwright browsers are not installed")
    assert proc.returncode == 0, proc.stdout[-3000:]
    loaded = uilane.load_report(report)
    assert all(c.ok for c in loaded.cases)
    assert {u for c in loaded.cases for u in c.use_case_ids} >= {"UC1", "UC2", "UC3", "UC4", "UC5"}


# ---------------------------------------------------------------- docs and CI (AC11)


def test_docs_and_ci():
    doc = (ROOT / "docs" / "ui-lane.md").read_text()
    for needle in ("[ui]", "paths", "command", "timeout_minutes", "DEVFLOW_UI_REPORT",
                   "DEVFLOW_UI_ARTIFACTS", "DEVFLOW_UI_PORT", "ui_case", "devflow init --force"):
        assert needle in doc, needle
    for name in ("README.md", "CONTRIBUTING.md"):
        assert "docs/ui-lane.md" in (ROOT / name).read_text(), name
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    jobs: dict[str, str] = {}
    current = ""
    for line in ci.splitlines():
        if line.startswith("  ") and not line.startswith("   ") and line.strip().endswith(":"):
            current = line.strip()[:-1]
            jobs[current] = ""
        elif current:
            jobs[current] += line + "\n"
    assert "ui-lane" in jobs and "test" in jobs
    assert "playwright install" in jobs["ui-lane"] and "chromium" in jobs["ui-lane"]
    assert "DEVFLOW_UI_LANE=1" in jobs["ui-lane"]
    assert "playwright" not in jobs["test"] and "ruff check" in jobs["test"]
