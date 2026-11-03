import json

import pytest

from devflow import stall, telemetry
from devflow.stall import MIN_SAMPLES, baseline_durations, check_stall, percentile, read_log_records
from devflow.telemetry_cli import _stage_stats

S = 1_000_000_000


def rec(name="stage.plan", dur=1, status="ok", **attrs):
    return {"name": name, "status": status, "start_ns": 0, "end_ns": int(dur * S), "attributes": attrs}


def test_percentile_pinned_values():
    assert [percentile([5], q) for q in (50, 90, 99)] == [5, 5, 5]
    assert [round(percentile([1, 2], q), 3) for q in (50, 90, 99)] == [1.5, 1.9, 1.99]
    assert [round(percentile([5, 1, 3, 2, 4], q), 3) for q in (50, 90, 99)] == [3, 4.6, 4.96]
    hundred = list(range(100, 0, -1))
    assert [round(percentile(hundred, q), 3) for q in (50, 90, 99)] == [50.5, 90.1, 99.01]
    assert percentile([], 99) == 0.0


def test_stage_stats_rounds_and_shares_percentile():
    assert _stage_stats([1, 2]) == {"count": 2, "mean": 1.5, "max": 2.0, "p50": 1.5, "p90": 1.9, "p99": 1.99}
    from devflow import telemetry_cli
    assert telemetry_cli.percentile is stall.percentile


def test_check_stall_boundaries():
    assert check_stall("s", 1000, [1, 1, 1, 1]) is None
    assert check_stall("s", 1000, []) is None
    five = [1, 2, 3, 4, 5]
    assert check_stall("s", 4.96, five) is None
    assert check_stall("s", 4.0, five) is None
    assert check_stall("s", 10, five) == {"stage": "s", "elapsed": 10, "p99": 4.96, "samples": 5}
    assert MIN_SAMPLES == 5


def test_baseline_durations_filters():
    records = [rec(dur=2), rec(dur=3, status="waiting"), rec(dur=4, status="error"),
               rec(dur=5, **{"langfuse.trace.metadata.synthetic": "true"}),
               rec("stage.other", dur=6), rec("stall.detected", dur=7),
               {"name": "stage.plan", "status": "ok", "start_ns": float("nan"), "end_ns": 5},
               {"name": "stage.plan", "status": "ok", "start_ns": 0, "end_ns": float("inf")},
               {"name": "stage.plan", "status": "ok", "start_ns": "x", "end_ns": 1}, "junk"]
    assert baseline_durations(records, "stage.plan") == [2.0]


def test_read_log_records_tolerant(tmp_path):
    assert read_log_records(tmp_path / "missing") == []
    assert read_log_records(tmp_path) == []  # a directory is unreadable
    path = tmp_path / "log"
    path.write_text('{"a": 1}\n\nnot json\n[1]\n"x"\n{"b": 2}\n')
    assert read_log_records(path) == [{"a": 1}, {"b": 2}]


def test_log_path_and_report_stall(tmp_path, capsys):
    try:
        telemetry.configure(settings={})
        assert telemetry.log_path() is None
        telemetry.configure(settings={"endpoint": "http://x", "secret_key": "s"})
        assert telemetry.log_path() is None
        log = tmp_path / "t.jsonl"
        telemetry.configure(settings={"log_file": str(log), "secret_key": "topsecret"})
        assert telemetry.log_path() == log
        telemetry.configure(settings={"log_file": str(log), "enabled": False})
        assert telemetry.log_path() is None
        telemetry.configure(settings={"log_file": str(log)})
        telemetry.report_stall({"stage": "stage.plan", "elapsed": 9.5, "p99": 2.0, "samples": 6})
    finally:
        telemetry.configure(settings={})
    err = capsys.readouterr().err.splitlines()
    assert len([line for line in err if line.startswith("devflow: stall warning:")]) == 1
    (row,) = [json.loads(x) for x in log.read_text().splitlines()]
    assert row["name"] == "stall.detected" and not row["name"].startswith("stage.")
    attrs = row["attributes"]
    assert (attrs["devflow.stage"], attrs["devflow.duration_seconds"],
            attrs["devflow.threshold_p99_seconds"], attrs["devflow.baseline_samples"]) == ("stage.plan", 9.5, 2.0, 6)


@pytest.mark.parametrize("bad", [None, "x"])
def test_check_stall_bad_elapsed(bad):
    assert check_stall("s", bad, [1, 2, 3, 4, 5]) is None
