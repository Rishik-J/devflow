import pytest

from devflow.states import (
    TERMINAL,
    TRANSITIONS,
    LimitExceeded,
    Limits,
    State,
    Ticket,
    TransitionError,
    advance,
)

HAPPY_PATH = [
    State.REFINING, State.AWAITING_PLAN_APPROVAL, State.TEST_DESIGN, State.IMPLEMENTING,
    State.GATES, State.IN_REVIEW, State.AWAITING_MERGE_APPROVAL, State.MERGED,
]


def walk(ticket: Ticket, path: list[State]) -> Ticket:
    for to in path:
        advance(ticket, to, actor="engine")
    return ticket


def test_happy_path_reaches_merged_with_full_history():
    t = walk(Ticket("T-1"), HAPPY_PATH)
    assert t.state is State.MERGED
    assert [h["to"] for h in t.history] == [s.value for s in HAPPY_PATH]


def test_illegal_transition_is_rejected_and_ticket_unchanged():
    t = Ticket("T-2")
    with pytest.raises(TransitionError):
        advance(t, State.MERGED, actor="agent")
    assert t.state is State.NEW and t.history == []


def test_terminal_states_have_no_exit():
    for state in TERMINAL:
        t = Ticket("T-3", state=state)
        for target in State:
            with pytest.raises(TransitionError):
                advance(t, target, actor="engine")


def test_any_live_state_can_escalate_or_cancel():
    for state in State:
        if state in TERMINAL:
            continue
        for target in (State.ESCALATED, State.CANCELLED):
            if state is State.ESCALATED and target is State.ESCALATED:
                continue
            t = Ticket("T-4", state=state)
            advance(t, target, actor="engine", reason="test")
            assert t.state is target


def test_every_state_is_in_the_table():
    assert set(TRANSITIONS) == set(State)


def test_review_rounds_are_capped():
    limits = Limits(max_review_rounds=2)
    t = walk(Ticket("T-5"), HAPPY_PATH[:6])
    for _ in range(2):
        advance(t, State.CHANGES_REQUESTED, actor="reviewer", limits=limits)
        advance(t, State.IMPLEMENTING, actor="engine", limits=limits)
        advance(t, State.GATES, actor="engine", limits=limits)
        advance(t, State.IN_REVIEW, actor="engine", limits=limits)
    with pytest.raises(LimitExceeded):
        advance(t, State.CHANGES_REQUESTED, actor="reviewer", limits=limits)
    assert t.state is State.IN_REVIEW and t.review_round == 2


def test_gate_failures_are_capped():
    limits = Limits(max_implement_attempts=2)
    t = walk(Ticket("T-6"), HAPPY_PATH[:5])
    for _ in range(2):
        advance(t, State.IMPLEMENTING, actor="engine", limits=limits)
        advance(t, State.GATES, actor="engine", limits=limits)
    with pytest.raises(LimitExceeded):
        advance(t, State.IMPLEMENTING, actor="engine", limits=limits)
    assert t.state is State.GATES


def test_first_implementation_does_not_count_as_a_retry():
    t = walk(Ticket("T-7"), HAPPY_PATH[:4])
    assert t.state is State.IMPLEMENTING and t.implement_attempts == 0


def test_escalated_ticket_can_be_resumed():
    t = walk(Ticket("T-8"), HAPPY_PATH[:4])
    advance(t, State.ESCALATED, actor="triage", reason="stuck")
    advance(t, State.IMPLEMENTING, actor="human", reason="answered")
    assert t.state is State.IMPLEMENTING
