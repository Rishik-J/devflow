"""The UI lane: a deterministic headless-browser suite that runs next to the backend gates.

The lane activates only when `[ui]` is configured and the diff touches user-facing files. It runs
the repo's own `[ui] command` (no agent drives anything), reads the JSON report that command
writes, and turns it into facts: which cases ran, which use cases they cover, and every console
warning, uncaught error and failing request the browser saw. An LLM may later map those facts to
the planned use cases, but it can only add blockers (see `reconcile`), never remove one.

Report contract (written by the command to `$DEVFLOW_UI_REPORT`):
    {"cases": [{"id", "use_case_ids", "ok", "duration_s", "errors", "console", "page_errors",
                "network_failures", "screenshot", "dom"}], "unavailable": "optional reason"}
"""

from __future__ import annotations

import json
import re
import socket
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .gates import project_env, run_command
from .loadconfig import Config

DEFAULT_UI_PATTERNS = (
    "src/devflow/ui.py", "site/**", "**/static/**",
    "*.html", "*.css", "*.js", "*.jsx", "*.ts", "*.tsx", "*.vue", "*.svelte",
)
UNCONFIGURED_LABEL = "ui:browser lane"
USE_CASE_KINDS = ("happy_path", "state_persistence", "error_state", "responsive", "deep_link",
                  "other")
MIN_USE_CASES, MAX_USE_CASES = 3, 6

CELL_CHARS = 200
DIGEST_CHARS = 6000
DIGEST_CASES = 30
DIGEST_ITEMS = 5
ITEM_CHARS = 300
TABLE_TITLE = "Feature Use-Case Verification"


class ReportError(ValueError):
    """The lane's JSON report is missing a field or has the wrong shape."""


# ---------------------------------------------------------------- which files are user-facing


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _matches(path: str, pattern: str) -> bool:
    path = path.removeprefix("./")
    pattern = pattern.removeprefix("./")
    subject = path if "/" in pattern else PurePosixPath(path).name
    return bool(_glob_regex(pattern).match(subject))


def touches_ui(changed_files: list[str], patterns: list[str] | tuple[str, ...] | None = None
               ) -> list[str]:
    """The changed files that match a UI pattern. `patterns` None means the defaults."""
    active = DEFAULT_UI_PATTERNS if patterns is None else patterns
    return [f for f in changed_files if any(_matches(f, p) for p in active)]


# ---------------------------------------------------------------- the report


@dataclass
class UiCase:
    id: str
    use_case_ids: list[str] = field(default_factory=list)
    ok: bool = True
    duration_s: float = 0.0
    errors: list[str] = field(default_factory=list)
    console: list[dict] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    network_failures: list[dict] = field(default_factory=list)
    screenshot: str | None = None
    dom: str | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "use_case_ids": list(self.use_case_ids), "ok": self.ok,
                "duration_s": self.duration_s, "errors": list(self.errors),
                "console": list(self.console), "page_errors": list(self.page_errors),
                "network_failures": list(self.network_failures),
                "screenshot": self.screenshot, "dom": self.dom}


@dataclass
class UiLaneReport:
    cases: list[UiCase] = field(default_factory=list)
    unavailable: str = ""

    def to_dict(self) -> dict:
        data: dict = {"cases": [c.to_dict() for c in self.cases]}
        if self.unavailable:
            data["unavailable"] = self.unavailable
        return data


def _need(cond: bool, where: str, what: str) -> None:
    if not cond:
        raise ReportError(f"{where}: {what}")


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _parse_case(raw: object, index: int) -> UiCase:
    where = f"cases[{index}]"
    _need(isinstance(raw, dict), where, "must be an object")
    assert isinstance(raw, dict)
    for key in ("id", "use_case_ids", "ok", "duration_s", "errors", "console", "page_errors",
                "network_failures"):
        _need(key in raw, where, f"missing {key!r}")
    _need(isinstance(raw["id"], str) and bool(raw["id"]), where, "id must be a non-empty string")
    _need(isinstance(raw["use_case_ids"], list)
          and all(isinstance(x, str) for x in raw["use_case_ids"]),
          where, "use_case_ids must be a list of strings")
    _need(isinstance(raw["ok"], bool), where, "ok must be a boolean")
    _need(_is_number(raw["duration_s"]), where, "duration_s must be a number")
    _need(isinstance(raw["errors"], list) and all(isinstance(x, str) for x in raw["errors"]),
          where, "errors must be a list of strings")
    _need(isinstance(raw["console"], list)
          and all(isinstance(x, dict) and isinstance(x.get("type"), str)
                  and isinstance(x.get("text"), str) for x in raw["console"]),
          where, "console must be a list of {type, text} objects")
    _need(isinstance(raw["page_errors"], list)
          and all(isinstance(x, str) for x in raw["page_errors"]),
          where, "page_errors must be a list of strings")
    _need(isinstance(raw["network_failures"], list)
          and all(isinstance(x, dict) and isinstance(x.get("url"), str)
                  and (x.get("status") is None or (isinstance(x.get("status"), int)
                                                  and not isinstance(x.get("status"), bool)))
                  for x in raw["network_failures"]),
          where, "network_failures must be a list of {url, status} objects")
    for key in ("screenshot", "dom"):
        _need(raw.get(key) is None or isinstance(raw.get(key), str), where,
              f"{key} must be a string or null")
    return UiCase(
        id=raw["id"], use_case_ids=list(raw["use_case_ids"]), ok=raw["ok"],
        duration_s=float(raw["duration_s"]), errors=list(raw["errors"]),
        console=[dict(x) for x in raw["console"]], page_errors=list(raw["page_errors"]),
        network_failures=[dict(x) for x in raw["network_failures"]],
        screenshot=raw.get("screenshot"), dom=raw.get("dom"))


def parse_report(text: str) -> UiLaneReport:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ReportError(f"not valid JSON: {exc}") from exc
    _need(isinstance(data, dict), "report", "must be a JSON object")
    _need("cases" in data, "report", "missing 'cases'")
    _need(isinstance(data["cases"], list), "report", "'cases' must be a list")
    unavailable = data.get("unavailable", "")
    _need(isinstance(unavailable, str), "report", "'unavailable' must be a string")
    return UiLaneReport([_parse_case(c, i) for i, c in enumerate(data["cases"])], unavailable)


def load_report(path: Path) -> UiLaneReport:
    """Strictly parse the report file. Raises ReportError (also when it cannot be read)."""
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise ReportError(f"cannot read report: {exc}") from exc
    return parse_report(text)


# ---------------------------------------------------------------- running the lane


@dataclass
class UiLaneResult:
    status: str  # passed | failed | unconfigured
    command: str = ""
    reasons: list[str] = field(default_factory=list)
    details: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    duration_s: float = 0.0
    report: UiLaneReport | None = None
    report_dir: str = ""
    use_cases: list[dict] = field(default_factory=list)
    output_tail: str = ""

    @property
    def ran(self) -> bool:
        return self.status != "unconfigured"

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def to_evidence(self, case_cap: int = DIGEST_CASES) -> dict:
        cases = []
        for c in (self.report.cases if self.report else [])[:case_cap]:
            cases.append({"id": c.id, "use_case_ids": c.use_case_ids[:DIGEST_ITEMS * 2],
                          "ok": c.ok, "duration_s": c.duration_s,
                          "errors": [_clip(e, ITEM_CHARS) for e in c.errors[:DIGEST_ITEMS]]})
        return {"ran": self.ran, "ok": self.ok, "status": self.status, "command": self.command,
                "reasons": [_clip(r, ITEM_CHARS) for r in self.reasons[:20]],
                "duration_s": self.duration_s, "exit_code": self.exit_code,
                "timed_out": self.timed_out, "cases": cases,
                "report_dir": self.report_dir}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _clip(text: object, limit: int) -> str:
    s = str(text)
    return s if len(s) <= limit else s[:limit] + "..."


def _describe_case(case: UiCase) -> str:
    items = list(case.errors)
    items += [f"console {c.get('type')}: {c.get('text')}" for c in case.console]
    items += [f"page error: {e}" for e in case.page_errors]
    items += [_describe_network(n) for n in case.network_failures]
    shown = "; ".join(_clip(i, ITEM_CHARS) for i in items[:DIGEST_ITEMS]) or "failed"
    more = f" (+{len(items) - DIGEST_ITEMS} more)" if len(items) > DIGEST_ITEMS else ""
    return f"{case.id}: {shown}{more}"


def _describe_network(item: dict) -> str:
    status = item.get("status")
    detail = f"HTTP {status}" if status is not None else (item.get("failure") or "request failed")
    return f"network {detail} {item.get('url')}"


def run_lane(cfg: Config, worktree: Path, plan_use_cases: list[dict] | None = None,
             timeout_s: float | None = None) -> UiLaneResult:
    """Run `[ui] command` in `worktree` and judge its report. No agent is involved."""
    ui = cfg.ui
    use_cases = [u for u in (plan_use_cases or []) if isinstance(u, dict)]
    if not ui.command:
        return UiLaneResult(status="unconfigured", use_cases=use_cases)
    timeout = timeout_s if timeout_s is not None else ui.timeout_minutes * 60
    # Outside the worktree, so nothing the browser writes is ever committed.
    out_dir = Path(tempfile.mkdtemp(prefix="devflow-ui-"))
    report_path = out_dir / "report.json"
    artifacts = out_dir / "artifacts"
    artifacts.mkdir()
    env = project_env(worktree)
    env.update({"DEVFLOW_UI_REPORT": str(report_path), "DEVFLOW_UI_ARTIFACTS": str(artifacts),
                "DEVFLOW_UI_PORT": str(free_port())})
    start = time.monotonic()
    code, out, timed_out = run_command(ui.command, worktree, timeout, env)
    result = UiLaneResult(
        status="failed", command=ui.command, exit_code=code, timed_out=timed_out,
        duration_s=round(time.monotonic() - start, 2), report_dir=str(out_dir),
        use_cases=use_cases, output_tail=out[-2000:])
    reasons, details = result.reasons, []
    if timed_out:
        reasons.append(f"UI lane timed out after {timeout:g}s")
        result.details = out[-1500:]
        return result
    if code != 0:
        reasons.append(f"UI lane command exited with code {code}")
        details.append(out[-1500:])
    report: UiLaneReport | None = None
    if not report_path.exists():
        reasons.append("UI lane wrote no report (is DEVFLOW_UI_REPORT honoured by the command?)")
    else:
        try:
            report = load_report(report_path)
        except ReportError as exc:
            reasons.append(f"UI lane wrote an invalid report: {exc}")
    result.report = report
    if report is not None:
        if report.unavailable:
            reasons.append(f"UI lane unavailable: {report.unavailable}")
        failing = [c for c in report.cases if not c.ok or _case_errors(c)]
        for c in failing:
            reasons.append(f"case {c.id} failed")
            details.append(_describe_case(c))
        if not report.cases and not report.unavailable:
            reasons.append("UI lane report contains no cases")
        covered = {u for c in report.cases for u in c.use_case_ids}
        uncovered = [u["id"] for u in use_cases if u.get("id") and u["id"] not in covered]
        if uncovered:
            reasons.append("use case(s) covered by no case: " + ", ".join(map(str, uncovered)))
    if not reasons:
        result.status = "passed"
    result.details = "\n".join(details)
    return result


def _case_errors(case: UiCase) -> bool:
    """A case that carries captured browser errors fails even if it says ok."""
    return bool(case.errors or case.page_errors or case.network_failures
                or any(c.get("type") in ("warning", "error") for c in case.console))


# ---------------------------------------------------------------- the plan's use-case matrix


def plan_touches_ui(plan: dict, cfg: Config) -> bool:
    files = sorted({f for s in plan.get("steps", []) if isinstance(s, dict)
                    for f in s.get("files", []) if isinstance(f, str)})
    return bool(cfg.ui.enabled and touches_ui(files, cfg.ui.paths))


def validate_use_cases(plan: dict, cfg: Config) -> list[str]:
    """Problems with the plan's `ui_use_cases`; empty when fine or not required."""
    if plan.get("status") != "ready" or not plan_touches_ui(plan, cfg):
        return []
    cases = plan.get("ui_use_cases")
    if not isinstance(cases, list):
        cases = []
    problems: list[str] = []
    if not MIN_USE_CASES <= len(cases) <= MAX_USE_CASES:
        problems.append(f"a plan that touches user-facing files needs {MIN_USE_CASES}-"
                        f"{MAX_USE_CASES} `ui_use_cases`, got {len(cases)}")
    known = {a.get("id") for a in plan.get("acceptance_criteria", []) if isinstance(a, dict)}
    seen: set[str] = set()
    for i, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            problems.append(f"use case #{i} is not an object")
            continue
        cid = case.get("id")
        label = cid if isinstance(cid, str) and cid.strip() else f"#{i}"
        if not (isinstance(cid, str) and cid.strip()):
            problems.append(f"use case {label} needs a non-empty id")
        elif cid in seen:
            problems.append(f"use case id {cid} is duplicated")
        else:
            seen.add(cid)
        desc = case.get("description")
        if not (isinstance(desc, str) and desc.strip()):
            problems.append(f"use case {label} needs a non-empty description")
        if case.get("kind") not in USE_CASE_KINDS:
            problems.append(f"use case {label} has kind {case.get('kind')!r}; "
                            f"use one of {', '.join(USE_CASE_KINDS)}")
        crits = case.get("criterion_ids")
        if not (isinstance(crits, list) and all(isinstance(c, str) for c in crits)):
            problems.append(f"use case {label} needs criterion_ids as a list of strings")
        else:
            unknown = sorted(set(crits) - known)
            if unknown:
                problems.append(f"use case {label} references unknown criteria "
                                + ", ".join(unknown))
    return problems


# ---------------------------------------------------------------- evidence for people and prompts


def bounded_digest(report: UiLaneReport | None, limit: int = DIGEST_CHARS) -> str:
    """Case results, error summaries and artifact paths, capped. Page text is untrusted data."""
    if report is None:
        return "(no report)"
    lines = []
    if report.unavailable:
        lines.append(f"unavailable: {_clip(report.unavailable, ITEM_CHARS)}")
    for c in report.cases[:DIGEST_CASES]:
        lines.append(f"- case {_clip(c.id, 120)} {'PASS' if c.ok else 'FAIL'} "
                     f"({c.duration_s:.2f}s) covers: {', '.join(c.use_case_ids[:10]) or 'none'}")
        for e in c.errors[:DIGEST_ITEMS]:
            lines.append(f"    error: {_clip(e, ITEM_CHARS)}")
        for m in c.console[:DIGEST_ITEMS]:
            lines.append(f"    console {_clip(m.get('type'), 20)}: {_clip(m.get('text'), ITEM_CHARS)}")
        for e in c.page_errors[:DIGEST_ITEMS]:
            lines.append(f"    page error: {_clip(e, ITEM_CHARS)}")
        for n in c.network_failures[:DIGEST_ITEMS]:
            lines.append(f"    {_clip(_describe_network(n), ITEM_CHARS)}")
        if c.screenshot:
            lines.append(f"    screenshot: {_clip(c.screenshot, ITEM_CHARS)}")
        if c.dom:
            lines.append(f"    dom: {_clip(c.dom, ITEM_CHARS)}")
    if len(report.cases) > DIGEST_CASES:
        lines.append(f"... {len(report.cases) - DIGEST_CASES} more case(s) not shown")
    text = "\n".join(lines) or "(no cases)"
    return text if len(text) <= limit else text[:limit] + "\n... digest truncated"


def _cell(text: object) -> str:
    s = re.sub(r"\s+", " ", str(text)).strip().replace("|", "\\|")
    return s if len(s) <= CELL_CHARS else s[:CELL_CHARS] + "..."


def _mapped(report: UiLaneReport | None, use_case_id: str) -> list[UiCase]:
    return [c for c in (report.cases if report else []) if use_case_id in c.use_case_ids]


def verification_rows(matrix: list[dict], report: UiLaneReport | None,
                      evaluation: dict | None = None) -> list[dict]:
    """One row per planned use case. Status comes from the (reconciled) evaluation if given,
    else from the deterministic report alone."""
    by_id = {e.get("id"): e for e in (evaluation or {}).get("use_cases", [])
             if isinstance(e, dict)}
    rows = []
    for uc in matrix:
        uid = str(uc.get("id", ""))
        mapped = _mapped(report, uid)
        passing = [c for c in mapped if c.ok]
        ev = by_id.get(uid)
        if ev is not None:
            status = ev.get("status", "missing")
            evidence = ev.get("evidence", "")
            case_ids = [str(x) for x in ev.get("case_ids", [])] or [c.id for c in mapped]
        else:
            status = "pass" if passing and len(passing) == len(mapped) else (
                "fail" if mapped else "missing")
            evidence = "; ".join(f"{c.id} {'ok' if c.ok else 'failed'}" for c in mapped) \
                or "no case covers this use case"
            case_ids = [c.id for c in mapped]
        rows.append({"id": uid, "title": uc.get("title", ""), "kind": uc.get("kind", ""),
                     "case_ids": case_ids, "status": status, "evidence": str(evidence)})
    return rows


def format_table(matrix: list[dict], report: UiLaneReport | None,
                 evaluation: dict | None = None) -> str:
    head = (f"### {TABLE_TITLE}\n\n| Use case | Title | Kind | Cases | Status | Evidence |\n"
            "| --- | --- | --- | --- | --- | --- |")
    body = ["| " + " | ".join(_cell(x) for x in (
        r["id"], r["title"], r["kind"], ", ".join(r["case_ids"]) or "-", r["status"],
        r["evidence"] or "-")) + " |" for r in verification_rows(matrix, report, evaluation)]
    return "\n".join([head, *body]) if body else head + "\n| _no use cases planned_ | | | | | |"


def reconcile(matrix: list[dict], report: UiLaneReport | None, evaluation: dict
              ) -> tuple[dict, list[dict]]:
    """Apply the engine's veto to the evaluator's answer.

    Returns (evaluation with corrected statuses, blocking review findings). A `pass` for a use
    case with no passing case in the deterministic report becomes `missing`; fail/missing use
    cases and blocking evaluator findings become blocking findings.
    """
    given = {e.get("id"): e for e in evaluation.get("use_cases", []) if isinstance(e, dict)}
    corrected, blocking = [], []
    for uc in matrix:
        uid = str(uc.get("id", ""))
        entry = dict(given.get(uid) or {"id": uid, "status": "missing", "case_ids": [],
                                        "evidence": "the evaluator did not report this use case"})
        if entry.get("status") == "pass" and not any(c.ok for c in _mapped(report, uid)):
            entry["status"] = "missing"
            entry["evidence"] = ("the evaluator reported pass, but no passing case in the "
                                 "deterministic report covers this use case")
        corrected.append(entry)
        if entry.get("status") != "pass":
            blocking.append({
                "id": f"ui-use-case-{uid}", "severity": "blocker", "blocking": True,
                "file": "(ui lane)",
                "summary": f"UI use case {uid} ({uc.get('title', '')}) is "
                           f"{entry.get('status')}: {entry.get('evidence', '')}"})
    for f in evaluation.get("findings", []):
        if isinstance(f, dict) and f.get("blocking"):
            blocking.append({"id": str(f.get("id", "ui-finding")),
                             "severity": f.get("severity", "major"), "blocking": True,
                             "file": "(ui evaluator)", "summary": str(f.get("summary", ""))})
    fixed = {**evaluation, "use_cases": corrected}
    return fixed, blocking
