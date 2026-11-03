"""Offline trace-completeness validator for exported devflow spans.

Pure and I/O-free: it takes span records (the dicts written to the JSONL trace log) and returns
a list of violations, empty when the ticket's trace is complete. Rows are grouped into
invocation slices by ``trace_id`` (one ``ticket.run`` root per engine invocation); required
stages are checked across the whole ticket, parentage within each slice.
"""
from __future__ import annotations

from collections import Counter

ROOT = "ticket.run"
SESSION_KEY = "langfuse.session.id"

# logical stage -> (span name, invocation in which the lifecycle normally emits it, 1-based).
# Deliberate and adjustable: a workflow that legitimately skips a stage must change this.
REQUIRED_STAGES = {
    "plan": ("stage.refining", 1),
    "test_plan": ("stage.test_design", 2),
    "implement": ("stage.implementing", 2),
    "gates": ("stage.gates", 2),
    "review": ("stage.in_review", 2),
}
# agent role (last dotted component of ``agent.<provider>.<role>``) -> stages that may own it
AGENT_STAGES = {
    "planner": {"stage.refining"},
    "test_planner": {"stage.test_design"},
    "coder": {"stage.implementing", "stage.gates"},
    "reviewer": {"stage.in_review", "stage.refining"},
}
# stages that must own at least one agent span somewhere in the ticket
AGENT_REQUIRED_STAGES = ("stage.refining", "stage.test_design", "stage.implementing", "stage.in_review")
STAGE_ATTACHED = ("gate.result", "state.transition")


class Violation(str):
    """A human-readable violation message that also carries its machine-readable ``kind``."""
    kind: str

    def __new__(cls, kind: str, message: str):
        obj = super().__new__(cls, message)
        obj.kind = kind
        return obj


def _label(r: dict) -> str:
    return f"span '{r.get('name')}' (id={r.get('span_id')})"


def check_trace_completeness(rows: list[dict]) -> list[Violation]:
    """Return the violations in ``rows``; an empty list means the trace is complete."""
    out: list[Violation] = []
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("trace_id", ""), []).append(r)
    ordered = sorted(groups.items(), key=lambda kv: (min(r.get("start_ns", 0) for r in kv[1]), kv[0]))
    slices = [g for _, g in ordered]

    sessions = Counter(r.get("attributes", {}).get(SESSION_KEY) for r in rows)
    if sessions:
        expected = min(sessions.items(), key=lambda kv: (-kv[1], str(kv[0])))[0]
        for n, s in enumerate(slices, 1):
            for r in sorted(s, key=lambda r: str(r.get("span_id"))):
                got = r.get("attributes", {}).get(SESSION_KEY)
                if got != expected:
                    out.append(Violation(
                        "session_mismatch",
                        f"slice {n}: {_label(r)} session id {got!r} differs from expected {expected!r}"))

    found: dict[str, list[dict]] = {}  # stage name -> spans, ticket-wide
    attached = {name: 0 for name in STAGE_ATTACHED}
    agent_owners: set[str] = set()
    for n, s in enumerate(slices, 1):
        out += _check_slice(n, s, found, attached, agent_owners)

    for logical, (name, hint) in REQUIRED_STAGES.items():
        if not found.get(name):
            n = min(hint, len(slices)) or 1
            out.append(Violation("missing_stage", f"slice {n}: required stage span '{name}' ({logical}) missing"))
    for name in STAGE_ATTACHED:
        if not attached[name]:
            out.append(Violation("missing_child", f"required '{name}' span under a stage span missing"))
    for stage in AGENT_REQUIRED_STAGES:
        if found.get(stage) and stage not in agent_owners:
            out.append(Violation("missing_child", f"required agent span under '{stage}' missing"))
    return out


def _check_slice(n: int, s: list[dict], found: dict, attached: dict, agent_owners: set) -> list[Violation]:
    out: list[Violation] = []
    s = sorted(s, key=lambda r: (r.get("start_ns", 0), str(r.get("span_id"))))
    index: dict[str, dict] = {}
    for r in s:
        if r.get("span_id") in index:
            out.append(Violation("duplicate_span", f"slice {n}: {_label(r)} duplicate span id"))
        index[r.get("span_id")] = r
    roots = [r for r in s if r.get("name") == ROOT]
    if not roots:
        out.append(Violation("missing_root", f"slice {n}: no '{ROOT}' root span (trace_id={s[0].get('trace_id')})"))
    for extra in roots[1:]:
        out.append(Violation("multiple_roots", f"slice {n}: {_label(extra)} is an extra '{ROOT}' root"))
    root = roots[0] if roots else None
    for r in roots:
        if r.get("parent_id"):
            out.append(Violation("root_has_parent",
                                 f"slice {n}: {_label(r)} root has non-empty parent '{r['parent_id']}'"))

    for r in s:
        if r.get("name") == ROOT:
            continue
        pid = r.get("parent_id", "")
        if not pid:
            out.append(Violation("orphan", f"slice {n}: {_label(r)} has no parent and is not '{ROOT}'"))
        elif pid not in index:
            out.append(Violation("dangling_parent", f"slice {n}: {_label(r)} parent '{pid}' not found"))
        else:
            seen, cur = {r["span_id"]}, index[pid]
            while True:
                if cur["span_id"] in seen:
                    if cur is r:
                        out.append(Violation("cycle", f"slice {n}: {_label(r)} is part of a parent cycle"))
                    break
                seen.add(cur["span_id"])
                nxt = cur.get("parent_id", "")
                if cur.get("name") == ROOT or nxt not in index:
                    break
                cur = index[nxt]

    for r in s:
        name = r.get("name", "")
        parent = index.get(r.get("parent_id", ""))
        if name.startswith("stage.") and name != "stage.result":
            found.setdefault(name, []).append(r)
            state = r.get("attributes", {}).get("devflow.state")
            if state != name.removeprefix("stage."):
                out.append(Violation("state_mismatch",
                                     f"slice {n}: {_label(r)} devflow.state {state!r} does not match its name"))
            if name in {v[0] for v in REQUIRED_STAGES.values()} and parent is not None \
                    and (root is None or parent is not root):
                out.append(Violation("stage_not_under_root",
                                     f"slice {n}: {_label(r)} is not a direct child of '{ROOT}' "
                                     f"(parent is '{parent.get('name')}')"))
        elif name.startswith("agent.") and parent is not None:
            pname = parent.get("name", "")
            if not pname.startswith("stage.") or pname == "stage.result":
                out.append(Violation("agent_not_under_stage",
                                     f"slice {n}: {_label(r)} parent '{pname}' is not a stage span"))
            else:
                allowed = AGENT_STAGES.get(name.rsplit(".", 1)[-1])
                if allowed is not None and pname not in allowed:
                    out.append(Violation("agent_wrong_stage",
                                         f"slice {n}: {_label(r)} is under '{pname}', expected one of "
                                         f"{sorted(allowed)}"))
                else:
                    agent_owners.add(pname)
        elif name in STAGE_ATTACHED and parent is not None:
            pname = parent.get("name", "")
            if not pname.startswith("stage.") or pname == "stage.result":
                out.append(Violation("child_not_under_stage",
                                     f"slice {n}: {_label(r)} parent '{pname}' is not a stage span"))
            else:
                attached[name] += 1
    return out
