"""Command-center data: why a ticket is paused, where it sits in the workflow, and how to resume it.

Everything is derived from the local store (state, history, events, stage results, cooldowns),
the repository's STOP/checkpoint files and the runner lock. Nothing here writes to GitHub.
"""
from __future__ import annotations

import fcntl
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import checkpoint
from .loadconfig import ConfigError, load_config
from .states import TERMINAL, TRANSITIONS, State
from .store import Store, state_db_path

# The five components from the landing page, in order, plus the state(s) each one owns.
NODES: list[dict[str, Any]] = [
    {"id": "openclaw", "num": "01", "title": "OpenClaw", "sub": "Plan + clarify",
     "states": ["new", "refining", "awaiting_clarification", "awaiting_plan_approval"]},
    {"id": "coder", "num": "02", "title": "Coding agent", "sub": "Tests + code",
     "states": ["test_design", "implementing", "changes_requested"]},
    {"id": "engine", "num": "03", "title": "Devflow engine", "sub": "Clean test gates",
     "states": ["gates"]},
    {"id": "reviewer", "num": "04", "title": "Reviewer", "sub": "Diff + evidence",
     "states": ["in_review"]},
    {"id": "github", "num": "05", "title": "GitHub PR", "sub": "CI + exact SHA",
     "states": ["awaiting_merge_approval", "merged"]},
]
INPUTS = [{"title": "GitHub Project", "sub": "issue + queue"}, {"title": "Graphify", "sub": "local code context"}]
STATE_NODE = {s: n["id"] for n in NODES for s in n["states"]}

# What the engine is waiting for in each human-gate state (mirrors engine._Wait messages).
GATES = {
    "awaiting_clarification": ("answer", "/answer <reply> (or /approve to accept the defaults)", "the issue"),
    "awaiting_plan_approval": ("plan_approval", "/approve (or /reject <feedback>)", "the issue"),
    "awaiting_merge_approval": ("merge_approval", "/approve (or /reject <feedback>)", "the pull request"),
    "escalated": ("escalated", "/resume", "the issue"),
}
WORKING = {"new", "refining", "test_design", "implementing", "gates", "in_review", "changes_requested"}


def _runner_busy(root: Path) -> bool:
    from .gitops import git
    try:
        common = Path(git(root, "rev-parse", "--git-common-dir").strip())
        common = common if common.is_absolute() else root / common
        with (common / "devflow-runner.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock, fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001
        return False
    return False


def _age(ts: float | None, now: float) -> float | None:
    return round(max(0.0, now - ts), 1) if ts else None


def _limits(root: Path) -> tuple[int, int, dict[str, bool]]:
    try:
        cfg = load_config(root)
        return (cfg.limits.max_review_rounds, cfg.limits.max_implement_attempts,
                dict(cfg.human_gates))
    except (ConfigError, AttributeError):
        return 3, 3, {}


def _diagnose(t: Any, events: list[dict[str, Any]], results: list[dict[str, Any]], context: dict[str, Any],
              now: float) -> dict[str, Any]:
    state = t.state.value
    transitions = [e for e in events if e["kind"] == "transition"]
    last = transitions[-1] if transitions else None
    since = _age(last["ts"], now) if last else None
    errors = [e for e in events if e["kind"] == "error" and e.get("state") == state
              and (not last or e["ts"] >= last["ts"])]
    node = STATE_NODE.get(state)
    if node is None and state == "escalated":  # show it on the component where the work gave up
        node = next((STATE_NODE[m["to"]] for m in reversed(transitions) if m.get("to") in STATE_NODE), None)
    out: dict[str, Any] = {"state": state, "node": node, "since_seconds": since,
                           "evidence": [], "needs": None, "status": "working", "tone": "info"}
    ev = out["evidence"]
    if last:
        ev.append(f"Entered {state} via {last.get('actor', '?')}: {last.get('reason') or 'no reason recorded'}")

    if t.state in TERMINAL:
        out.update(status="done" if t.state is State.MERGED else "cancelled", tone="ok",
                   headline="Merged" if t.state is State.MERGED else "Cancelled",
                   detail="This ticket is finished; nothing to resume.")
        return out

    if context["stop"]:
        ev.append("STOP file present at .devflow/STOP")
    if context["checkpoint"]:
        ev.append("Three-ticket checkpoint is waiting for `devflow continue`")

    if state in GATES:
        gate, command, where = GATES[state]
        enabled = context["gates"].get(gate, True) if gate in ("plan_approval", "merge_approval") else True
        if state == "escalated":
            reason = last.get("reason") if last else ""
            out.update(status="escalated", tone="bad", headline="Escalated: the engine gave up and needs a human",
                       detail=reason or "No escalation reason was recorded.")
            out["needs"] = {"who": "you", "command": "/resume", "where": where,
                            "note": "Review and fix the cause first. Resume resets the review and attempt counters."}
        elif enabled:
            labels = {"awaiting_clarification": "Waiting for your answer to the planner's questions",
                      "awaiting_plan_approval": "Waiting for plan approval",
                      "awaiting_merge_approval": "Waiting for merge approval"}
            out.update(status="waiting_human", tone="warn", headline=labels[state],
                       detail=f"The engine will not continue until an authorized person comments {command} on {where}.")
            out["needs"] = {"who": "an authorized approver", "command": command, "where": where}
        else:
            out.update(status="working", tone="info", headline=f"{gate.replace('_', ' ')} gate disabled",
                       detail="This gate passes automatically on the next run.")
        if state == "awaiting_merge_approval":
            sha = t.sessions.get("reviewed_sha")
            ev.append(f"Reviewed commit: {sha[:10]}" if sha else "No reviewed commit recorded; engine will escalate")
    else:
        out.update(status="stalled", tone="warn", headline=f"Stopped mid-{state.replace('_', ' ')}",
                   detail="No gate is blocking this state, so the run was interrupted or is between steps. "
                          "Resuming continues from the stored state.")

    if errors:
        err = errors[-1]
        ev.append(f"{len(errors)} error(s) in this state, last: {str(err.get('error', ''))[:300]}")
        if state in WORKING:
            out.update(status="blocked", tone="bad", headline=f"Failing in {state.replace('_', ' ')}",
                       detail=f"Last error: {str(err.get('error', ''))[:300]}. The engine retries once more, "
                              "then escalates.")
    if t.review_round or t.implement_attempts:
        ev.append(f"Review round {t.review_round}/{context['max_review']}, "
                  f"implementation attempt {t.implement_attempts}/{context['max_attempts']}")
        if t.review_round >= context["max_review"] or t.implement_attempts >= context["max_attempts"]:
            ev.append("Retry cap reached: the next failure escalates")
    for name, cd in context["cooldowns"].items():
        if cd.get("until", 0) > now:
            ev.append(f"{name} is cooling down for {int(cd['until'] - now)}s: {cd.get('reason', '')}")
            if state in WORKING and out["status"] in ("working", "stalled"):
                out.update(status="blocked", tone="warn", headline=f"No coding agent available ({name} cooling down)")
    if state == "in_review" or state == "changes_requested":
        review = next((r for r in reversed(results) if r["stage"] == "review"), None)
        if review:
            p = review["payload"] if isinstance(review["payload"], dict) else {}
            blocking = [f for f in p.get("findings", []) if isinstance(f, dict) and f.get("severity") == "blocking"]
            if blocking:
                ev.append(f"{len(blocking)} blocking review finding(s) outstanding")
    if context["stop"] or context["checkpoint"]:
        out.update(status="blocked", tone="bad", headline="Queue is held",
                   detail="The queue will not start work: remove .devflow/STOP or run `devflow continue`.")
    out["resumable"] = not context["busy"]
    if context["busy"]:
        ev.append("Another devflow runner currently owns this repository")
    return out


def _timeline(history: list[dict[str, Any]], events: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    moves = [e for e in events if e["kind"] == "transition"]
    rows = []
    for i, m in enumerate(moves):
        end = moves[i + 1]["ts"] if i + 1 < len(moves) else now
        rows.append({"to": m.get("to"), "actor": m.get("actor"), "reason": m.get("reason"),
                     "ts": m["ts"], "seconds_in_state": round(max(0.0, end - m["ts"]), 1),
                     "open": i + 1 == len(moves)})
    return rows


def list_tickets(root: Path) -> list[dict[str, Any]]:
    db = state_db_path(root)
    if not db.exists():
        return []
    now, max_review, max_attempts, gates = time.time(), *_limits(root)
    with Store(db) as store:
        cooldowns = {a: c for a in {"claude", "codex", "cursor"} if (c := store.get_cooldown(a))}
        context = {"stop": (root / ".devflow" / "STOP").exists(), "checkpoint": checkpoint.read(root)["pending"],
                   "busy": _runner_busy(root), "gates": gates, "max_review": max_review,
                   "max_attempts": max_attempts, "cooldowns": cooldowns}
        out = []
        for t in store.list_tickets():
            d = _diagnose(t, store.events(t.id), store.stage_results(t.id), context, now)
            out.append({"issue": t.issue, "title": t.title or f"Ticket #{t.issue}", "state": d["state"],
                        "status": d["status"], "tone": d["tone"], "headline": d["headline"],
                        "since_seconds": d["since_seconds"], "node": d["node"]})
    order = {"escalated": 0, "blocked": 1, "waiting_human": 2, "stalled": 3, "working": 4, "done": 5, "cancelled": 6}
    return sorted(out, key=lambda r: (order.get(r["status"], 9), -(r["issue"] or 0)))


def ticket_detail(root: Path, issue: int) -> dict[str, Any] | None:
    db = state_db_path(root)
    if not db.exists():
        return None
    now, max_review, max_attempts, gates = time.time(), *_limits(root)
    with Store(db) as store:
        t = store.get_ticket(f"#{issue}")
        if t is None:
            return None
        events = store.events(t.id)
        cooldowns = {a: c for a in {"claude", "codex", "cursor"} if (c := store.get_cooldown(a))}
        context = {"stop": (root / ".devflow" / "STOP").exists(), "checkpoint": checkpoint.read(root)["pending"],
                   "busy": _runner_busy(root), "gates": gates, "max_review": max_review,
                   "max_attempts": max_attempts, "cooldowns": cooldowns}
        diagnosis = _diagnose(t, events, store.stage_results(t.id), context, now)
        timeline = _timeline(t.history, events, now)
    visited = {STATE_NODE.get(r["to"]) for r in timeline}
    current = diagnosis["node"]
    order = [n["id"] for n in NODES]
    nodes = []
    for n in NODES:
        if n["id"] == current:
            status = {"done": "done", "cancelled": "done"}.get(diagnosis["status"], diagnosis["status"])
        elif n["id"] in visited and current in order and order.index(n["id"]) < order.index(current):
            status = "done"
        elif n["id"] in visited:
            status = "visited"
        else:
            status = "pending"
        nodes.append({**n, "status": status, "current": n["id"] == current,
                      "visits": sum(1 for r in timeline if STATE_NODE.get(r["to"]) == n["id"])})
    return {"issue": issue, "title": t.title or f"Ticket #{issue}", "branch": t.branch, "diagnosis": diagnosis,
            "graph": {"inputs": INPUTS, "nodes": nodes,
                      "loops": {"review_to_coder": t.review_round, "gate_retries": t.implement_attempts},
                      "transitions": {k.value: sorted(v.value for v in vs) for k, vs in TRANSITIONS.items()}},
            "timeline": timeline, "run": run_status(root, issue)}


# ---------------------------------------------------------------------- resume runner

_RUNS: dict[tuple[str, int], dict[str, Any]] = {}
_LOCK = threading.Lock()


def run_status(root: Path, issue: int) -> dict[str, Any]:
    with _LOCK:
        run = _RUNS.get((str(root), issue))
        if run is None:
            return {"state": "idle"}
        code = run["proc"].poll()
        log = run["log"]
    try:
        tail = log.read_text(errors="replace")[-6000:]
    except OSError:
        tail = ""
    return {"state": "running" if code is None else ("finished" if code in (0, 3) else "failed"),
            "exit_code": code, "started": run["started"], "dry_run": run["dry_run"], "log": tail}


def start_resume(root: Path, issue: int, dry_run: bool = False) -> dict[str, Any]:
    """Run `devflow ticket <issue>` once in a child process, exactly as the CLI would."""
    key = (str(root), issue)
    with _LOCK:
        run = _RUNS.get(key)
        if run and run["proc"].poll() is None:
            raise RuntimeError(f"#{issue} is already being resumed from this UI")
    if _runner_busy(root):
        raise RuntimeError("another devflow ticket or queue runner owns this repository")
    log_dir = root / ".devflow" / "state" / "ui-runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"issue-{issue}.log"
    cmd = [sys.executable, "-m", "devflow.cli", "ticket", str(issue), str(root)]
    if dry_run:
        cmd.append("--dry-run")
    handle = log.open("w")
    try:
        proc = subprocess.Popen(cmd, cwd=root, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    finally:
        handle.close()
    with _LOCK:
        _RUNS[key] = {"proc": proc, "log": log, "started": time.time(), "dry_run": dry_run}
    return run_status(root, issue)
