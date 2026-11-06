"""Ticket state machine.

Pure data and checks, no I/O. The engine owns transitions; agents only supply the inputs
(a plan, a review verdict) that the engine uses to pick the next state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class State(str, Enum):
    NEW = "new"
    REFINING = "refining"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    TEST_DESIGN = "test_design"
    IMPLEMENTING = "implementing"
    GATES = "gates"
    IN_REVIEW = "in_review"
    CHANGES_REQUESTED = "changes_requested"
    AWAITING_MERGE_APPROVAL = "awaiting_merge_approval"
    MERGED = "merged"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


TERMINAL = {State.MERGED, State.CANCELLED}

# Human gates are always states. If a gate is disabled in config the engine passes through it
# with actor="auto", so the audit trail looks the same either way.
TRANSITIONS: dict[State, set[State]] = {
    State.NEW: {State.REFINING},
    State.REFINING: {State.AWAITING_CLARIFICATION, State.AWAITING_PLAN_APPROVAL},
    State.AWAITING_CLARIFICATION: {State.REFINING},
    State.AWAITING_PLAN_APPROVAL: {State.TEST_DESIGN, State.REFINING},
    State.TEST_DESIGN: {State.IMPLEMENTING, State.REFINING},
    State.IMPLEMENTING: {State.GATES},
    State.GATES: {State.IN_REVIEW, State.IMPLEMENTING},
    State.IN_REVIEW: {State.AWAITING_MERGE_APPROVAL, State.CHANGES_REQUESTED},
    State.CHANGES_REQUESTED: {State.IMPLEMENTING},
    State.AWAITING_MERGE_APPROVAL: {State.MERGED, State.CHANGES_REQUESTED},
    State.ESCALATED: {State.REFINING, State.IMPLEMENTING, State.IN_REVIEW},
    State.MERGED: set(),
    State.CANCELLED: set(),
}


class TransitionError(ValueError):
    """The requested transition is not allowed from the current state."""


class LimitExceeded(RuntimeError):
    """A retry cap was hit. The engine should escalate to a human instead of looping."""


@dataclass
class Limits:
    max_review_rounds: int = 3
    max_implement_attempts: int = 3
    max_ticket_cost_usd: float = 0.0  # 0 disables; counted from the last start or /resume
    max_ticket_tokens: int = 0  # 0 disables; input + output tokens, same window


@dataclass
class Ticket:
    id: str
    state: State = State.NEW
    review_round: int = 0
    implement_attempts: int = 0
    history: list[dict[str, str]] = field(default_factory=list)
    issue: int | None = None
    title: str = ""
    branch: str = ""
    worktree: str = ""
    base_sha: str = ""
    sessions: dict[str, str] = field(default_factory=dict)


def allowed_next(state: State) -> set[State]:
    targets = set(TRANSITIONS[state])
    if state not in TERMINAL:
        targets |= {State.ESCALATED, State.CANCELLED}
    return targets


def advance(
    ticket: Ticket, to: State, *, actor: str, reason: str = "", limits: Limits | None = None
) -> Ticket:
    """Move a ticket to `to`, enforcing the table and the retry caps.

    Raises TransitionError for an illegal move and LimitExceeded when a cap is reached.
    In both cases the ticket is left unchanged.
    """
    limits = limits or Limits()
    if to not in allowed_next(ticket.state):
        raise TransitionError(f"{ticket.id}: {ticket.state.value} -> {to.value} is not allowed")

    review_round = ticket.review_round
    attempts = ticket.implement_attempts
    if to is State.CHANGES_REQUESTED:
        review_round += 1
        if review_round > limits.max_review_rounds:
            raise LimitExceeded(f"{ticket.id}: more than {limits.max_review_rounds} review rounds")
    if to is State.IMPLEMENTING and ticket.state is State.GATES:
        attempts += 1
        if attempts > limits.max_implement_attempts:
            raise LimitExceeded(
                f"{ticket.id}: gates failed after {limits.max_implement_attempts} attempts"
            )

    ticket.history.append(
        {"from": ticket.state.value, "to": to.value, "actor": actor, "reason": reason}
    )
    ticket.state = to
    ticket.review_round = review_round
    ticket.implement_attempts = attempts
    return ticket
