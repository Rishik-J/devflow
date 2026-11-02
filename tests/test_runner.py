
import pytest

from devflow import cli
from devflow.board import FakeBoard
from devflow.runner import QueueRunner, RunLimits, RunnerBusy, ownership
from devflow.states import State
from tests.test_engine import Env, good_coder


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.store.close()


def queue(env, **kwargs):
    board = FakeBoard()
    board.put(1, "Ready")
    return env.engine(board=board, **kwargs), board


def test_empty_board_finishes_without_calling_a_model(env):
    engine = env.engine(board=FakeBoard())
    report = QueueRunner(engine, RunLimits()).run()
    assert report.outcomes == [] and "sweep complete" in report.reason
    assert env.store.list_tickets() == []


def test_queue_follows_priority_and_continues_past_plan_approval(env):
    engine, board = queue(env)
    env.host.add_issue(2, "Second", "multiply")
    board.put(2, "Ready", priority="P0")
    report = QueueRunner(engine, RunLimits()).run()
    assert [o["issue"] for o in report.outcomes] == [2, 1]
    assert all(o["state"] == "awaiting_plan_approval" for o in report.outcomes)


def test_second_run_reads_human_approval_and_resumes(env):
    engine, board = queue(env)
    QueueRunner(engine, RunLimits()).run()
    env.say("alice", "/approve")
    engine = env.engine(board=board)
    report = QueueRunner(engine, RunLimits()).run()
    assert report.outcomes[0]["state"] == "awaiting_merge_approval"
    env.say("alice", "/approve", number=env.pr())
    report = QueueRunner(env.engine(board=board), RunLimits()).run()
    assert report.outcomes[0]["state"] == "merged" and board.status_of(1) == "Done"


def test_stop_before_run_does_not_claim_a_ticket(env):
    engine, _ = queue(env)
    (env.repo / ".devflow" / "STOP").touch()
    report = QueueRunner(engine, RunLimits()).run()
    assert "STOP" in report.reason and report.outcomes == []
    assert env.store.list_tickets() == []


def test_stop_during_implementation_pauses_before_gates(env):
    engine, board = queue(env)
    QueueRunner(engine, RunLimits()).run()
    env.say("alice", "/approve")

    def coder(req):
        good_coder(req)
        (env.repo / ".devflow" / "STOP").touch()
        return "done"

    report = QueueRunner(env.engine(board=board, coder=coder), RunLimits()).run()
    assert "STOP" in report.reason
    assert env.store.get_ticket("#1").state is State.GATES
    assert env.store.stage_results("#1", "gates") == []
    (env.repo / ".devflow" / "STOP").unlink()
    QueueRunner(env.engine(board=board), RunLimits()).run()
    assert env.store.get_ticket("#1").state is State.AWAITING_MERGE_APPROVAL


def test_ticket_cap_does_not_claim_the_next_ready_ticket(env):
    engine, board = queue(env)
    env.host.add_issue(2, "Second", "multiply")
    board.put(2, "Ready")
    report = QueueRunner(engine, RunLimits(max_tickets=1)).run()
    assert len(report.outcomes) == 1 and report.reason == "ticket limit reached"
    assert env.store.get_ticket("#2") is None


def test_cost_ceiling_pauses_before_another_stage(env):
    engine, board = queue(env)
    QueueRunner(engine, RunLimits()).run()
    env.say("alice", "/approve")
    engine = env.engine(board=board)
    runner = QueueRunner(engine, RunLimits(max_cost_usd=.01))
    # Simulate recorded usage arriving from an agent call during this invocation.
    env.store.record_usage("claude", "coder", "#1", cost_usd=1)
    report = runner.run()
    assert report.reason == "recorded model cost limit reached"
    assert env.store.get_ticket("#1").state is State.AWAITING_PLAN_APPROVAL


def test_time_ceiling_prevents_starting_a_ticket(env):
    engine, _ = queue(env)
    now = [0.]
    runner = QueueRunner(engine, RunLimits(max_minutes=1), now=lambda: now[0])
    now[0] = 61
    assert runner.run().reason == "run time limit reached"
    assert env.store.list_tickets() == []


def test_exclusive_ownership_releases_on_error(env):
    with ownership(env.repo), pytest.raises(RunnerBusy), ownership(env.repo):
        pass
    with pytest.raises(RuntimeError), ownership(env.repo):
        raise RuntimeError("crash")
    with ownership(env.repo):
        pass


def test_board_reconciliation_repairs_stale_status(env):
    engine, board = queue(env)
    QueueRunner(engine, RunLimits()).run()
    board.set_status(1, "Ready")
    QueueRunner(env.engine(board=board), RunLimits()).run()
    assert board.status_of(1) == "In progress"


def test_watch_stops_when_stop_file_arrives(env):
    engine, _ = queue(env)

    def sleep(seconds):
        (env.repo / ".devflow" / "STOP").touch()

    runner = QueueRunner(engine, RunLimits(), sleep=sleep)
    assert "STOP" in runner.run(watch=5).reason


def test_no_board_and_invalid_arguments_are_clear_errors(env, capsys):
    assert cli.main(["run", str(env.repo), "--max-tickets", "0"]) == 2
    assert "positive" in capsys.readouterr().err
    with pytest.raises(ValueError, match="Project"):
        QueueRunner(env.engine(), RunLimits()).run()
