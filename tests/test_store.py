from pathlib import Path

import pytest

from devflow.states import LimitExceeded, Limits, State, Ticket, TransitionError
from devflow.store import Store, state_db_path


@pytest.fixture
def store(tmp_path: Path):
    with Store(state_db_path(tmp_path)) as s:
        yield s


def test_ticket_roundtrip_with_extra_fields(store: Store):
    t = Ticket("T-1", issue=42, title="Add x", branch="devflow/42-add-x", worktree="/w",
               base_sha="abc", sessions={"coder": "s1"})
    store.save_ticket(t)
    got = store.get_ticket("T-1")
    assert got == t and store.get_ticket("nope") is None


def test_transition_persists_state_history_and_event(store: Store):
    t = Ticket("T-2")
    store.save_ticket(t)
    store.transition(t, State.REFINING, actor="engine", reason="start")
    got = store.get_ticket("T-2")
    assert got.state is State.REFINING
    assert got.history == [
        {"from": "new", "to": "refining", "actor": "engine", "reason": "start"}
    ]
    assert store.events("T-2", "transition")[0]["to"] == "refining"


def test_illegal_transition_writes_nothing(store: Store):
    t = Ticket("T-3")
    store.save_ticket(t)
    with pytest.raises(TransitionError):
        store.transition(t, State.MERGED, actor="agent")
    assert store.get_ticket("T-3").state is State.NEW
    assert store.events("T-3") == []


def test_cap_hit_writes_nothing(store: Store):
    limits = Limits(max_review_rounds=1)
    t = Ticket("T-4", state=State.IN_REVIEW, review_round=1)
    store.save_ticket(t)
    with pytest.raises(LimitExceeded):
        store.transition(t, State.CHANGES_REQUESTED, actor="reviewer", limits=limits)
    assert store.get_ticket("T-4").review_round == 1 and t.review_round == 1


def test_failed_write_rolls_back_memory(store: Store, monkeypatch: pytest.MonkeyPatch):
    t = Ticket("T-5")
    store.save_ticket(t)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(Store, "_write_ticket", staticmethod(boom))
    with pytest.raises(RuntimeError):
        store.transition(t, State.REFINING, actor="engine")
    assert t.state is State.NEW and t.history == []
    assert store.get_ticket("T-5").state is State.NEW


def test_resume_from_disk(tmp_path: Path):
    path = state_db_path(tmp_path)
    with Store(path) as s:
        t = Ticket("T-6")
        s.save_ticket(t)
        s.transition(t, State.REFINING, actor="engine")
    with Store(path) as s:
        assert s.get_ticket("T-6").state is State.REFINING


def test_list_by_state_and_stage_results(store: Store):
    a, b = Ticket("A"), Ticket("B", state=State.IMPLEMENTING)
    store.save_ticket(a)
    store.save_ticket(b)
    assert [t.id for t in store.list_tickets(State.IMPLEMENTING)] == ["B"]
    assert {t.id for t in store.list_tickets()} == {"A", "B"}
    store.add_stage_result("B", "gates", 1, False, {"failed": ["unit"]})
    store.add_stage_result("B", "gates", 2, True, {})
    results = store.stage_results("B", "gates")
    assert [r["ok"] for r in results] == [False, True]
    assert results[0]["payload"] == {"failed": ["unit"]}


def test_event_payload_cannot_overwrite_the_event_kind(store: Store):
    store.add_event("T-9", "command", {"kind": "approve", "ts": "x", "comment_id": 7})
    event = store.events("T-9", "command")[0]
    assert event["kind"] == "command" and isinstance(event["ts"], float)
    assert event["comment_id"] == 7


def test_usage_cooldown_and_baseline_roundtrip(store: Store):
    store.record_usage("claude", "coder", "T", cost_usd=1.5, tokens_in=10, tokens_out=5, ts=100.0)
    store.record_usage("claude", "coder", "T", cost_usd=0.5, ts=50.0)
    assert store.usage_since("claude", 80.0) == {
        "cost_usd": 1.5, "tokens_in": 10, "tokens_out": 5, "runs": 1}
    assert store.get_cooldown("claude") is None
    store.set_cooldown("claude", 500.0, "limit", 2)
    assert store.get_cooldown("claude")["strikes"] == 2
    store.clear_cooldown("claude")
    assert store.get_cooldown("claude") is None
    assert store.get_baseline("k") is None
    store.set_baseline("k", {"a": 1})
    store.set_baseline("k", {"a": 2})
    assert store.get_baseline("k") == {"a": 2}
