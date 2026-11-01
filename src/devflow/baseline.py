"""Judge gates against the base commit, so pre-existing failures do not block every ticket.

Some repos are already red on their default branch (the Prediction Markets repo has over 200
`ruff` errors that CI never runs). A ticket should be blamed only for failures it introduces.
Each failing gate is reduced to fingerprints (a failing test id, or a rule code in a file);
a gate passes if every fingerprint it shows was already present, at the same count, on the base.
Output we cannot parse is fingerprinted by its normalised text, so unchanged output is
pre-existing and anything different is new (the conservative direction).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import worktree
from .gates import GateReport, GateResult, run_gates, setup_worktree
from .loadconfig import Config
from .store import Store

_PYTEST = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)
_RUFF_FULL = re.compile(r"^\s*([A-Z]{1,4}\d{2,4})\b[^\n]*\n\s*-->\s*(\S+?):\d+:\d+", re.MULTILINE)
_RUFF_CONCISE = re.compile(r"^(\S+?):\d+:\d+: ([A-Z]{1,4}\d{2,4})\b", re.MULTILINE)
_TSC = re.compile(r"^(\S+?)\(\d+,\d+\): error (TS\d+)", re.MULTILINE)
_DIGITS = re.compile(r"\d+")
_GENERIC = re.compile(r"^(\S+?):\d+(?::\d+)?: (?:error|warning):\s*(.+)$", re.MULTILINE)


def fingerprints(result: GateResult) -> Counter[str]:
    """What exactly is failing, independent of line numbers and timing."""
    if result.ok:
        return Counter()
    text = result.output_tail
    found: Counter[str] = Counter()
    for test in _PYTEST.findall(text):
        found[f"test:{test.split(' - ')[0]}"] += 1
    for code, path in _RUFF_FULL.findall(text):
        found[f"rule:{path}:{code}"] += 1
    for path, code in _RUFF_CONCISE.findall(text):
        found[f"rule:{path}:{code}"] += 1
    for path, code in _TSC.findall(text):
        found[f"rule:{path}:{code}"] += 1
    for path, message in _GENERIC.findall(text):
        normalised_message = _DIGITS.sub("N", message.strip())[:80]
        found[f"msg:{path}:{normalised_message}"] += 1
    if result.timed_out:
        found["timeout"] += 1
    if not found:
        normalised = re.sub(r"\d+(?:\.\d+)?", "N", text.strip()[-1500:])
        found[f"opaque:{hashlib.sha1(normalised.encode()).hexdigest()[:12]}"] += 1
    return found


@dataclass
class NewFailure:
    project: str
    gate: str
    command: str
    new: list[str]
    output_tail: str


@dataclass
class Verdict:
    ok: bool
    new_failures: list[NewFailure] = field(default_factory=list)
    preexisting: int = 0
    unconfigured: list[str] = field(default_factory=list)
    nothing_ran: bool = False

    def summary(self) -> str:
        if self.nothing_ran:
            return "no gates are configured, so nothing could be verified"
        if self.ok:
            tail = f" ({self.preexisting} failure(s) already present on the base, ignored)" \
                if self.preexisting else ""
            return "all gates pass" + tail
        lines = []
        for f in self.new_failures:
            shown = ", ".join(f.new[:5]) + (" ..." if len(f.new) > 5 else "")
            lines.append(f"- {f.project}:{f.gate} `{f.command}` has new failures: {shown}")
        return "\n".join(lines)


def evaluate(now: GateReport, base: GateReport | None) -> Verdict:
    if not now.results:
        return Verdict(ok=False, unconfigured=now.missing, nothing_ran=True)
    base_by_key = {(r.project, r.gate, r.command): r for r in (base.results if base else [])}
    verdict = Verdict(ok=True, unconfigured=list(now.missing))
    for r in now.results:
        if r.ok:
            continue
        before = base_by_key.get((r.project, r.gate, r.command))
        base_fp = fingerprints(before) if before is not None else Counter()
        now_fp = fingerprints(r)
        new = [k for k, n in now_fp.items() if n > base_fp.get(k, 0)]
        verdict.preexisting += sum(min(n, base_fp.get(k, 0)) for k, n in now_fp.items())
        if new:
            verdict.ok = False
            verdict.new_failures.append(
                NewFailure(r.project, r.gate, r.command, sorted(new), r.output_tail))
    return verdict


# ---------------------------------------------------------------- caching the base run


def report_to_dict(report: GateReport) -> dict:
    return {"results": [asdict(r) for r in report.results], "missing": report.missing}


def report_from_dict(data: dict) -> GateReport:
    return GateReport([GateResult(**r) for r in data["results"]], list(data["missing"]))


def gate_digest(cfg: Config) -> str:
    spec = [(p.path, p.setup, sorted(p.gates.items())) for p in cfg.projects]
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


def ensure_baseline(cfg: Config, store: Store, base_sha: str) -> GateReport:
    """Gate results on the base commit, computed once per (commit, gate config)."""
    key = f"{base_sha}:{gate_digest(cfg)}"
    cached = store.get_baseline(key)
    if cached is not None:
        return report_from_dict(cached)
    path = _baseline_path(cfg, base_sha)
    worktree.create_detached(cfg.root, path, base_sha)
    setup = setup_worktree(cfg, path)
    if not setup.ok and setup.results:
        report = GateReport(setup.results, [])
    else:
        report = run_gates(cfg, path)
    store.set_baseline(key, report_to_dict(report))
    return report


def _baseline_path(cfg: Config, base_sha: str) -> Path:
    return cfg.worktree_root / f"_baseline-{base_sha[:8]}"
