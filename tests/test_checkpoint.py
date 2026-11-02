from devflow import checkpoint
from devflow.agents import FakeAgent
from devflow.board import FakeBoard
from devflow.runner import QueueRunner, RunLimits
from devflow.states import State
from tests.test_engine import APPROVE, Env


def test_three_merges_pause_until_acknowledged(tmp_path):
    assert not checkpoint.read(tmp_path)["pending"]
    assert checkpoint.merged(tmp_path, 1, "4", "First") is None
    assert checkpoint.merged(tmp_path, 1, "4", "First") is None
    assert checkpoint.merged(tmp_path, 2, "5", "Second") is None
    reached = checkpoint.merged(tmp_path, 3, "6", "Third")
    assert reached is not None and reached["pending"]
    assert [item["issue"] for item in reached["completed"]] == [1, 2, 3]
    assert checkpoint.merged(tmp_path, 4, "7", "Fourth") is None
    assert checkpoint.acknowledge(tmp_path)
    assert checkpoint.read(tmp_path) == {"pending": False, "completed": []}
    assert not checkpoint.acknowledge(tmp_path)


def test_pending_checkpoint_blocks_queue_even_after_restart(tmp_path):
    env = Env(tmp_path / "case")
    try:
        board = FakeBoard()
        board.put(1, "Ready")
        checkpoint.write(env.repo, {"pending": True, "completed": []})
        report = QueueRunner(env.engine(board=board), RunLimits()).run()
        assert "checkpoint" in report.reason
        assert env.store.list_tickets() == []
    finally:
        env.store.close()


def test_failed_checkpoint_delivery_retries_on_next_run(tmp_path):
    env = Env(tmp_path / "case")
    try:
        checkpoint.write(env.repo, {"pending": True, "completed": [],
                                    "message": "Three tickets done", "notified": False})
        engine = env.engine()

        class FlakyNotifier:
            calls = 0

            def send(self, text):
                assert text == "Three tickets done"
                self.calls += 1
                return self.calls > 1

        engine.notifier = FlakyNotifier()
        runner = QueueRunner(engine, RunLimits())
        runner.notify_pending()
        assert not checkpoint.read(env.repo)["notified"]
        runner.notify_pending()
        assert checkpoint.read(env.repo)["notified"]
        runner.notify_pending()
        assert engine.notifier.calls == 2
    finally:
        env.store.close()


def test_openclaw_reviewer_can_approve_autonomous_merge(tmp_path):
    env = Env(tmp_path / "case", plan_gate=False, merge_gate=False)
    try:
        engine = env.engine()
        engine.agents["reviewer"] = FakeAgent({"reviewer": [
            {"decision": "approve", "reason": "Scope and tests are concrete."}, APPROVE,
        ]},
                                              name="openclaw:devflow")
        result = engine.run(1)
        assert result.ticket.state is State.MERGED
        assert env.store.stage_results("#1", "plan_review")[-1]["payload"]["decision"] == "approve"
        review = env.store.stage_results("#1", "review")[-1]["payload"]
        assert review["agent"] == "openclaw:devflow"
        assert review["approved"] and review["engine_gates_ok"]
    finally:
        env.store.close()
