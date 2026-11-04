"""Deterministic checks on a plan, run before an LLM reviewer or a coder spends money on it.

Only high-precision checks are blocking. Heuristics that can be wrong are warnings: they are
recorded and shown to the reviewer but never reject a plan on their own.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from .diffguard import Finding, blocking, is_protected
from .loadconfig import Config

__all__ = ["Finding", "blocking", "format_findings", "lint"]

# Claims that a configured gate does not exist. Checked only when that gate IS configured.
_NO_INTEGRATION = re.compile(
    r"\bno (?:separate |configured |dedicated |such )?integration (?:gate|tests?)\b|"
    r"\bintegration gate (?:is |was |does |doesn't |do )?(?:missing|absent|not (?:configured|exist)|"
    r"invented|not exist|n't exist)\b|"
    r"\bunit[- ]only (?:gate|gates|verification|project)\b|"
    r"\bonly (?:the )?(?:lint and )?unit gates?\b|"
    r"\brepo (?:configures|defines|has) only (?:lint|unit)",
    re.IGNORECASE,
)
_GATE_COMMAND = re.compile(
    r"integration gate[^`\n]{0,60}`([^`]+)`|`([^`]+)`[^.\n]{0,60}(?:as|is) (?:the )?(?:repo's |repository's )?"
    r"(?:configured )?integration gate",
    re.IGNORECASE,
)
_TELEMETRY_CHANGE = re.compile(
    r"trace metadata|span attribute|telemetry (?:event|key|attribute)|new (?:trace|telemetry) |"
    r"langfuse\.trace\.metadata|_TRACE_KEYS",
    re.IGNORECASE,
)
_SKIP_WORDING = re.compile(
    r"\b(?:add|use|apply|insert|mark|decorate)\w*\s+(?:a\s+|an\s+|the\s+)?`?(?:pytest\.mark\.)?"
    r"(?:skip|skipif|xfail|importorskip)\b", re.IGNORECASE)
_CREATES = re.compile(r"\b(?:creat|add|new|introduc|write)\w*", re.IGNORECASE)


def _texts(plan: dict) -> list[tuple[str, str]]:
    """(where, text) for every free-text field of a plan."""
    out = [("goal", str(plan.get("goal", "")))]
    out += [(f"criterion {c.get('id', '?')}", str(c.get("text", ""))) for c in plan.get("acceptance_criteria", [])]
    out += [(f"step {s.get('id', '?')}", str(s.get("description", ""))) for s in plan.get("steps", [])]
    out += [("risk", str(r)) for r in plan.get("risks", [])]
    out += [("out of scope", str(r)) for r in plan.get("out_of_scope", [])]
    return out


def _configured_commands(cfg: Config) -> tuple[set[str], bool]:
    commands, integration = set(), False
    for project in cfg.projects:
        for gate, cmds in project.gates.items():
            commands.update(cmds)
            integration = integration or (gate == "integration" and bool(cmds))
    return commands, integration


def _normalise(command: str) -> str:
    return " ".join(command.split())


def lint(plan: dict, cfg: Config, worktree=None) -> list[Finding]:
    """Findings for a plan with status 'ready'. Anything else has nothing to check yet."""
    if plan.get("status") != "ready":
        return []
    findings: list[Finding] = []
    steps = plan.get("steps", [])
    files = sorted({f for s in steps for f in s.get("files", [])})

    for path in files:
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts:
            findings.append(Finding("bad_path", "block", path,
                                    "plan paths must be relative to the repository root and stay inside it"))
        elif is_protected(path):
            findings.append(Finding(
                "protected_path", "block", path,
                "devflow blocks changes to workflow and devflow config files in its gates, so this "
                "step would fail after the coder had already worked. Remove it from the plan and "
                "list it under follow_ups for a person to do."))

    commands, has_integration = _configured_commands(cfg)
    normalised = {_normalise(c) for c in commands}
    for where, text in _texts(plan):
        if has_integration and _NO_INTEGRATION.search(text):
            findings.append(Finding(
                "gate_claim", "block", where,
                "says there is no integration gate, but one is configured. The configured gates are "
                + "; ".join(sorted(commands)) + "."))
        for match in _GATE_COMMAND.finditer(text):
            cited = _normalise(match.group(1) or match.group(2) or "")
            if cited and has_integration and cited not in normalised and cited.startswith(("python", "pytest", "npm", "uv")):
                findings.append(Finding(
                    "gate_invented", "block", where,
                    f"calls `{cited}` the integration gate, but it is not a configured gate "
                    f"(configured: {'; '.join(sorted(commands))})."))
        if _SKIP_WORDING.search(text):
            findings.append(Finding("skip_wording", "warn", where,
                                    "mentions skipping or xfailing tests; the skipped_test gate blocks that"))

    whole = " ".join(t for _, t in _texts(plan))
    if _TELEMETRY_CHANGE.search(whole) and not any(f.endswith("telemetry.py") for f in files):
        findings.append(Finding(
            "telemetry_registration", "warn", "(plan)",
            "adds trace metadata or telemetry events but does not touch telemetry.py; new keys must be "
            "registered in the allowlist or they are silently dropped"))

    if worktree is not None:
        by_file = {f: " ".join(s.get("description", "") for s in steps if f in s.get("files", []))
                   for f in files}
        for path, description in by_file.items():
            target = worktree / path
            if (not target.exists() and not target.parent.exists() and not _CREATES.search(description)
                    and not is_protected(path)):
                findings.append(Finding("missing_path", "warn", path,
                                        "neither the file nor its directory exists and the step does not "
                                        "say it creates it"))
    return findings


def format_findings(findings: list[Finding]) -> str:
    """Feedback text for the planner."""
    lines = ["Deterministic checks rejected this plan. Revise it and resubmit:"]
    lines += [f"- [{f.kind}] {f.file}: {f.detail}" for f in findings if f.severity == "block"]
    return "\n".join(lines)
