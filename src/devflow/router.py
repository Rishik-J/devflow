"""Route coding-heavy work across claude, codex and cursor by credits and usage.

None of these tools exposes a reliable "credits remaining" number, so routing works two ways:

* Proactive: every run is recorded (real cost when the tool reports it, otherwise an estimate
  from token counts), and each agent has a budget window in config. The agent with the most
  headroom left in its window gets the next job.
* Reactive: when an agent reports a rate or usage limit, it is put on a cooldown that doubles
  on repeats, and the job falls through to the next agent in the same call.

Sessions are not portable between tools, so a job that moves to another agent starts a new
session; the caller is told (`switched`) so it can re-brief the new agent.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from . import telemetry
from .agents import Agent, AgentError, AgentRequest, AgentResult, check_ready, make_agent
from .agents.base import _with, run_structured
from .loadconfig import AgentBudget, Config, RoutingCfg
from .store import Store

LIMIT_PATTERN = re.compile(
    r"rate.?limit|usage.?limit|quota|credit|too many requests|\b429\b|overloaded|"
    r"limit (?:reached|exceeded)|out of (?:usage|credits)|insufficient|"
    r"hit your [\w -]{0,20}limit",
    re.IGNORECASE,
)


class NoAgentAvailable(RuntimeError):
    """Every candidate is unavailable, cooling down, or out of budget."""

    def __init__(self, message: str, retry_at: float | None = None):
        super().__init__(message)
        self.retry_at = retry_at


@dataclass
class AgentStatus:
    name: str
    ready: bool
    reason: str
    used_usd: float
    limit_usd: float
    headroom: float
    priority: int
    runs: int
    cooldown_until: float | None = None

    @property
    def eligible(self) -> bool:
        return self.ready and self.cooldown_until is None and self.headroom > 0


@dataclass
class Choice:
    name: str
    agent: Agent
    independent: bool
    reason: str


@dataclass
class RoutedResult:
    result: AgentResult
    agent: str
    switched: bool
    independent: bool


def is_limit_error(text: str) -> bool:
    return bool(LIMIT_PATTERN.search(text or ""))


class Router:
    def __init__(
        self,
        store: Store,
        cfg: RoutingCfg,
        agents: dict[str, Agent],
        *,
        now: Callable[[], float] = time.time,
        ready: Callable[[Agent], tuple[bool, str]] = check_ready,
    ):
        self.store, self.cfg, self.agents = store, cfg, agents
        self.now, self.ready = now, ready

    def routes(self, role: str) -> bool:
        return role in self.cfg.roles

    def budget(self, name: str) -> AgentBudget:
        return self.cfg.agents.get(name, AgentBudget())

    def status(self, name: str) -> AgentStatus:
        budget = self.budget(name)
        now = self.now()
        usage = self.store.usage_since(name, now - budget.window_hours * 3600)
        ok, why = self.ready(self.agents[name])
        cooldown = self.store.get_cooldown(name)
        until = cooldown["until"] if cooldown and cooldown["until"] > now else None
        headroom = 1.0
        if budget.limit_usd > 0:
            headroom = max(0.0, 1.0 - usage["cost_usd"] / budget.limit_usd)
        return AgentStatus(
            name=name, ready=ok, reason=why, used_usd=usage["cost_usd"],
            limit_usd=budget.limit_usd, headroom=headroom, priority=budget.priority,
            runs=int(usage["runs"]), cooldown_until=until,
        )

    def statuses(self) -> list[AgentStatus]:
        return [self.status(n) for n in self.agents]

    def pick(
        self,
        role: str,
        *,
        avoid: Iterable[str] = (),
        prefer: str | None = None,
        exclude: Iterable[str] = (),
    ) -> Choice:
        """Choose an agent for `role`.

        `avoid` is a soft preference (e.g. the reviewer should not be the coder's tool); `exclude`
        is hard (agents already tried and failed in this call); `prefer` keeps a ticket on the
        agent whose session it already has.
        """
        if not self.routes(role):
            raise ValueError(f"role {role!r} is not routed (routing.roles = {self.cfg.roles})")
        excluded, avoided = set(exclude), set(avoid)
        statuses = [s for s in self.statuses() if s.name not in excluded]
        eligible = [s for s in statuses if s.eligible]
        if not eligible:
            raise NoAgentAvailable(self._explain(statuses), self._retry_at(statuses))

        independent = True
        pool = [s for s in eligible if s.name not in avoided]
        if not pool:
            pool, independent = eligible, not avoided
        if prefer and any(s.name == prefer for s in pool):
            chosen = next(s for s in pool if s.name == prefer)
            why = "continuing the ticket's session"
        else:
            if self.cfg.strategy == "priority":
                pool.sort(key=lambda s: (s.priority, -s.headroom, s.name))
            else:
                pool.sort(key=lambda s: (-s.headroom, s.priority, s.name))
            chosen = pool[0]
            why = (f"{chosen.headroom:.0%} headroom" if chosen.limit_usd > 0
                   else f"priority {chosen.priority}, no local cap")
        return Choice(chosen.name, self.agents[chosen.name], independent, why)

    def _retry_at(self, statuses: list[AgentStatus]) -> float | None:
        times = [s.cooldown_until for s in statuses if s.cooldown_until and s.ready]
        return min(times) if times else None

    def _explain(self, statuses: list[AgentStatus]) -> str:
        if not statuses:
            return "no coding agents are configured"
        parts = []
        for s in statuses:
            if not s.ready:
                parts.append(f"{s.name}: not ready ({s.reason or 'unavailable'})")
            elif s.cooldown_until:
                mins = max(0, int((s.cooldown_until - self.now()) / 60))
                parts.append(f"{s.name}: cooling down for ~{mins} min after a limit")
            else:
                parts.append(f"{s.name}: budget window used up (${s.used_usd:.2f}/${s.limit_usd:.2f})")
        return "no coding agent available: " + "; ".join(parts)

    def estimate_cost(self, name: str, result: AgentResult, prompt_chars: int) -> tuple[float, int, int]:
        if result.cost_usd is not None:
            usage = result.raw.get("usage") or {}
            return (result.cost_usd, int(usage.get("input_tokens", 0)),
                    int(usage.get("output_tokens", 0)))
        usage = result.raw.get("usage") or {}
        tin = int(usage.get("input_tokens") or prompt_chars / 4)
        tout = int(usage.get("output_tokens") or len(result.text) / 4)
        b = self.budget(name)
        return tin / 1e6 * b.usd_per_mtok_in + tout / 1e6 * b.usd_per_mtok_out, tin, tout

    def record(self, name: str, role: str, ticket_id: str, prompt_chars: int,
               result: AgentResult) -> bool:
        """Record a finished run. Returns True if it hit a limit (agent put on cooldown)."""
        cost, tin, tout = self.estimate_cost(name, result, prompt_chars)
        self.store.record_usage(name, role, ticket_id, cost_usd=cost, tokens_in=tin,
                                tokens_out=tout, ok=result.ok, error=result.error)
        if result.ok:
            self.store.clear_cooldown(name)
            return False
        return self._maybe_cooldown(name, result.error)

    def record_failure(self, name: str, role: str, ticket_id: str, error: str) -> bool:
        self.store.record_usage(name, role, ticket_id, cost_usd=0.0, ok=False, error=error)
        return self._maybe_cooldown(name, error)

    def _maybe_cooldown(self, name: str, error: str) -> bool:
        if not is_limit_error(error):
            return False
        previous = self.store.get_cooldown(name)
        strikes = (previous["strikes"] if previous else 0) + 1
        minutes = min(self.cfg.cooldown_minutes * 2 ** (strikes - 1),
                      self.cfg.cooldown_max_hours * 60)
        self.store.set_cooldown(name, self.now() + minutes * 60, error, strikes)
        return True


def run_routed(
    router: Router,
    req: AgentRequest,
    ticket_id: str,
    *,
    avoid: Iterable[str] = (),
    prefer: str | None = None,
    structured: bool = False,
    root: Path | None = None,
) -> RoutedResult:
    """Run `req` on the best agent, falling through to the next one if it hits a limit.

    Errors that are not about limits (bad output, timeouts, crashes) are raised, not rerouted:
    switching tools on a real failure would hide it.
    """
    tried: list[str] = []
    prompt_chars = len(req.prompt) + len(req.system)
    for _ in range(len(router.agents)):
        choice = router.pick(req.role, avoid=avoid, prefer=prefer, exclude=tried)
        telemetry.content_event("routing.selected", input={"preferred": prefer, "already_tried": tried},
                                output={"selected": choice.name, "reason": getattr(choice, "reason", "router priority and available headroom")},
                                attributes={"devflow.agent": choice.name, "devflow.role": req.role,
                                            "devflow.fallback_index": len(tried)})
        this = req if (prefer is None or choice.name == prefer) else _with(req, session=None)
        try:
            if structured:
                result = run_structured(choice.agent, this, root)
            else:
                result = choice.agent.run(this)
        except AgentError as exc:
            if router.record_failure(choice.name, req.role, ticket_id, str(exc)):
                tried.append(choice.name)
                continue
            raise
        limited = router.record(choice.name, req.role, ticket_id, prompt_chars, result)
        cost, tokens_in, tokens_out = router.estimate_cost(choice.name, result, prompt_chars)
        telemetry.event("usage.recorded", **{"devflow.agent": choice.name,
                                             "devflow.role": req.role,
                                             "devflow.cost_usd": cost,
                                             "devflow.cost_source": "reported" if result.cost_usd is not None else "estimated",
                                             "devflow.tokens_in": tokens_in,
                                             "devflow.tokens_out": tokens_out})
        if not result.ok:
            if limited:
                tried.append(choice.name)
                continue
            raise AgentError(f"{choice.name} {req.role} failed: {result.error}")
        return RoutedResult(result, choice.name, switched=bool(tried), independent=choice.independent)
    raise NoAgentAvailable("every coding agent hit a limit", router._retry_at(router.statuses()))


def build_router(cfg: Config, store: Store) -> Router:
    names = list(cfg.routing.agents) or [a for a in (cfg.agents.get("coder"),
                                                       cfg.agents.get("reviewer")) if a]
    agents = {n: make_agent(n) for n in dict.fromkeys(names)}
    return Router(store, cfg.routing, agents)
