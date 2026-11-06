"""Drive one ticket through the pipeline.

Each state has a handler that does one thing and either moves the ticket (through the store, so
every move is validated and persisted) or stops. The engine stops in three ways: it needs a
person (a human gate, a clarification), it needs time (CI pending, agents cooling down), or the
ticket is finished or escalated. Running it again resumes from the stored state, so a crash, a
pause and a restart all look the same.

What decides what:
  * code decides state, gates, retries, pushing and merging, and only trusts facts it produced;
  * agents decide plans, code, and review, and return JSON that is validated before use;
  * people decide plan approval, clarifications and merge, by commenting `/approve` etc.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from . import (
    baseline,
    commands,
    diffguard,
    failures,
    followups,
    gitops,
    graphctx,
    prompts,
    stall,
    telemetry,
    uilane,
    worktree,
)
from .agents import Agent, AgentError, AgentRequest, AgentResult, run_structured
from .board import Board, BoardError, status_for
from .context import ledger_path, load_brief
from .gates import GateReport, run_gates, setup_worktree
from .graphify import build_graph
from .host import Host, HostError, upsert_status
from .learn import retro
from .learn.capture import capture
from .learn.ledger import Ledger, LessonRejected, parse_learn, render_for_prompt
from .loadconfig import Config
from .notify import Notifier, NullNotifier
from .router import CapabilityUnavailable, NoAgentAvailable, Router, run_routed
from .states import TERMINAL, LimitExceeded, State, Ticket
from .store import Store

MAX_CLARIFICATION_ROUNDS = 3
MAX_PLAN_CYCLES = 4
FEEDBACK_OUTPUT_CHARS = 1500
OUTPUT_TAIL_CHARS = 2000
IMPACT_LIST_CAP = 100


class _Wait(telemetry.WaitSignal):
    """Stop here; nothing more can happen until a person acts or time passes."""


class GraphUnavailableError(RuntimeError):
    pass


class _Escalate(Exception):
    """Stop here and hand the ticket to a person."""


@dataclass
class Outcome:
    ticket: Ticket
    status: str  # done | paused | escalated
    reason: str = ""


class Engine:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        host: Host,
        router: Router,
        agents: dict[str, Agent],
        notifier: Notifier | None = None,
        *,
        graph_builder: Callable = build_graph,
        board: Board | None = None,
        dry_run: bool = False,
        log: Callable[[str], None] = lambda message: None,
        max_steps: int = 60,
    ):
        self.cfg, self.store, self.host, self.router = cfg, store, host, router
        self.agents = agents
        self.notifier = notifier or NullNotifier()
        self.graph_builder, self.dry_run, self.log, self.max_steps = (
            graph_builder, dry_run, log, max_steps)
        self.board = board
        self.checkpoint: Callable[[], str] = lambda: ""
        self._ticket_id = ""
        self.preflight: Callable[[], list] | None = None  # wired by runtime.build_engine
        self._preflight_at, self._preflight_result = 0.0, []
        self._handlers: dict[State, Callable[[Ticket], None]] = {
            State.NEW: self._new,
            State.REFINING: self._refining,
            State.AWAITING_CLARIFICATION: self._awaiting_clarification,
            State.AWAITING_PLAN_APPROVAL: self._awaiting_plan_approval,
            State.TEST_DESIGN: self._test_design,
            State.IMPLEMENTING: self._implementing,
            State.GATES: self._gates,
            State.IN_REVIEW: self._in_review,
            State.CHANGES_REQUESTED: self._changes_requested,
            State.AWAITING_MERGE_APPROVAL: self._awaiting_merge_approval,
            State.ESCALATED: self._escalated,
        }

    # ------------------------------------------------------------------ driving

    @telemetry.ticket_run
    def run(self, issue: int) -> Outcome:
        ticket = self.store.get_ticket(f"#{issue}")
        if ticket is None:
            ticket = Ticket(id=f"#{issue}", issue=issue)
            self.store.save_ticket(ticket)
        else:
            self._apply_learn_commands(ticket)
        telemetry.trace_metadata(base_sha=ticket.base_sha or None, remote=self.cfg.remote)
        self._ticket_id = ticket.id
        for _ in range(self.max_steps):
            stop = self.checkpoint()
            if stop:
                return Outcome(ticket, "paused", stop)
            if ticket.state in TERMINAL:
                return Outcome(ticket, "done", ticket.state.value)
            before = ticket.state
            history = self._stall_history("stage." + before.value)
            started = time.monotonic()
            try:
                try:
                    with telemetry.span("stage." + ticket.state.value,
                                        {"devflow.state": ticket.state.value, "langfuse.observation.type": "chain",
                                         "devflow.implement_attempt": ticket.implement_attempts,
                                         "devflow.review_round": ticket.review_round}) as stage:
                        stage.content(input={"issue": issue, "state": ticket.state.value,
                                             "implementation_attempt": ticket.implement_attempts,
                                             "review_round": ticket.review_round})
                        self._check_budget(ticket)
                        self._handlers[ticket.state](ticket)
                        stage.content(output={"next_state": ticket.state.value})
                        stage.set(**{"devflow.next_state": ticket.state.value})
                finally:
                    self._stall_check("stage." + before.value, time.monotonic() - started, history)
            except _Wait as wait:
                return Outcome(ticket, "paused", str(wait))
            except NoAgentAvailable as exc:
                return Outcome(ticket, "paused", str(exc))
            except (_Escalate, LimitExceeded, CapabilityUnavailable) as exc:
                self._escalate(ticket, str(exc))
                return Outcome(ticket, "escalated", str(exc))
            except (AgentError, HostError, gitops.GitError, worktree.WorktreeError,
                    prompts.PromptError) as exc:
                if (isinstance(exc, AgentError | HostError)
                        and failures.classify(str(exc)) in (failures.INFRA, failures.LIMIT)):
                    return self._infra_pause(ticket, before, exc)
                if isinstance(exc, AgentError) and failures.classify(str(exc)) == failures.SANDBOX:
                    # Retrying the same agent in the same sandbox cannot change the answer.
                    reason = (f"{type(exc).__name__} in {before.value} looks like a sandbox "
                              f"denial, so retrying would only spend money: {exc}")
                    self.store.add_event(ticket.id, "error", {"state": before.value, "error": str(exc)})
                    self._escalate(ticket, reason)
                    return Outcome(ticket, "escalated", reason)
                if self._record_error(ticket, exc) >= 2:
                    reason = f"{type(exc).__name__} twice in {before.value}: {exc}"
                    self._escalate(ticket, reason)
                    return Outcome(ticket, "escalated", reason)
                return Outcome(ticket, "paused", f"{type(exc).__name__}: {exc} (will retry)")
        return Outcome(ticket, "paused", "step limit reached; run again to continue")

    def _stall_history(self, stage: str):
        """Prior durations for a stage, read before its own span is written. Never raises."""
        try:
            path = telemetry.log_path()
            if path is None:
                return None
            return stall.baseline_durations(stall.read_log_records(path), stage)
        except Exception:  # noqa: BLE001 - stall check must never alter ticket flow
            return None

    def _stall_check(self, stage: str, elapsed: float, history) -> None:
        """Warn and emit stall.detected when a finished stage exceeded its own p99. Never raises."""
        with suppress(Exception):  # stall check must never alter ticket flow
            if history is None:
                return
            info = stall.check_stall(stage, elapsed, history)
            if info:
                telemetry.report_stall(info)

    MAX_INFRA_PAUSES = 8
    PREFLIGHT_TTL_S = 60.0

    def _infra_pause(self, ticket: Ticket, state: State, exc: Exception) -> Outcome:
        """Something outside the ticket is down (OpenClaw, a rate limit). Wait; do not escalate."""
        self.store.add_event(ticket.id, "infra_error", {"state": state.value, "error": str(exc)[:500]})
        events = self.store.events(ticket.id)
        last_move = max((i for i, e in enumerate(events) if e["kind"] == "transition"), default=-1)
        n = sum(1 for e in events[last_move + 1:]
                if e["kind"] == "infra_error" and e.get("state") == state.value)
        if n >= self.MAX_INFRA_PAUSES:
            reason = f"infrastructure kept failing in {state.value} ({n} attempts): {exc}"
            self._escalate(ticket, reason)
            return Outcome(ticket, "escalated", reason)
        if n == 1:
            self.notifier.send(f"#{ticket.issue} ({ticket.title}): paused, infrastructure problem: "
                               f"{str(exc)[:120]}")
        return Outcome(ticket, "paused", f"infrastructure problem, will retry ({n}/{self.MAX_INFRA_PAUSES}): {exc}")

    def _run_preflight(self) -> None:
        if self.preflight is None:
            return
        now = time.monotonic()
        if not self._preflight_result or now - self._preflight_at > self.PREFLIGHT_TTL_S:
            self._preflight_result, self._preflight_at = self.preflight(), now
        from .preflight import blocking_failures, summarize
        if blocking_failures(self._preflight_result):
            raise _Wait("preflight failed: " + summarize(self._preflight_result))

    BUDGET_STATES = frozenset({State.NEW, State.REFINING, State.TEST_DESIGN, State.IMPLEMENTING,
                               State.GATES, State.IN_REVIEW, State.CHANGES_REQUESTED})

    def _spent(self, ticket: Ticket) -> tuple[float, int]:
        """Cost and tokens recorded for this ticket since it started or was last resumed."""
        totals = self.store.totals_for_ticket(ticket.id)
        base_cost, _, base_tokens = ticket.sessions.get("budget_base", "0:0").partition(":")
        try:
            return (max(0.0, totals["cost_usd"] - float(base_cost)),
                    max(0, int(totals["tokens"]) - int(base_tokens or 0)))
        except ValueError:
            return totals["cost_usd"], int(totals["tokens"])

    def _check_budget(self, ticket: Ticket) -> None:
        limits = self.cfg.limits
        if ticket.state not in self.BUDGET_STATES or not (limits.max_ticket_cost_usd
                                                          or limits.max_ticket_tokens):
            return
        cost, tokens = self._spent(ticket)
        over = []
        if limits.max_ticket_cost_usd and cost >= limits.max_ticket_cost_usd:
            over.append(f"${cost:.2f} spent, cap ${limits.max_ticket_cost_usd:.2f}")
        if limits.max_ticket_tokens and tokens >= limits.max_ticket_tokens:
            over.append(f"{tokens:,} tokens used, cap {limits.max_ticket_tokens:,}")
        if over:
            raise _Escalate("ticket budget exceeded (" + "; ".join(over) + "). Raise "
                            "[limits] max_ticket_cost_usd / max_ticket_tokens if this is expected, "
                            "then comment /resume to continue.")

    def _remaining_budget_usd(self, ticket: Ticket) -> float | None:
        cap = self.cfg.limits.max_ticket_cost_usd
        if not cap:
            return None
        return max(0.01, round(cap - self._spent(ticket)[0], 4))

    def _record_direct(self, agent: Agent, role: str, req: AgentRequest, result: AgentResult) -> None:
        """Record usage for agents that run outside the router (OpenClaw planner, reviewer ...)."""
        if not self._ticket_id:
            return
        usage = (result.raw or {}).get("usage") or {}
        tin = int(usage.get("input_tokens") or (len(req.prompt) + len(req.system)) / 4)
        tout = int(usage.get("output_tokens") or len(result.text or "") / 4)
        self.store.record_usage(agent.name, role, self._ticket_id, cost_usd=result.cost_usd or 0.0,
                                tokens_in=tin, tokens_out=tout, ok=result.ok, error=result.error)

    def _record_error(self, ticket: Ticket, exc: Exception) -> int:
        self.store.add_event(ticket.id, "error", {"state": ticket.state.value, "error": str(exc)})
        events = self.store.events(ticket.id)
        last_move = max((i for i, e in enumerate(events) if e["kind"] == "transition"), default=-1)
        return sum(1 for e in events[last_move + 1:]
                   if e["kind"] == "error" and e.get("state") == ticket.state.value)

    # ------------------------------------------------------------------ helpers

    def _move(self, ticket: Ticket, to: State, actor: str, reason: str = "") -> None:
        self.log(f"{ticket.id}: {ticket.state.value} -> {to.value} ({actor}) {reason}".strip())
        before = ticket.state.value
        self.store.transition(ticket, to, actor=actor, reason=reason, limits=self.cfg.limits)
        telemetry.content_event("state.transition", input={"from": before, "actor": actor, "reason": reason},
                                output={"to": to.value},
                                attributes={"devflow.state": to.value, "devflow.actor": actor})
        self._sync_board(ticket)

    def _sync_board(self, ticket: Ticket) -> None:
        """Mirror the ticket's state onto the board. A board problem never fails the ticket."""
        if self.board is None or ticket.issue is None:
            return
        want = status_for(ticket.state, self.cfg.board.statuses)
        if ticket.sessions.get("board_status") == want:
            return
        try:
            self.board.set_status(ticket.issue, want)
            ticket.sessions["board_status"] = want
            self.store.save_ticket(ticket)
        except BoardError as exc:
            self.store.add_event(ticket.id, "board_error", {"error": str(exc), "wanted": want})
            self.log(f"{ticket.id}: could not set board status to {want!r}: {exc}")

    def _escalate(self, ticket: Ticket, reason: str) -> None:
        if ticket.state is not State.ESCALATED:
            self.store.transition(ticket, State.ESCALATED, actor="engine", reason=reason[:300])
        text = (f"devflow needs you on #{ticket.issue} ({ticket.title}): {reason}\n"
                "Fix or answer in the issue, then comment `/resume`.")
        self._say(ticket, text)
        self.notifier.send(text)
        self._learn(ticket, "escalated", reason)

    def _put(self, ticket: Ticket, kind: str, payload: dict, ok: bool = True) -> None:
        attempt = len(self.store.stage_results(ticket.id, kind)) + 1
        self.store.add_stage_result(ticket.id, kind, attempt, ok, payload)
        facts = {"devflow.artifact": kind, "devflow.ok": ok, "devflow.attempt": attempt}
        if kind == "review":
            facts["devflow.review_findings"] = len(payload.get("findings", []))
            facts["devflow.blocking_findings"] = sum(bool(f.get("blocking"))
                                                    for f in payload.get("findings", []))
            facts["devflow.reviewer_independent"] = bool(payload.get("independent"))
        telemetry.content_event("stage.result", output=payload, attributes=facts)

    def _latest(self, ticket: Ticket, kind: str) -> dict | None:
        rows = self.store.stage_results(ticket.id, kind)
        return rows[-1]["payload"] if rows else None

    def _count(self, ticket: Ticket, kind: str) -> int:
        rows = self.store.stage_results(ticket.id, kind)
        if kind == "plan":
            resumed = [event["ts"] for event in self.store.events(ticket.id, "transition")
                       if event.get("reason") == "resumed"]
            if resumed:
                rows = [row for row in rows if row["ts"] > resumed[-1]]
        return len(rows)

    def _say(self, ticket: Ticket, text: str, number: int | None = None) -> None:
        self.host.comment(number or ticket.issue or 0, text)

    def _status(self, ticket: Ticket, text: str) -> None:
        upsert_status(self.host, ticket.issue or 0,
                      f"**devflow** · `{ticket.state.value}` · {ticket.branch or 'no branch yet'}\n\n"
                      + text)

    def _approvers(self) -> set[str]:
        return set(self.cfg.approvers) or {self.host.user()}

    def _consumed(self, ticket: Ticket) -> set[int]:
        used: set[int] = set()
        for e in self.store.events(ticket.id):
            if e["kind"] == "command":
                used.add(e["comment_id"])
            elif e["kind"] == "command_returned":
                used.discard(e["comment_id"])
        return used

    def _commands(self, ticket: Ticket, number: int, kinds: tuple[str, ...]) -> list[commands.Command]:
        return commands.pending(self.host.comments(number), self._approvers(),
                                self._consumed(ticket), kinds)

    def _consume(self, ticket: Ticket, cmd: commands.Command) -> None:
        self.store.add_event(ticket.id, "command", {"comment_id": cmd.comment_id,
                                                    "command": cmd.kind, "author": cmd.author,
                                                    "text": cmd.text[:500]})

    def _gate_on(self, name: str) -> bool:
        return self.cfg.human_gates.get(name, True)

    def _wt(self, ticket: Ticket) -> Path:
        return Path(ticket.worktree)

    def _request(
        self, ticket: Ticket, role: str, prompt: str, *, schema: str | None, read_only: bool,
        cwd: Path | None = None, files: list[str] | None = None, session: str | None = None,
        slice_text: str | None = None, requires: tuple[str, ...] = (),
    ) -> AgentRequest:
        wt = cwd or self._wt(ticket)
        if role == "planner":
            self._check_graph_staleness(wt)  # before the rebuild, so staleness is reported
        self._refresh_graph(wt)  # best effort; digest says if absent
        system = prompts.role_prompt(self.cfg.root, role) + "\n\n" + graphctx.agent_preamble(wt, files)
        system += self._context_for(ticket, role, files or [])
        if role == "planner" and slice_text is not None:
            system += self._ticket_slice(wt, slice_text)
        gates = {project.path: project.gates for project in self.cfg.projects}
        system += ("\n\n## Authoritative configured gates\n"
                   "These commands are loaded from the main checkout's .devflow/config.toml. "
                   "That file may be absent from this ticket worktree; do not infer that a gate "
                   "is missing from its absence.\n" + json.dumps(gates, sort_keys=True))
        if schema:
            system += "\n\n" + prompts.schema_block(schema, self.cfg.root)
        return AgentRequest(
            role=role, prompt=prompt, cwd=wt, system=system, schema=schema, read_only=read_only,
            session=session, timeout_s=self.cfg.stage_timeout_minutes * 60,
            mcp=graphctx.mcp_config(wt), mcp_overrides=graphctx.codex_overrides(wt),
            max_budget_usd=self._remaining_budget_usd(ticket),
            requires=requires,
        )

    def _ticket_slice(self, wt: Path, text: str) -> str:
        """The ego-network section for the planner prompt, or "" when no slice is available."""
        result = None
        with suppress(Exception):
            result = graphctx.ticket_slice(wt, text)
        if result is None:
            telemetry.trace_metadata(slice_status="unavailable", slice_seeds=0, slice_nodes=0)
            return ""
        telemetry.trace_metadata(slice_status="injected", slice_seeds=len(result.seeds),
                                 slice_nodes=result.nodes)
        return f"\n\n{prompts.SLICE_HEADER}\n{result.text}\n\n{prompts.SLICE_GUIDANCE}"

    def _blast_radius(self, wt: Path, base_sha: str, impact) -> str | None:
        """The reviewer checklist text, recording blast trace metadata; None when unavailable."""
        try:
            if impact is None:
                raise GraphUnavailableError
            names = gitops.diff_names(wt, base_sha)
            res = graphctx.reviewer_checklist(impact, names)
            telemetry.trace_metadata(
                blast_status="injected", blast_nodes=impact.node_count,
                blast_edges=impact.edge_count, blast_callers_total=res.callers_total,
                blast_callers_verified=res.callers_verified,
                impacted_tests=graphctx.impacted_tests(impact, names))
            return res.text
        except Exception:  # noqa: BLE001 - never stop a ticket
            telemetry.trace_metadata(blast_status="unavailable")
            return None

    def _check_graph_staleness(self, wt: Path) -> None:
        """Warn (never block) when the graph is stale or unbuilt as planning begins."""
        with suppress(Exception):  # best effort; never stop a ticket
            state = graphctx.graph_freshness(wt)
            telemetry.trace_metadata(graph_staleness=state)
            if state in ("stale", "unbuilt"):
                telemetry.report_graph_staleness({
                    "status": state, "fingerprint": graphctx.fingerprint(wt),
                    "stored": graphctx._stored(wt),
                    "age_seconds": graphctx.graph_age_seconds(wt)})

    def _refresh_graph(self, wt: Path) -> None:
        """Freshness check that can never stop a ticket; records graph provenance on the trace."""
        try:
            status = graphctx.ensure_fresh(wt, builder=self.graph_builder)
        except Exception:  # noqa: BLE001 - a broken graph builder must not stop the ticket
            telemetry.trace_metadata(graph_status="unavailable", graph_fingerprint="",
                                     graph_nodes=None, graph_edges=None)
            return
        known = status.state in ("fresh", "refreshed", "failed")
        telemetry.trace_metadata(
            graph_status=status.state, graph_fingerprint=status.fingerprint,
            graph_nodes=status.nodes if known else None,
            graph_edges=status.edges if known else None)

    def _context_for(self, ticket: Ticket, role: str, files: list[str]) -> str:
        """The repo brief and the lessons that fit this role and these files."""
        parts = []
        brief = load_brief(self.cfg.root)
        if brief:
            parts.append("## Repository brief\n" + brief)
        learning = self.cfg.learning
        if learning.enabled and role != "retro":
            ledger = Ledger(ledger_path(self.cfg.root))
            chosen = ledger.select(role, files, learning.budget_chars, record=True)
            if chosen:
                ledger.save()
                self.store.add_event(ticket.id, "lessons_used",
                                     {"role": role, "ids": [x.id for x in chosen]})
                parts.append(render_for_prompt(chosen))
        return ("\n\n" + "\n\n".join(parts)) if parts else ""

    def _plain(self, role: str, req: AgentRequest) -> AgentResult:
        agent = self.agents.get(role)
        if agent is None:
            raise AgentError(f"no agent configured for role {role!r}")
        result = run_structured(agent, req, self.cfg.root) if req.schema else agent.run(req)
        self._record_direct(agent, role, req, result)
        return result

    # Files that cannot be verified inside a sandbox without sockets or a browser. A heuristic;
    # the planner's own `requires` field and a runtime sandbox denial add to it.
    NEEDS_BY_PATH = (
        ("sockets", ("*server*", "*socket*", "*test_ui*", "tests/ui/*", "*e2e*")),
        ("browser", ("*playwright*", "*selenium*", "tests/ui/*", "*e2e*", "*browser*")),
    )

    def _requirements(self, ticket: Ticket, plan: dict | None) -> tuple[str, ...]:
        import fnmatch
        needed = {r for r in (plan or {}).get("requires", [])}
        needed |= {r for r in ticket.sessions.get("needs", "").split(",") if r}
        files = [f.lower() for f in self._plan_files(plan)]
        for capability, patterns in self.NEEDS_BY_PATH:
            if any(fnmatch.fnmatch(f, p) for f in files for p in patterns):
                needed.add(capability)
        return tuple(sorted(needed))

    def _plan_files(self, plan: dict | None) -> list[str]:
        return sorted({f for s in (plan or {}).get("steps", []) for f in s.get("files", [])})

    # ------------------------------------------------------------------ NEW

    def _new(self, t: Ticket) -> None:
        self._run_preflight()
        issue = self.host.get_issue(t.issue or 0)
        if issue.state != "open":
            self._move(t, State.CANCELLED, "engine", f"issue is {issue.state}")
            return
        t.title = issue.title
        wt = worktree.create(self.cfg.root, self.cfg.worktree_root, t.issue or 0, issue.title,
                             self.cfg.default_branch)
        t.branch, t.worktree, t.base_sha = wt.branch, str(wt.path), wt.base_sha
        telemetry.trace_metadata(base_sha=t.base_sha)
        self.store.save_ticket(t)
        self._move(t, State.REFINING, "engine", "started")

    # ------------------------------------------------------------------ REFINING

    def _refining(self, t: Ticket) -> None:
        if self._count(t, "plan") >= MAX_PLAN_CYCLES:
            raise _Escalate(f"the plan was reworked {MAX_PLAN_CYCLES} times since the last "
                            "resume without settling")
        setup = setup_worktree(self.cfg, self._wt(t))
        if not setup.ok and setup.results:
            raise _Escalate("environment setup failed: " + setup.results[-1].output_tail[-400:])

        issue = self.host.get_issue(t.issue or 0)
        answers = [e["text"] for e in self.store.events(t.id, "answers")]
        feedback = (self._latest(t, "plan_feedback") or {}).get("text", "")
        previous = self._latest(t, "plan") or {}
        req = self._request(t, "planner",
                            prompts.planner_task(
                                issue, answers, feedback, previous.get("questions"),
                                {project.path: project.gates for project in self.cfg.projects},
                                ui_lane=self.cfg.ui.enabled),
                            schema="plan", read_only=True,
                            slice_text=f"{issue.title}\n\n{issue.body}")
        plan = self._plain("planner", req).data
        self._put(t, "plan", plan)

        if plan["status"] == "needs_clarification":
            if len(answers) >= MAX_CLARIFICATION_ROUNDS:
                raise _Escalate("still ambiguous after repeated clarification")
            lines = [f"{i}. {q['question']}  _(default: {q['proposed_default']})_"
                     for i, q in enumerate(plan["questions"], 1)]
            numbered = "\n".join(lines)
            self._say(t, f"**devflow has questions before planning #{t.issue}**\n\n{numbered}\n\n"
                         "Reply `/answer ...` (or `/approve` to accept all the defaults).")
            self.notifier.send(f"#{t.issue} ({t.title}): devflow has {len(lines)} question(s).")
            self._move(t, State.AWAITING_CLARIFICATION, "planner", f"{len(lines)} question(s)")
            return
        problems = uilane.validate_use_cases(plan, self.cfg)
        if problems:
            # Same mechanism as a failed test design: the plan is not accepted, the planner
            # gets the reasons, and MAX_PLAN_CYCLES bounds the loop.
            self._put(t, "plan_feedback", {"text": "The plan touches user-facing files, so it "
                                           "needs well-formed `ui_use_cases`: "
                                           + "; ".join(problems) + "."})
            self.log(f"{t.id}: plan rejected, ui use cases: {'; '.join(problems)}")
            return
        self._say(t, self._format_plan(plan))
        if self._gate_on("plan_approval"):
            self.notifier.send(f"#{t.issue} ({t.title}): plan ready for your approval.")
        self._move(t, State.AWAITING_PLAN_APPROVAL, "planner", "plan ready")

    @staticmethod
    def _format_plan(plan: dict) -> str:
        ac = "\n".join(f"- **{a['id']}** {a['text']}" for a in plan["acceptance_criteria"])
        steps = "\n".join(f"{i}. {s['description']}  `{', '.join(s['files']) or 'no files'}`"
                          for i, s in enumerate(plan["steps"], 1))
        risks = "\n".join(f"- {r}" for r in plan.get("risks", []))
        cases = "\n".join(f"- **{u.get('id')}** ({u.get('kind')}) {u.get('title') or ''}: "
                          f"{u.get('description')}  _covers {', '.join(u.get('criterion_ids', []))}_"
                          for u in plan.get("ui_use_cases", []) if isinstance(u, dict))
        return (f"**Proposed plan**\n\n**Goal:** {plan['goal']}\n\n**Acceptance criteria**\n{ac}\n\n"
                f"**Steps**\n{steps}\n" + (f"\n**UI use cases**\n{cases}\n" if cases else "")
                + (f"\n**Risks**\n{risks}\n" if risks else "")
                + "\nComment `/approve` to proceed, or `/reject <what to change>`.")

    # ------------------------------------------------------------------ human gates

    def _awaiting_clarification(self, t: Ticket) -> None:
        cmds = self._commands(t, t.issue or 0, ("answer", "approve"))
        if not cmds:
            raise _Wait("waiting for /answer on the issue")
        text = "\n\n".join(
            c.text if c.kind == "answer" else "Proceed with the proposed defaults." for c in cmds)
        for c in cmds:
            self._consume(t, c)
        self.store.add_event(t.id, "answers", {"text": text})
        self._move(t, State.REFINING, "human", "answered")

    def _awaiting_plan_approval(self, t: Ticket) -> None:
        if not self._gate_on("plan_approval"):
            reviewer = self.agents.get("reviewer")
            if reviewer is None:
                self._move(t, State.TEST_DESIGN, "auto", "plan approval disabled")
                return
            plan = self._latest(t, "plan") or {}
            issue = self.host.get_issue(t.issue or 0)
            req = self._request(
                t, "reviewer",
                "Review this proposed implementation plan against the issue and repository. "
                "Check scope, acceptance criteria, testability, integration coverage, and "
                "risk. Approve only if a coding agent could execute it without guessing. "
                "If rejected, give concrete revision feedback.\n\n"
                + prompts.ticket_block(issue) + "\n\n"
                + prompts.fence("PROPOSED PLAN", json.dumps(plan, indent=2))
                + "\n\nAuthoritative gates from the main checkout (the ticket worktree may "
                "not contain the local config file):\n"
                + json.dumps({project.path: project.gates for project in self.cfg.projects},
                             indent=2, sort_keys=True)
                + "\nReject a plan that says a configured integration gate is missing, "
                "unit-only, or outside this ticket's verification.",
                schema="plan_review", read_only=True,
            )
            plan_result = run_structured(reviewer, req, self.cfg.root)
            self._record_direct(reviewer, "plan_reviewer", req, plan_result)
            decision = plan_result.data
            self._put(t, "plan_review", decision)
            if decision["decision"] == "approve":
                self._move(t, State.TEST_DESIGN, reviewer.name, "plan approved")
            else:
                self._put(t, "plan_feedback", {"text": decision["reason"]})
                self._move(t, State.REFINING, reviewer.name, "plan rejected")
            return
        cmds = self._commands(t, t.issue or 0, ("approve", "reject"))
        if not cmds:
            raise _Wait("waiting for /approve or /reject on the issue")
        cmd = cmds[0]
        self._consume(t, cmd)
        if cmd.kind == "approve":
            self._move(t, State.TEST_DESIGN, f"human:{cmd.author}", "plan approved")
        else:
            self._put(t, "plan_feedback", {"text": cmd.text or "The plan was rejected."})
            self._move(t, State.REFINING, f"human:{cmd.author}", "plan rejected")

    # ------------------------------------------------------------------ TEST_DESIGN

    def _test_design(self, t: Ticket) -> None:
        plan = self._latest(t, "plan") or {}
        wanted = {a["id"] for a in plan.get("acceptance_criteria", [])}
        problems = ""
        for _ in range(2):
            req = self._request(t, "test_planner", prompts.test_planner_task(plan, problems),
                                schema="test_plan", read_only=True, files=self._plan_files(plan))
            routed = run_routed(self.router, req, t.id, structured=True, root=self.cfg.root)
            tests = routed.result.data
            covered = {cid for test in tests["tests"] for cid in test["criterion_ids"]}
            uncovered, unknown = sorted(wanted - covered), sorted(covered - wanted)
            if not uncovered and not unknown:
                self._put(t, "test_plan", tests)
                self._move(t, State.IMPLEMENTING, routed.agent, f"{len(tests['tests'])} test(s)")
                return
            problems = "; ".join(filter(None, [
                f"no test covers {', '.join(uncovered)}" if uncovered else "",
                f"tests reference unknown criteria {', '.join(unknown)}" if unknown else ""]))
        self._put(t, "plan_feedback", {"text": f"The criteria could not be fully tested: {problems}. "
                                               "Make each criterion observable and testable."})
        self._move(t, State.REFINING, "engine", "traceability failed: " + problems)

    # ------------------------------------------------------------------ IMPLEMENTING

    def _implementing(self, t: Ticket) -> None:
        plan = self._latest(t, "plan") or {}
        feedback = t.sessions.pop("pending_feedback", "")
        prefer, session = t.sessions.get("coder_agent"), t.sessions.get("coder")
        wt = self._wt(t)
        stat = gitops.diff_stat(wt, t.base_sha)
        req = self._request(
            t, "coder",
            prompts.coder_task(plan, self._latest(t, "test_plan") or {}, feedback,
                               resuming=bool(session), diff_stat=stat),
            schema=None, read_only=False, files=self._plan_files(plan), session=session,
            requires=self._requirements(t, plan))
        routed = run_routed(self.router, req, t.id, prefer=prefer)
        t.sessions["coder"] = routed.result.session or ""
        t.sessions["coder_agent"] = routed.agent
        self.store.save_ticket(t)
        try:
            sha = gitops.commit_all(
                wt, f"{t.title} (#{t.issue})\n\nImplemented by devflow ({routed.agent}).")
        except gitops.UnsafeCommit as exc:
            raise _Escalate(str(exc)) from exc
        self._put(t, "implement", {"agent": routed.agent, "switched": routed.switched,
                                   "committed": sha, "summary": routed.result.text[-1500:]})
        if sha is None and failures.classify(routed.result.text) == failures.SANDBOX:
            needs = {n for n in t.sessions.get("needs", "").split(",") if n}
            if "sockets" not in needs and self.router.capable_agents({"sockets"}):
                # This agent's sandbox blocked the work; try one that has sockets before giving up.
                t.sessions["needs"] = ",".join(sorted(needs | {"sockets"}))
                t.sessions.pop("coder", None)
                self.store.save_ticket(t)
                self.store.add_event(t.id, "capability_retry", {"agent": routed.agent,
                                                                "needs": t.sessions["needs"]})
                return
            raise _Escalate(f"the coder ({routed.agent}) made no changes and reported that its "
                            "sandbox blocked the work. Retrying the same agent cannot fix that; "
                            "use an agent with the needed capability or run it by hand: "
                            + routed.result.text.strip()[-400:])
        if feedback and sha is None:
            # Re-running gates and review on identical code cannot change their answer. The
            # coder may disagree with the feedback; that is a call for a person.
            raise _Escalate("the coder made no changes after feedback and said: "
                            + routed.result.text.strip()[-400:])
        self._move(t, State.GATES, routed.agent, "committed" if sha else "no changes")

    # ------------------------------------------------------------------ GATES

    def _impact(self, path: Path, base_sha: str):
        """Best-effort impact report for the diff in `path`, or None. Never raises."""
        with suppress(Exception):
            self._refresh_graph(path)
            return graphctx.impact(path, gitops.diff_names(path, base_sha))
        return None

    def _verify(self, path: Path, base_sha: str, impact=None, plan: dict | None = None
                ) -> tuple[baseline.Verdict, list[diffguard.Finding], GateReport]:
        """Gate results against the base, plus the diff guard, in `path`."""
        setup = setup_worktree(self.cfg, path)
        if not setup.ok and setup.results:
            return (baseline.Verdict(ok=False, new_failures=[baseline.NewFailure(
                setup.results[-1].project, "setup", setup.results[-1].command, ["setup failed"],
                setup.results[-1].output_tail)]), [], setup)
        base = baseline.ensure_baseline(self.cfg, self.store, base_sha)
        tests: list[str] = []
        check = None
        if impact is not None:
            with suppress(Exception):
                tests = graphctx.impacted_tests(impact, gitops.diff_names(path, base_sha))

                def check(result):
                    return not baseline.evaluate(GateReport([result]), base).ok
        report = run_gates(self.cfg, path, impacted_tests=tests or None, priority_check=check)
        verdict = baseline.evaluate(report, base)
        self._ui_lane(path, base_sha, plan, report, verdict)
        return verdict, diffguard.analyze(path, base_sha), report

    def _ui_lane(self, path: Path, base_sha: str, plan: dict | None, report: GateReport,
                 verdict: baseline.Verdict) -> None:
        """Run the browser lane when `[ui]` is on and the diff touches user-facing files.

        Strict and never compared with the base: a console error on the base is not noise to
        accept. Without `[ui]` this does nothing at all.
        """
        ui = self.cfg.ui
        if not ui.enabled or not uilane.touches_ui(gitops.diff_names(path, base_sha), ui.paths):
            return
        matrix = [u for u in (plan or {}).get("ui_use_cases", []) if isinstance(u, dict)]
        if not ui.command:
            report.ui = uilane.UiLaneResult(status="unconfigured", use_cases=matrix)
            verdict.unconfigured.append(uilane.UNCONFIGURED_LABEL)
            telemetry.event("gate.ui_lane", **{"devflow.ran": False, "devflow.ok": True,
                                               "devflow.duration_s": 0.0, "devflow.cases": 0})
            return
        result = uilane.run_lane(self.cfg, path, matrix)
        report.ui = result
        telemetry.event("gate.ui_lane", **{
            "devflow.ran": True, "devflow.ok": result.ok, "devflow.duration_s": result.duration_s,
            "devflow.cases": len(result.report.cases) if result.report else 0})
        if not result.ok:
            verdict.ok = False
            verdict.new_failures.append(baseline.NewFailure(
                ".", "ui", ui.command, list(result.reasons),
                "\n".join(filter(None, [result.output_tail[-600:], result.details]))))

    @staticmethod
    def _bounded_impact(impact) -> dict | None:
        """`ImpactReport.to_dict()` with every list capped; None if it cannot be serialized."""
        try:
            if impact is None:
                return None
            data = impact.to_dict()
            cap = IMPACT_LIST_CAP
            for key in ("files", "unknown_files", "symbols", "dependent_modules",
                        "external_contracts", "uncovered"):
                if isinstance(data.get(key), list):
                    data[key] = data[key][:cap]
            data["callers"] = {h: cs[:cap] for h, cs in data.get("callers", {}).items()}
            data["test_coverage"] = dict(list(data.get("test_coverage", {}).items())[:cap])
            return data
        except Exception:  # noqa: BLE001 - never stop a ticket
            return None

    def _evidence(self, wt: Path, base_sha: str, verdict: baseline.Verdict,
                  report: GateReport | None, impact) -> dict:
        """Bounded, additive gate evidence for the stage payload (results, failures, impact)."""
        results = [{"project": r.project, "gate": r.gate, "command": r.command, "ok": r.ok,
                    "exit_code": r.exit_code, "duration_s": r.duration_s,
                    "timed_out": r.timed_out, "output_tail": (r.output_tail or "")[-OUTPUT_TAIL_CHARS:]}
                   for r in (report.results if report else [])]
        failures = [{"project": f.project, "gate": f.gate, "command": f.command,
                     "new": list(f.new[:20])} for f in verdict.new_failures]
        bounded = self._bounded_impact(impact)
        tests: list[str] = []
        if impact is not None:
            with suppress(Exception):
                tests = graphctx.impacted_tests(impact, gitops.diff_names(wt, base_sha))[:IMPACT_LIST_CAP]
        if bounded is not None:
            bounded["impacted_tests"] = tests
        evidence = {"results": results, "failures": failures, "impact": bounded,
                    "impacted_tests": tests}
        if report is not None and report.ui is not None and report.ui.ran:
            lane = report.ui.to_evidence()
            if report.ui.use_cases:
                lane["table"] = uilane.format_table(report.ui.use_cases[:12], report.ui.report)
            evidence["ui_lane"] = lane
        return evidence

    def _failure_text(self, verdict: baseline.Verdict, findings: list[diffguard.Finding]) -> str:
        lines = []
        for f in verdict.new_failures:
            lines.append(f"Gate {f.project}:{f.gate} (`{f.command}`) now fails: {', '.join(f.new[:8])}")
            lines.append(f.output_tail[-FEEDBACK_OUTPUT_CHARS:])
        for f in diffguard.blocking(findings):
            lines.append(f"Not allowed: {f.kind} in {f.file}: {f.detail}")
        if verdict.nothing_ran:
            lines.append("No gates are configured, so the change cannot be verified.")
        if verdict.preexisting and verdict.new_failures:
            lines.append(f"Note: {verdict.preexisting} other failure(s) in the output above were "
                         "already present before your change. They are not your responsibility; "
                         "fix only the new ones listed.")
        return "\n".join(lines)

    @staticmethod
    def _signature(*parts: object) -> str:
        return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]

    def _gates(self, t: Ticket) -> None:
        wt = self._wt(t)
        if not gitops.has_changes_since(wt, t.base_sha):
            verdict = baseline.Verdict(ok=False, new_failures=[baseline.NewFailure(
                ".", "changes", "git diff", ["no changes were made"], "")])
            findings: list[diffguard.Finding] = []
            report, impact = None, None
        else:
            impact = self._impact(wt, t.base_sha)
            verdict, findings, report = self._verify(wt, t.base_sha, impact,
                                                     self._latest(t, "plan"))
        ok = verdict.ok and not diffguard.blocking(findings)
        evidence = self._evidence(wt, t.base_sha, verdict, report, impact)
        self._put(t, "gates", {
            "ok": ok, "summary": verdict.summary(), "unconfigured": verdict.unconfigured,
            "findings": [vars(f) for f in findings], **evidence}, ok=ok)
        if ok:
            t.sessions.pop("gate_fail_sig", None)
            self.store.save_ticket(t)
            self._move(t, State.IN_REVIEW, "engine", verdict.summary())
            return
        feedback = self._failure_text(verdict, findings)
        sig = self._signature([(f.gate, f.new) for f in verdict.new_failures],
                              [(x.kind, x.file) for x in diffguard.blocking(findings)])
        capped = t.implement_attempts + 1 > self.cfg.limits.max_implement_attempts
        feedback = self._maybe_triage(t, "gate_fail_sig", sig,
                                      "The same gate failures appeared twice in a row.",
                                      feedback, cap_will_end_it=capped) or feedback
        t.sessions["pending_feedback"] = feedback
        self.store.save_ticket(t)
        self._move(t, State.IMPLEMENTING, "engine", "gates failed")

    def _maybe_triage(
        self, t: Ticket, key: str, sig: str, situation: str, facts: str, *, cap_will_end_it: bool
    ) -> str | None:
        """If this failure repeats the last one, ask the triage agent what to do.

        Skipped when the retry cap is about to escalate the ticket anyway: a triage call
        could not change the outcome, only cost a model run.
        """
        previous = t.sessions.get(key)
        t.sessions[key] = sig
        self.store.save_ticket(t)
        if previous != sig or cap_will_end_it:
            return None
        req = self._request(t, "triage", prompts.triage_task(situation, facts),
                            schema="triage", read_only=True)
        decision = self._plain("triage", req).data
        self.store.add_event(t.id, "triage", decision)
        if decision["decision"] == "escalate":
            raise _Escalate(f"triage: {decision['reason']}")
        if decision["decision"] == "replan":
            self._put(t, "plan_feedback", {"text": decision.get("message_to_coder")
                                           or decision["reason"]})
            self.store.transition(t, State.ESCALATED, actor="triage", reason="replan")
            self._move(t, State.REFINING, "triage", decision["reason"])
            raise _Wait("replanning after triage")
        return f"{facts}\n\nTriage note: {decision.get('message_to_coder') or decision['reason']}"

    # ------------------------------------------------------------------ IN_REVIEW

    def _pr_number(self, t: Ticket) -> int:
        return int(t.sessions["pr"])

    def _in_review(self, t: Ticket) -> None:
        wt, plan = self._wt(t), self._latest(t, "plan") or {}
        # Push on EVERY pass, not only when the PR is created: after a review round the coder's
        # fix is a new commit, and the review (and the human's merge approval) must be about
        # the commit that is actually on GitHub.
        if self.dry_run:
            self.log(f"dry-run: would push {t.branch}")
        else:
            gitops.push_and_verify(wt, t.branch)
        if "pr" not in t.sessions:
            body = (f"Closes #{t.issue}\n\n**Goal:** {plan.get('goal', '')}\n\n"
                    + "\n".join(f"- {a['id']}: {a['text']}" for a in plan.get("acceptance_criteria", []))
                    + "\n\n_Opened by devflow._")
            pr = self.host.create_pr(t.branch, self.cfg.default_branch, f"{t.title} (#{t.issue})", body)
            t.sessions["pr"] = str(pr.number)
            self.store.save_ticket(t)

        if self.cfg.require_integration and not any(
                p.gates.get("integration") for p in self.cfg.projects):
            raise _Escalate("review requires an integration gate (require_integration = true) "
                            "but none is configured. Define one in .devflow/config.toml.")
        head = gitops.head_sha(wt)
        review_wt = worktree.create_detached(
            self.cfg.root, self.cfg.worktree_root / f"{t.branch.split('/', 1)[-1]}-review", head)
        impact = self._impact(review_wt, t.base_sha)
        verdict, findings, report = self._verify(review_wt, t.base_sha, impact, plan)
        evidence = self._evidence(review_wt, t.base_sha, verdict, report, impact)
        ui_verification, ui_blocking, ui_advisory = self._ui_verification(t, review_wt, plan, report)
        blast = self._blast_radius(review_wt, t.base_sha, impact)
        facts = verdict.summary() + (f"\nNot configured: {', '.join(verdict.unconfigured)}"
                                     if verdict.unconfigured else "")
        stat = gitops.diff_stat(review_wt, t.base_sha)
        diff = gitops.git(review_wt, "diff", f"{t.base_sha}...HEAD", timeout=120)
        req = self._request(t, "reviewer",
                            prompts.reviewer_task(plan, self._latest(t, "test_plan") or {}, facts,
                                                  stat, t.base_sha, diff, blast_radius=blast,
                                                  ui_verification=(ui_verification or {}).get("table")),
                            schema="review", read_only=True, cwd=review_wt,
                            files=gitops.diff_names(review_wt, t.base_sha))
        routed = self._review_run(t, req)
        review = routed.result.data

        # The evaluator can only add blockers; the review's own findings stay as they were.
        findings_all = [*review["findings"], *ui_blocking, *ui_advisory]
        blocking = [f for f in findings_all if f.get("blocking")]
        engine_ok = verdict.ok and not diffguard.blocking(findings)
        approved = review["verdict"] == "approve" and not blocking and engine_ok
        if review["verdict"] == "approve" and not engine_ok:
            blocking.append({"id": "engine-gates", "severity": "blocker", "blocking": True,
                             "file": "(gates)", "summary": "The reviewer approved but the engine's "
                             "own gate run failed:\n" + self._failure_text(verdict, findings)})
        self._put(t, "review", {"agent": routed.agent, "independent": routed.independent,
                                "sha": head,
                                "verdict": review["verdict"], "approved": approved,
                                "findings": findings_all, "engine_gates_ok": engine_ok,
                                "facts": facts, "notes": review["integration"].get("notes", ""),
                                "impact": evidence["impact"], "gate_results": evidence["results"],
                                "reply": routed.result.text[-3000:],
                                **({"ui_verification": ui_verification} if ui_verification else {})},
                  ok=approved)
        self._say(t, self._format_review({**review, "findings": findings_all}, routed.agent,
                                         routed.independent, facts, approved,
                                         (ui_verification or {}).get("table")),
                  number=self._pr_number(t))

        if approved:
            t.sessions["reviewed_sha"] = head
            t.sessions.pop("review_fail_sig", None)
            self.store.save_ticket(t)
            if self._gate_on("merge_approval"):
                self.notifier.send(f"#{t.issue} ({t.title}): reviewed and ready to merge.")
            self._move(t, State.AWAITING_MERGE_APPROVAL, routed.agent, "approved")
            return
        if not blocking:
            # The schema normally prevents this; a repo-local schema could loosen it.
            raise _Escalate("the reviewer requested changes without any blocking finding: "
                            + review["integration"].get("notes", "")[:300])
        feedback = "\n\n".join(
            f"[{f.get('severity')}] {f.get('file')}{':' + str(f['line']) if f.get('line') else ''} "
            f"- {f.get('summary')}" + (f"\nSuggested: {f['suggested_fix']}" if f.get("suggested_fix") else "")
            for f in blocking) or "The reviewer requested changes without blocking findings."
        sig = self._signature(sorted(f.get("summary", "") for f in blocking))
        capped = t.review_round + 1 > self.cfg.limits.max_review_rounds
        feedback = self._maybe_triage(t, "review_fail_sig", sig,
                                      "The reviewer raised the same blocking findings twice "
                                      "in a row.", feedback, cap_will_end_it=capped) or feedback
        t.sessions["pending_feedback"] = feedback
        self.store.save_ticket(t)
        self._move(t, State.CHANGES_REQUESTED, routed.agent, f"{len(blocking)} blocking finding(s)")

    def _review_run(self, t: Ticket, req: AgentRequest):
        """Run a reviewer-class request: the dedicated reviewer agent if any, else routed
        away from the coder's tool."""
        if "reviewer" in self.agents:
            # A dedicated supervisor can review without entering the coding-agent pool.
            from .router import RoutedResult
            reviewer = self.agents["reviewer"]
            result = run_structured(reviewer, req, self.cfg.root)
            self._record_direct(reviewer, "reviewer", req, result)
            return RoutedResult(result, reviewer.name, switched=False,
                                independent=reviewer.name != t.sessions.get("coder_agent", ""))
        return run_routed(self.router, req, t.id, avoid={t.sessions.get("coder_agent", "")},
                          structured=True, root=self.cfg.root)

    def _ui_verification(self, t: Ticket, review_wt: Path, plan: dict, report: GateReport | None
                         ) -> tuple[dict | None, list[dict], list[dict]]:
        """The use-case table, plus blocking and advisory findings from the evaluator.

        The evaluator runs once, only after the deterministic lane ran and passed, and the
        engine corrects whatever it says against the lane's own report.
        """
        lane = report.ui if report is not None else None
        matrix = [u for u in plan.get("ui_use_cases", []) if isinstance(u, dict)]
        if lane is None or not lane.ran or not matrix:
            return None, [], []
        matrix = matrix[:12]
        evaluation, blocking, advisory = None, [], []
        if lane.ok:
            evaluation = self._ui_evaluation(t, review_wt, plan, lane)
            evaluation, blocking = uilane.reconcile(matrix, lane.report, evaluation)
            advisory = [{"id": str(f.get("id", "ui-finding")), "severity": f.get("severity", "nit"),
                         "blocking": False, "file": "(ui evaluator)",
                         "summary": str(f.get("summary", ""))}
                        for f in evaluation.get("findings", [])
                        if isinstance(f, dict) and not f.get("blocking")]
        rows = uilane.verification_rows(matrix, lane.report, evaluation)
        return ({"table": uilane.format_table(matrix, lane.report, evaluation), "rows": rows,
                 "evaluated": evaluation is not None,
                 "notes": str((evaluation or {}).get("notes", ""))[:1000]},
                blocking, advisory)

    def _ui_evaluation(self, t: Ticket, review_wt: Path, plan: dict, lane) -> dict:
        digest = uilane.bounded_digest(lane.report)
        req = self._request(t, "ui_evaluator",
                            prompts.ui_evaluator_task(plan, plan.get("acceptance_criteria", []),
                                                      digest),
                            schema="ui_evaluation", read_only=True, cwd=review_wt)
        return self._review_run(t, req).result.data

    @staticmethod
    def _format_review(review: dict, agent: str, independent: bool, facts: str, approved: bool,
                       ui_table: str | None = None) -> str:
        head = "approved" if approved else "changes requested"
        note = "" if independent else "\n_Note: reviewed by the same tool that wrote the code._"
        items = "\n".join(f"- **{f['severity']}**{' (blocking)' if f.get('blocking') else ''} "
                          f"`{f['file']}`: {f['summary']}" for f in review["findings"]) or "- none"
        table = f"\n\n{ui_table}" if ui_table else ""
        return f"**devflow review ({agent}): {head}**{note}\n\n{items}\n\n_Gates:_ {facts}{table}"

    def _changes_requested(self, t: Ticket) -> None:
        self._move(t, State.IMPLEMENTING, "engine", "addressing review")

    # ------------------------------------------------------------------ merge

    def _awaiting_merge_approval(self, t: Ticket) -> None:
        number = self._pr_number(t)
        pr = self.host.get_pr(number)
        if pr.state == "merged":
            self._finish(t)
            return
        if pr.state == "closed":
            raise _Escalate("the pull request was closed without merging")
        if not t.sessions.get("reviewed_sha") and not self.dry_run:
            # Never ask a person to approve (or merge) a commit we cannot name as reviewed.
            raise _Escalate("there is no record of which commit was reviewed, so the merge "
                            "cannot be pinned to it. Comment /resume to have it reviewed again.")
        approved_by_human = False
        if self._gate_on("merge_approval"):
            cmds = self._commands(t, number, ("approve", "reject"))
            if not cmds:
                raise _Wait(f"waiting for /approve on PR #{number}")
            cmd = cmds[0]
            self._consume(t, cmd)
            if cmd.kind == "reject":
                t.sessions["pending_feedback"] = cmd.text or "Merge was rejected."
                self.store.save_ticket(t)
                self._move(t, State.CHANGES_REQUESTED, f"human:{cmd.author}", "merge rejected")
                return
            approved_by_human = True
        checks = self.host.checks(number)
        if checks == "pending":
            if approved_by_human:
                self._unconsume_last(t)  # keep the approval; just wait for CI
            raise _Wait("CI checks are still running")
        if checks == "failure":
            t.sessions["pending_feedback"] = "The repository's CI checks failed on the pull request."
            self.store.save_ticket(t)
            self._move(t, State.CHANGES_REQUESTED, "engine", "CI failed")
            return
        reviewed = t.sessions.get("reviewed_sha", "")
        if not self.dry_run:
            on_github = gitops.remote_head(self._wt(t), t.branch)
            if on_github != reviewed:
                raise _Escalate(
                    f"the pull request branch is at {on_github[:8] or 'nothing'} but the "
                    f"approved review was of {reviewed[:8]}. Someone pushed after review; "
                    "refusing to merge a commit nobody reviewed.")
        self.host.merge_pr(number, reviewed)
        self._finish(t)

    def _unconsume_last(self, t: Ticket) -> None:
        """Approval stays valid while CI runs: re-read it next time instead of losing it."""
        events = self.store.events(t.id, "command")
        if events:
            self.store.add_event(t.id, "command_returned", {"comment_id": events[-1]["comment_id"]})

    def _finish(self, t: Ticket) -> None:
        self._move(t, State.MERGED, "engine", "merged")
        text = f"#{t.issue} ({t.title}) was merged."
        text += self._file_followups(t)
        self._say(t, text)
        if self._gate_on("merge_approval"):
            self.notifier.send(text)
        self._learn(t, "merged")
        if not self.dry_run:
            worktree.remove(self.cfg.root, self._wt(t), delete_branch=True)
            review = self.cfg.worktree_root / f"{t.branch.split('/', 1)[-1]}-review"
            if review.exists():
                worktree.remove(self.cfg.root, review)

    def _file_followups(self, t: Ticket) -> str:
        """Turn what the ticket uncovered into Backlog tickets. Returns text for the comment."""
        try:
            found = followups.collect(self._latest(t, "plan"), self._latest(t, "review"))
            if not found:
                return ""
            report = followups.file_followups(self.host, self.board, self.store,
                                              self.cfg.board, t, found)
        except (HostError, BoardError) as exc:
            self.store.add_event(t.id, "followup_error", {"error": str(exc)})
            self.log(f"{t.id}: could not file follow-ups: {exc}")
            return ""
        if not report.filed:
            return ""
        lines = "\n".join(f"- #{f.issue.number} {f.candidate.title}" for f in report.filed)
        return ("\n\nFiled in Backlog (move to Ready to work them):\n" + lines)

    def _learn(self, t: Ticket, outcome: str, reason: str = "", *, model: str | None = None) -> None:
        """Capture what the ticket taught and file candidate lessons. Never fails the ticket."""
        learning = self.cfg.learning
        if not learning.enabled or "retro" not in self.agents or self.dry_run:
            return
        episode = sum(1 for e in self.store.events(t.id)
                      if e["kind"] == "transition" and e.get("to") == "escalated")
        key = "merged" if outcome == "merged" else f"escalated#{episode}"
        if any(e.get("key") == key for e in self.store.events(t.id, "learned")):
            return
        try:
            record = capture(self.store, t, outcome, reason)
            telemetry.event("workflow.summary", **{
                "devflow.total_seconds": record["seconds_total"],
                "devflow.review_rounds": record["review_rounds"],
                "devflow.human_wait_seconds": sum(v for k, v in record["seconds_by_state"].items()
                                                  if k.startswith("awaiting_")),
                "devflow.result": outcome})
            self.store.add_event(t.id, "run_record", {"key": key, "record": record})
            graphctx.ensure_fresh(self.cfg.root, builder=self.graph_builder)
            ledger = Ledger(ledger_path(self.cfg.root))
            ledger.start_run()
            report = retro.distill(
                self.agents["retro"], record, ledger, root=self.cfg.root,
                system=prompts.role_prompt(self.cfg.root, "retro"), cwd=self.cfg.root,
                ticket=t.issue or 0, promote_after=learning.promote_after,
                timeout_s=self.cfg.stage_timeout_minutes * 60, model=model)
            ledger.retire_stale(learning.max_idle_runs)
            ledger.save()
        except (AgentError, OSError, ValueError, KeyError) as exc:
            self.store.add_event(t.id, "learning_error", {"error": str(exc)[:300], "key": key})
            self.log(f"{t.id}: learning failed: {exc}")
            return
        self.store.add_event(t.id, "learned", {
            "key": key, "proposed": report.proposed, "added": [x.id for x in report.added],
            "rejected": [{"statement": s, "why": why} for s, why in report.rejected]})

    def _apply_learn_commands(self, t: Ticket) -> None:
        """`/learn [type:] statement` from an approver is a lesson a person stated: active now."""
        if not self.cfg.learning.enabled or self.dry_run or t.issue is None:
            return
        numbers = [t.issue] + ([int(t.sessions["pr"])] if t.sessions.get("pr") else [])
        for number in numbers:
            for cmd in self._commands(t, number, ("learn",)):
                self._consume(t, cmd)
                kind, statement = parse_learn(cmd.text)
                ledger = Ledger(ledger_path(self.cfg.root))
                try:
                    lesson = ledger.add(
                        type=kind, statement=statement, scope=None, ticket=t.issue,
                        evidence=f"stated by {cmd.author}", source="human",
                        promote_after=self.cfg.learning.promote_after)
                    ledger.save()
                    reply = f"Recorded as lesson `{lesson.id}` ({lesson.type}, {lesson.status})."
                except LessonRejected as exc:
                    reply = f"I could not record that lesson: {exc}."
                self._say(t, reply, number=number)

    def cleanup_dry_run(self) -> None:
        """Remove the local worktrees and branches a dry run created. Nothing was pushed."""
        for t in self.store.list_tickets():
            for path in (t.worktree, str(self.cfg.worktree_root
                                         / f"{t.branch.split('/', 1)[-1]}-review")):
                if path and Path(path).exists():
                    worktree.remove(self.cfg.root, Path(path), delete_branch=path == t.worktree)

    # ------------------------------------------------------------------ escalation

    def _escalated(self, t: Ticket) -> None:
        cmds = self._commands(t, t.issue or 0, ("resume",))
        if not cmds:
            raise _Wait("escalated; waiting for /resume on the issue")
        self._consume(t, cmds[0])
        t.review_round, t.implement_attempts = 0, 0
        t.sessions.pop("gate_fail_sig", None)
        t.sessions.pop("review_fail_sig", None)
        totals = self.store.totals_for_ticket(t.id)
        t.sessions["budget_base"] = f"{totals['cost_usd']}:{totals['tokens']}"
        self.store.save_ticket(t)
        back = State.IMPLEMENTING if self._latest(t, "test_plan") else State.REFINING
        self._move(t, back, f"human:{cmds[0].author}", "resumed")
