"""Pure helpers for stage latency percentiles and post-completion stall detection."""
from __future__ import annotations

import json
import math
from pathlib import Path

MIN_SAMPLES = 5


def percentile(values, q):
    """Percentile q (0-100) by linear interpolation between closest ranks; 0.0 for no samples."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * q / 100
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[low])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (position - low))


def _attrs(record):
    attributes = record.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _seconds(record):
    start, end = record.get("start_ns"), record.get("end_ns")
    if not (_is_number(start) and _is_number(end)):
        return None
    try:
        start, end = float(start), float(end)
        seconds = (end - start) / 1e9
    except OverflowError:
        return None
    if not (math.isfinite(start) and math.isfinite(end) and math.isfinite(seconds)) or end < start:
        return None
    return seconds


def _is_synthetic(record):
    attributes = _attrs(record)
    return (attributes.get("langfuse.trace.metadata.synthetic") == "true"
            or attributes.get("langfuse.session.id") == "devflow-connectivity-demo")


def read_log_records(path):
    """JSONL object records from path; a missing/unreadable file or bad lines yield nothing for them."""
    records = []
    try:
        text = Path(path).read_text()
    except (OSError, ValueError):
        return records
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            records.append(row)
    return records


def baseline_durations(records, stage):
    """Durations of non-synthetic, status-ok spans named exactly `stage`."""
    found = []
    for record in records:
        if not isinstance(record, dict) or record.get("name") != stage or record.get("status") != "ok":
            continue
        if _is_synthetic(record):
            continue
        seconds = _seconds(record)
        if seconds is not None:
            found.append(seconds)
    return found


def check_stall(stage, elapsed_seconds, history):
    """None, or {stage, elapsed, p99, samples} when elapsed exceeds p99 of at least MIN_SAMPLES."""
    try:
        values = [v for v in history if _is_number(v) and math.isfinite(v)]
        if len(values) < MIN_SAMPLES or not _is_number(elapsed_seconds) or not math.isfinite(elapsed_seconds):
            return None
        p99 = round(percentile(values, 99), 3)
        if elapsed_seconds > p99:
            return {"stage": stage, "elapsed": round(elapsed_seconds, 3), "p99": p99, "samples": len(values)}
    except (TypeError, ValueError):
        return None
    return None
