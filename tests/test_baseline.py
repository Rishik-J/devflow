from collections import Counter
from pathlib import Path

from devflow import baseline
from devflow.baseline import evaluate, fingerprints
from devflow.gates import GateReport, GateResult
from devflow.loadconfig import Config, ProjectCfg
from devflow.states import Limits
from devflow.store import Store, state_db_path


def result(output: str, *, ok: bool = False, gate: str = "lint", cmd: str = "c",
           timed_out: bool = False, project: str = ".") -> GateResult:
    return GateResult(project=project, gate=gate, command=cmd, ok=ok,
                      exit_code=0 if ok else 1, duration_s=0.1, timed_out=timed_out,
                      output_tail=output)


def report(*results: GateResult, missing=()) -> GateReport:
    return GateReport(list(results), list(missing))


RUFF_FULL = """E712 Avoid equality comparisons to `True`
   --> src/a.py:12:5
    |
 12 |     x == True
    |
RUF100 [*] Unused `noqa` directive
   --> src/b.py:3:1
Found 2 errors.
"""


def test_passing_gate_has_no_fingerprints():
    assert fingerprints(result("", ok=True)) == Counter()


def test_pytest_failures_are_identified_by_test_id():
    out = ("FAILED tests/test_a.py::test_x - assert 1 == 2\n"
           "ERROR tests/test_b.py::test_y - fixture missing\n")
    assert fingerprints(result(out, gate="unit")) == Counter(
        {"test:tests/test_a.py::test_x": 1, "test:tests/test_b.py::test_y": 1})


def test_ruff_full_and_concise_formats_ignore_line_numbers():
    full = fingerprints(result(RUFF_FULL))
    assert full == Counter({"rule:src/a.py:E712": 1, "rule:src/b.py:RUF100": 1})
    concise = fingerprints(result("src/a.py:99:1: E712 Avoid equality\nsrc/b.py:7:2: RUF100 x\n"))
    assert concise == full


def test_tsc_and_generic_messages():
    assert fingerprints(result("src/x.ts(10,5): error TS2322: bad", gate="typecheck")) == Counter(
        {"rule:src/x.ts:TS2322": 1})
    out = "src/m.py:10: error: Incompatible types at 5 (expected int)"
    fp = fingerprints(result(out, gate="typecheck"))
    assert fp == Counter({"msg:src/m.py:Incompatible types at N (expected int)": 1})


def test_unparseable_output_is_fingerprinted_by_normalised_text():
    a = fingerprints(result("something broke after 12.5s on line 40"))
    b = fingerprints(result("something broke after 3.1s on line 7"))
    c = fingerprints(result("a different failure entirely"))
    assert a == b and a != c and next(iter(a)).startswith("opaque:")


def test_timeouts_are_fingerprinted():
    assert fingerprints(result("", timed_out=True))["timeout"] == 1


# ---------------------------------------------------------------- evaluate


def test_failures_already_on_the_base_are_ignored():
    base = report(result(RUFF_FULL))
    now = report(result(RUFF_FULL.replace("12:5", "40:5")))  # moved, not new
    v = evaluate(now, base)
    assert v.ok and v.preexisting == 2 and not v.new_failures
    assert "already present on the base" in v.summary()


def test_a_new_failure_in_a_failing_gate_is_new():
    base = report(result(RUFF_FULL))
    extra = RUFF_FULL + "F401 unused import\n   --> src/c.py:1:1\n"
    v = evaluate(report(result(extra)), base)
    assert not v.ok and v.new_failures[0].new == ["rule:src/c.py:F401"] and v.preexisting == 2
    assert "src/c.py:F401" in v.summary()


def test_more_of_the_same_error_counts_as_new():
    one = "src/a.py:1:1: E501 long\n"
    v = evaluate(report(result(one + one)), report(result(one)))
    assert not v.ok and v.new_failures[0].new == ["rule:src/a.py:E501"]


def test_fixing_some_preexisting_failures_is_fine():
    v = evaluate(report(result("src/a.py:1:1: E501 x\n")),
                 report(result("src/a.py:1:1: E501 x\nsrc/b.py:1:1: E501 x\n")))
    assert v.ok and v.preexisting == 1


def test_gate_that_passed_on_base_and_fails_now_is_new():
    v = evaluate(report(result("FAILED t.py::a", gate="unit")),
                 report(result("", ok=True, gate="unit")))
    assert not v.ok and v.new_failures[0].gate == "unit"


def test_no_baseline_means_every_failure_is_new():
    assert not evaluate(report(result("src/a.py:1:1: E501 x")), None).ok


def test_unchanged_opaque_failure_is_preexisting_but_changed_is_new():
    base = report(result("boom happened"))
    assert evaluate(report(result("boom happened")), base).ok
    assert not evaluate(report(result("a different boom")), base).ok


def test_different_commands_are_not_compared_with_each_other():
    base = report(result("src/a.py:1:1: E501 x", cmd="ruff check ."))
    v = evaluate(report(result("src/a.py:1:1: E501 x", cmd="ruff check src")), base)
    assert not v.ok


def test_nothing_ran_is_not_ok():
    v = evaluate(report(missing=["lint"]), None)
    assert not v.ok and v.nothing_ran and "nothing could be verified" in v.summary()


def test_all_passing():
    assert evaluate(report(result("", ok=True)), None).summary() == "all gates pass"


# ---------------------------------------------------------------- caching


def make_cfg(root: Path, tmp: Path, gates: dict) -> Config:
    return Config(
        root=root, name="r", default_branch="main", remote="", worktree_root=tmp / "wts",
        limits=Limits(), stage_timeout_minutes=5, gate_timeout_minutes=1, human_gates={},
        agents={}, projects=[ProjectCfg(".", "python", gates)], github={},
    )


def test_report_roundtrip():
    r = report(result("x", gate="unit"), missing=["lint"])
    again = baseline.report_from_dict(baseline.report_to_dict(r))
    assert again.results == r.results and again.missing == r.missing


def test_baseline_runs_once_on_a_pristine_checkout(base_repo: Path, tmp_path: Path):
    from tests.conftest import git

    cfg = make_cfg(base_repo, tmp_path, {"unit": ["echo ran >> ran.log; test ! -f dirty"]})
    sha = git(base_repo, "rev-parse", "HEAD")
    with Store(state_db_path(tmp_path)) as store:
        first = baseline.ensure_baseline(cfg, store, sha)
        assert first.ok
        wt = baseline._baseline_path(cfg, sha)
        assert (wt / "ran.log").read_text() == "ran\n"
        again = baseline.ensure_baseline(cfg, store, sha)  # cached: command not run again
        assert again.ok and (wt / "ran.log").read_text() == "ran\n"
        cfg2 = make_cfg(base_repo, tmp_path, {"unit": ["true"]})  # different gate config
        baseline.ensure_baseline(cfg2, store, sha)
        assert store._db.execute("SELECT COUNT(*) FROM baselines").fetchone()[0] == 2
        assert baseline.gate_digest(cfg) != baseline.gate_digest(cfg2)
