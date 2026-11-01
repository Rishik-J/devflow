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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import baseline, commands, diffguard, gitops, graphctx, prompts, worktree
from .agents import Agent, AgentError, AgentRequest, AgentResult, run_structured
from .gates import GateReport, run_gates, setup_worktree
from .graphify import build_graph
from .host import Host, HostError, upsert_status
from .loadconfig import Config
from .notify import Notifier, NullNotifier
from .router import NoAgentAvailable, Router, run_routed
from .states import TERMINAL, LimitExceeded, State, Ticket
from .store import Store

MAX_CLARIFICATION_ROUNDS = 3
MAX_PLAN_CYCLES = 4
FEEDBACK_OUTPUT_CHARS = 1500


class _Wait(Exception):
    """Stop here; nothing more can happen until a person acts or time passes."""


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
        dry_run: bool = False,
        log: Callable[[str], None] = lambda message: None,
        max_steps: int = 60,
    ):
        self.cfg, self.store, self.host, self.router = cfg, store, host, router
        self.agents = agents
        self.notifier = notifier or NullNotifier()
        self.graph_builder, self.dry_run, self.log, self.max_steps = (
            graph_builder, dry_run, log, max_steps)
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

    def run(self, issue: int) -> Outcome:
        ticket = self.store.get_ticket(f"#{issue}")
        if ticket is None:
            ticket = Ticket(id=f"#{issue}", issue=issue)
            self.store.save_ticket(ticket)
        for _ in range(self.max_steps):
            if ticket.state in TERMINAL:
                return Outcome(ticket, "done", ticket.state.value)
            before = ticket.state
            try:
                self._handlers[ticket.state](ticket)
            except _Wait as wait:
                return Outcome(ticket, "paused", str(wait))
            except NoAgentAvailable as exc:
                return Outcome(ticket, "paused", str(exc))
            except (_Escalate, LimitExceeded) as exc:
                self._escalate(ticket, str(exc))
                return Outcome(ticket, "escalated", str(exc))
            except (AgentError, HostError, gitops.GitError, worktree.WorktreeError,
                    prompts.PromptError) as exc:
                if self._record_error(ticket, exc) >= 2:
                    reason = f"{type(exc).__name__} twice in {before.value}: {exc}"
                    self._escalate(ticket, reason)
                    return Outcome(ticket, "escalated", reason)
                return Outcome(ticket, "paused", f"{type(exc).__name__}: {exc} (will retry)")
        return Outcome(ticket, "paused", "step limit reached; run again to continue")

    def _record_error(self, ticket: Ticket, exc: Exception) -> int:
        self.store.add_event(ticket.id, "error", {"state": ticket.state.value, "error": str(exc)})
        events = self.store.events(ticket.id)
        last_move = max((i for i, e in enumerate(events) if e["kind"] == "transition"), default=-1)
        return sum(1 for e in events[last_move + 1:]
                   if e["kind"] == "error" and e.get("state") == ticket.state.value)

    # ------------------------------------------------------------------ helpers

    def _move(self, ticket: Ticket, to: State, actor: str, reason: str = "") -> None:
        self.log(f"{ticket.id}: {ticket.state.value} -> {to.value} ({actor}) {reason}".strip())
        self.store.transition(ticket, to, actor=actor, reason=reason, limits=self.cfg.limits)

    def _escalate(self, ticket: Ticket, reason: str) -> None:
        if ticket.state is not State.ESCALATED:
            self.store.transition(ticket, State.ESCALATED, actor="engine", reason=reason[:300])
        text = (f"devflow needs you on #{ticket.issue} ({ticket.title}): {reason}\n"
                "Fix or answer in the issue, then comment `/resume`.")
        self._say(ticket, text)
        self.notifier.send(text)

    def _put(self, ticket: Ticket, kind: str, payload: dict, ok: bool = True) -> None:
        attempt = len(self.store.stage_results(ticket.id, kind)) + 1
        self.store.add_stage_result(ticket.id, kind, attempt, ok, payload)

    def _latest(self, ticket: Ticket, kind: str) -> dict | None:
        rows = self.store.stage_results(ticket.id, kind)
        return rows[-1]["payload"] if rows else None

    def _count(self, ticket: Ticket, kind: str) -> int:
        return len(self.store.stage_results(ticket.id, kind))

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
                                                    "command": cmd.kind, "author": cmd.author})

    def _gate_on(self, name: str) -> bool:
        return self.cfg.human_gates.get(name, True)

    def _wt(self, ticket: Ticket) -> Path:
        return Path(ticket.worktree)

    def _request(
        self, ticket: Ticket, role: str, prompt: str, *, schema: str | None, read_only: bool,
        cwd: Path | None = None, files: list[str] | None = None, session: str | None = None,
    ) -> AgentRequest:
        wt = cwd or self._wt(ticket)
        graphctx.ensure_fresh(wt, builder=self.graph_builder)  # best effort; digest says if absent
        system = prompts.role_prompt(self.cfg.root, role) + "\n\n" + graphctx.agent_preamble(wt, files)
        return AgentRequest(
            role=role, prompt=prompt, cwd=wt, system=system, schema=schema, read_only=read_only,
            session=session, timeout_s=self.cfg.stage_timeout_minutes * 60,
            mcp=graphctx.mcp_config(wt), mcp_overrides=graphctx.codex_overrides(wt),
        )

    def _plain(self, role: str, req: AgentRequest) -> AgentResult:
        agent = self.agents.get(role)
        if agent is None:
            raise AgentError(f"no agent configured for role {role!r}")
        return run_structured(agent, req, self.cfg.root) if req.schema else agent.run(req)

    def _plan_files(self, plan: dict | None) -> list[str]:
        return sorted({f for s in (plan or {}).get("steps", []) for f in s.get("files", [])})

    # ------------------------------------------------------------------ NEW

    def _new(self, t: Ticket) -> None:
        issue = self.host.get_issue(t.issue or 0)
        if issue.state != "open":
            self._move(t, State.CANCELLED, "engine", f"issue is {issue.state}")
            return
        t.title = issue.title
        wt = worktree.create(self.cfg.root, self.cfg.worktree_root, t.issue or 0, issue.title,
                             self.cfg.default_branch)
        t.branch, t.worktree, t.base_sha = wt.branch, str(wt.path), wt.base_sha
        self.store.save_ticket(t)
        self._move(t, State.REFINING, "engine", "started")

    # ------------------------------------------------------------------ REFINING

    def _refining(self, t: Ticket) -> None:
        if self._count(t, "plan") >= MAX_PLAN_CYCLES:
            raise _Escalate(f"the plan was reworked {MAX_PLAN_CYCLES} times without settling")
        setup = setup_worktree(self.cfg, self._wt(t))
        if not setup.ok and setup.results:
            raise _Escalate("environment setup failed: " + setup.results[-1].output_tail[-400:])

        issue = self.host.get_issue(t.issue or 0)
        answers = [e["text"] for e in self.store.events(t.id, "answers")]
        feedback = (self._latest(t, "plan_feedback") or {}).get("text", "")
        previous = self._latest(t, "plan") or {}
        req = self._request(t, "planner",
                            prompts.planner_task(issue, answers, feedback, previous.get("questions")),
                            schema="plan", read_only=True)
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
        self._say(t, self._format_plan(plan))
        self.notifier.send(f"#{t.issue} ({t.title}): plan ready for your approval.")
        self._move(t, State.AWAITING_PLAN_APPROVAL, "planner", "plan ready")

    @staticmethod
    def _format_plan(plan: dict) -> str:
        ac = "\n".join(f"- **{a['id']}** {a['text']}" for a in plan["acceptance_criteria"])
        steps = "\n".join(f"{i}. {s['description']}  `{', '.join(s['files']) or 'no files'}`"
                          for i, s in enumerate(plan["steps"], 1))
        risks = "\n".join(f"- {r}" for r in plan.get("risks", []))
        return (f"**Proposed plan**\n\n**Goal:** {plan['goal']}\n\n**Acceptance criteria**\n{ac}\n\n"
                f"**Steps**\n{steps}\n" + (f"\n**Risks**\n{risks}\n" if risks else "")
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
            self._move(t, State.TEST_DESIGN, "auto", "plan approval disabled")
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
            schema=None, read_only=False, files=self._plan_files(plan), session=session)
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
        self._move(t, State.GATES, routed.agent, "committed" if sha else "no changes")

    # ------------------------------------------------------------------ GATES

    def _verify(self, path: Path, base_sha: str) -> tuple[baseline.Verdict, list[diffguard.Finding], GateReport]:
        """Gate results against the base, plus the diff guard, in `path`."""
        setup = setup_worktree(self.cfg, path)
        if not setup.ok and setup.results:
            return (baseline.Verdict(ok=False, new_failures=[baseline.NewFailure(
                setup.results[-1].project, "setup", setup.results[-1].command, ["setup failed"],
                setup.results[-1].output_tail)]), [], setup)
        report = run_gates(self.cfg, path)
        base = baseline.ensure_baseline(self.cfg, self.store, base_sha)
        verdict = baseline.evaluate(report, base)
        return verdict, diffguard.analyze(path, base_sha), report

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
        else:
            verdict, findings, _ = self._verify(wt, t.base_sha)
        ok = verdict.ok and not diffguard.blocking(findings)
        self._put(t, "gates", {
            "ok": ok, "summary": verdict.summary(), "unconfigured": verdict.unconfigured,
            "findings": [vars(f) for f in findings]}, ok=ok)
        if ok:
            t.sessions.pop("last_fail_sig", None)
            self.store.save_ticket(t)
            self._move(t, State.IN_REVIEW, "engine", verdict.summary())
            return
        feedback = self._failure_text(verdict, findings)
        sig = self._signature([(f.gate, f.new) for f in verdict.new_failures],
                              [(x.kind, x.file) for x in diffguard.blocking(findings)])
        capped = t.implement_attempts + 1 > self.cfg.limits.max_implement_attempts
        feedback = self._maybe_triage(t, sig, "The same gate failures appeared twice in a row.",
                                      feedback, cap_will_end_it=capped) or feedback
        t.sessions["pending_feedback"] = feedback
        self.store.save_ticket(t)
        self._move(t, State.IMPLEMENTING, "engine", "gates failed")

    def _maybe_triage(
        self, t: Ticket, sig: str, situation: str, facts: str, *, cap_will_end_it: bool
    ) -> str | None:
        """If this failure repeats the last one, ask the triage agent what to do.

        Skipped when the retry cap is about to escalate the ticket anyway: a triage call
        could not change the outcome, only cost a model run.
        """
        previous = t.sessions.get("last_fail_sig")
        t.sessions["last_fail_sig"] = sig
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
        if "pr" not in t.sessions:
            if self.dry_run:
                self.log(f"dry-run: would push {t.branch}")
            else:
                gitops.push(wt, t.branch)
            body = (f"Closes #{t.issue}\n\n**Goal:** {plan.get('goal', '')}\n\n"
                    + "\n".join(f"- {a['id']}: {a['text']}" for a in plan.get("acceptance_criteria", []))
                    + "\n\n_Opened by devflow._")
            pr = self.host.create_pr(t.branch, self.cfg.default_branch, f"{t.title} (#{t.issue})", body)
            t.sessions["pr"] = str(pr.number)
            self.store.save_ticket(t)

        head = gitops.head_sha(wt)
        review_wt = worktree.create_detached(
            self.cfg.root, self.cfg.worktree_root / f"{t.branch.split('/', 1)[-1]}-review", head)
        verdict, findings, _ = self._verify(review_wt, t.base_sha)
        facts = verdict.summary() + (f"\nNot configured: {', '.join(verdict.unconfigured)}"
                                     if verdict.unconfigured else "")
        stat = gitops.diff_stat(review_wt, t.base_sha)
        diff = gitops.git(review_wt, "diff", f"{t.base_sha}...HEAD", timeout=120)
        req = self._request(t, "reviewer",
                            prompts.reviewer_task(plan, self._latest(t, "test_plan") or {}, facts,
                                                  stat, t.base_sha, diff),
                            schema="review", read_only=True, cwd=review_wt,
                            files=gitops.diff_names(review_wt, t.base_sha))
        routed = run_routed(self.router, req, t.id, avoid={t.sessions.get("coder_agent", "")},
                            structured=True, root=self.cfg.root)
        review = routed.result.data

        blocking = [f for f in review["findings"] if f.get("blocking")]
        engine_ok = verdict.ok and not diffguard.blocking(findings)
        approved = review["verdict"] == "approve" and not blocking and engine_ok
        if review["verdict"] == "approve" and not engine_ok:
            blocking.append({"id": "engine-gates", "severity": "blocker", "blocking": True,
                             "file": "(gates)", "summary": "The reviewer approved but the engine's "
                             "own gate run failed:\n" + self._failure_text(verdict, findings)})
        self._put(t, "review", {"agent": routed.agent, "independent": routed.independent,
                                "verdict": review["verdict"], "approved": approved,
                                "findings": review["findings"], "engine_gates_ok": engine_ok,
                                "facts": facts}, ok=approved)
        self._say(t, self._format_review(review, routed.agent, routed.independent, facts, approved),
                  number=self._pr_number(t))

        if approved:
            t.sessions.pop("last_fail_sig", None)
            self.store.save_ticket(t)
            self.notifier.send(f"#{t.issue} ({t.title}): reviewed and ready to merge.")
            self._move(t, State.AWAITING_MERGE_APPROVAL, routed.agent, "approved")
            return
        feedback = "\n\n".join(
            f"[{f.get('severity')}] {f.get('file')}{':' + str(f['line']) if f.get('line') else ''} "
            f"- {f.get('summary')}" + (f"\nSuggested: {f['suggested_fix']}" if f.get("suggested_fix") else "")
            for f in blocking) or "The reviewer requested changes without blocking findings."
        sig = self._signature(sorted(f.get("summary", "") for f in blocking))
        capped = t.review_round + 1 > self.cfg.limits.max_review_rounds
        feedback = self._maybe_triage(t, sig, "The reviewer raised the same blocking findings "
                                              "twice in a row.", feedback,
                                      cap_will_end_it=capped) or feedback
        t.sessions["pending_feedback"] = feedback
        self.store.save_ticket(t)
        self._move(t, State.CHANGES_REQUESTED, routed.agent, f"{len(blocking)} blocking finding(s)")

    @staticmethod
    def _format_review(review: dict, agent: str, independent: bool, facts: str, approved: bool) -> str:
        head = "approved" if approved else "changes requested"
        note = "" if independent else "\n_Note: reviewed by the same tool that wrote the code._"
        items = "\n".join(f"- **{f['severity']}**{' (blocking)' if f.get('blocking') else ''} "
                          f"`{f['file']}`: {f['summary']}" for f in review["findings"]) or "- none"
        return f"**devflow review ({agent}): {head}**{note}\n\n{items}\n\n_Gates:_ {facts}"

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
        self.host.merge_pr(number)
        self._finish(t)

    def _unconsume_last(self, t: Ticket) -> None:
        """Approval stays valid while CI runs: re-read it next time instead of losing it."""
        events = self.store.events(t.id, "command")
        if events:
            self.store.add_event(t.id, "command_returned", {"comment_id": events[-1]["comment_id"]})

    def _finish(self, t: Ticket) -> None:
        self._move(t, State.MERGED, "engine", "merged")
        text = f"#{t.issue} ({t.title}) was merged."
        self._say(t, text)
        self.notifier.send(text)
        if not self.dry_run:
            worktree.remove(self.cfg.root, self._wt(t), delete_branch=True)
            review = self.cfg.worktree_root / f"{t.branch.split('/', 1)[-1]}-review"
            if review.exists():
                worktree.remove(self.cfg.root, review)

    # ------------------------------------------------------------------ escalation

    def _escalated(self, t: Ticket) -> None:
        cmds = self._commands(t, t.issue or 0, ("resume",))
        if not cmds:
            raise _Wait("escalated; waiting for /resume on the issue")
        self._consume(t, cmds[0])
        t.review_round, t.implement_attempts = 0, 0
        t.sessions.pop("last_fail_sig", None)
        self.store.save_ticket(t)
        back = State.IMPLEMENTING if self._latest(t, "test_plan") else State.REFINING
        self._move(t, back, f"human:{cmds[0].author}", "resumed")
