"""Bounded queue execution with exclusive ownership and stage-boundary stop checks."""
from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import checkpoint
from .board import BoardError, status_for
from .scheduler import select
from .states import TERMINAL


class RunnerBusy(ValueError):
    pass


@contextmanager
def ownership(root: Path):
    # Every git worktree of the same repo shares this lock in the common git directory.
    from .gitops import git
    common = Path(git(root, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = root / common
    with (common / "devflow-runner.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunnerBusy("another devflow ticket or queue runner owns this repository") from exc
        try:
            lock.seek(0)
            lock.truncate()
            lock.write(str(os.getpid()))
            lock.flush()
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@dataclass
class RunLimits:
    max_tickets: int = 10
    max_minutes: float = 60
    max_cost_usd: float = 0  # 0 disables the recorded-cost ceiling


@dataclass
class RunReport:
    reason: str
    outcomes: list[dict] = field(default_factory=list)


class QueueRunner:
    def __init__(self, engine, limits: RunLimits, *, now=time.monotonic, sleep=time.sleep):
        self.engine, self.limits, self.now, self.sleep = engine, limits, now, sleep
        self.started = now()
        self.start_cost = self.cost()
        self.seen: set[int] = set()
        self.admitted: set[int] = set()
        self.outcomes: list[dict] = []

    def cost(self) -> float:
        return sum(self.engine.store.usage_since(name, 0)["cost_usd"]
                   for name in self.engine.router.agents)

    def guard(self) -> str:
        root = self.engine.cfg.root
        if checkpoint.read(root)["pending"]:
            return "three-ticket checkpoint awaiting your continue reply"
        if (root / ".devflow" / "STOP").exists():
            return "STOP file found; remove .devflow/STOP before resuming"
        if self.now() - self.started >= self.limits.max_minutes * 60:
            return "run time limit reached"
        if self.limits.max_cost_usd and self.cost() - self.start_cost >= self.limits.max_cost_usd:
            return "recorded model cost limit reached"
        return ""

    def notify_pending(self) -> None:
        root = self.engine.cfg.root
        data = checkpoint.read(root)
        if not data["pending"] or data.get("notified") or not data.get("message"):
            return
        if self.engine.notifier.send(data["message"]):
            data["notified"] = True
            checkpoint.write(root, data)
        else:
            self.engine.log("checkpoint notification failed; will retry on the next run")

    def reconcile(self):
        # Cached board_status must not mask an external board edit or a previous write failure.
        board = self.engine.board
        statuses = {i.issue: i.status for i in board.items()}
        for ticket in self.engine.store.list_tickets():
            if ticket.issue not in statuses:
                continue
            wanted = status_for(ticket.state, self.engine.cfg.board.statuses)
            if statuses[ticket.issue] != wanted:
                try:
                    board.set_status(ticket.issue, wanted)
                except BoardError as exc:
                    self.engine.log(f"board reconciliation for {ticket.id}: {exc}")

    def sweep(self) -> bool:
        engine = self.engine
        self.reconcile()
        attempted = False
        # Recheck waiting tickets too: a new /approve or /answer may now be available.
        for ticket in engine.store.list_tickets():
            if ticket.state in TERMINAL or ticket.issue is None or ticket.issue in self.seen:
                continue
            if self.guard() or len(self.admitted) >= self.limits.max_tickets:
                return attempted
            self.execute(ticket.issue)
            attempted = True
        while not self.guard() and len(self.admitted) < self.limits.max_tickets:
            selection = select(engine.board, engine.host, engine.store, engine.cfg.board)
            if selection.pick is None:
                break
            issue = selection.pick.issue
            if issue in self.seen:
                break
            self.execute(issue)
            attempted = True
        return attempted

    def execute(self, issue: int):
        self.seen.add(issue)
        self.admitted.add(issue)
        result = self.engine.run(issue)
        if result.ticket.state.value == "merged" and not self.engine.dry_run:
            reached = checkpoint.merged(self.engine.cfg.root, issue,
                                        result.ticket.sessions.get("pr", ""), result.ticket.title)
            if reached:
                lines = [f"- #{x['issue']} {x['title']} (PR #{x['pr']})"
                         for x in reached["completed"]]
                suggestions = ""
                agent = self.engine.agents.get("planner")
                if agent:
                    from .agents import AgentError, AgentRequest
                    try:
                        response = agent.run(AgentRequest(
                            role="checkpoint", cwd=self.engine.cfg.root,
                            system="You oversee this product. Reply in plain text with 2-3 "
                                   "specific, bounded next-ticket ideas and why they matter. "
                                   "Do not run commands or approve work.",
                            prompt="Three merged tickets:\n" + "\n".join(lines),
                            read_only=True, timeout_s=180))
                        if response.ok:
                            suggestions = response.text[:1800].strip()
                    except (AgentError, OSError, ValueError) as exc:
                        self.engine.log(f"checkpoint recommendations unavailable: {type(exc).__name__}")
                message = ("Devflow completed three tickets:\n" + "\n".join(lines)
                           + "\n\nSuggested next steps:\n" +
                           (suggestions or "Review the Project backlog and choose the next Ready tickets.")
                           + "\n\nReply 'continue' after reading this update.")
                reached["message"] = message
                reached["notified"] = False
                checkpoint.write(self.engine.cfg.root, reached)
                self.notify_pending()
        self.outcomes.append({"issue": issue, "status": result.status,
                              "state": result.ticket.state.value, "reason": result.reason})
        self.engine.log(f"#{issue}: {result.status} ({result.reason})")

    def run(self, watch: int = 0) -> RunReport:
        if self.engine.board is None:
            raise ValueError("configure a GitHub Project before running the queue")
        old_guard = self.engine.checkpoint
        self.engine.checkpoint = self.guard
        try:
            self.notify_pending()
            while True:
                if self.guard():
                    return RunReport(self.guard(), self.outcomes)
                self.sweep()
                if self.guard():
                    return RunReport(self.guard(), self.outcomes)
                if len(self.admitted) >= self.limits.max_tickets:
                    return RunReport("ticket limit reached", self.outcomes)
                if not watch:
                    return RunReport("sweep complete; waiting tickets can be resumed next run", self.outcomes)
                # Sleep in short increments so STOP and time limits remain responsive.
                for _ in range(watch):
                    if self.guard():
                        return RunReport(self.guard(), self.outcomes)
                    self.sleep(1)
                # Count distinct tickets per invocation, but permit existing ones to resume.
                self.seen.clear()
        finally:
            self.engine.checkpoint = old_guard


def command(args) -> int:
    from .detect import repo_root
    from .host import GhHost, repo_slug
    from .loadconfig import load_config
    from .runtime import build_engine, make_board
    from .store import Store, state_db_path
    if args.max_tickets < 1 or args.max_minutes <= 0 or args.max_cost_usd < 0 or args.watch < 0:
        raise ValueError("ticket/time limits must be positive; cost and watch must be nonnegative")
    root = repo_root(Path(args.path).resolve())
    if root is None:
        raise ValueError("not inside a git repository")
    if args.dry_run:
        cfg = load_config(root)
        board = make_board(cfg)
        if board is None:
            raise ValueError("configure a GitHub Project before running the queue")
        with Store(state_db_path(root)) as store:
            selection = select(board, GhHost(repo_slug(cfg.remote)), store, cfg.board)
        print("Preview only: no agents launched or GitHub changes made")
        for item in selection.ready:
            print(f"Ready: #{item.issue} {item.title}")
        print("next: " + (f"#{selection.pick.issue}" if selection.pick else selection.reason))
        return 0
    with ownership(root):
        engine, store, _ = build_engine(root, log=print)
        try:
            limits = RunLimits(args.max_tickets, args.max_minutes, args.max_cost_usd)
            report = QueueRunner(engine, limits).run(args.watch)
            print(report.reason)
        except KeyboardInterrupt:
            print("interrupted; saved ticket states will resume on the next run")
            return 130
        finally:
            store.close()
    return 0
