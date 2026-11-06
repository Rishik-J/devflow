import json
from pathlib import Path

import pytest

from devflow.agents import AgentError, AgentRequest, AgentResult, CursorAgent, FakeAgent
from devflow.agents.base import ProcResult
from devflow.loadconfig import AgentBudget, RoutingCfg
from devflow.router import (
    NoAgentAvailable,
    Router,
    build_router,
    is_limit_error,
    run_routed,
)
from devflow.store import Store, state_db_path

NOW = 1_000_000.0


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_router(tmp_path: Path, agents: dict, *, budgets=None, strategy="headroom",
                ready=None, clock=None, **cfg_kw) -> tuple[Router, Store, Clock]:
    store = Store(state_db_path(tmp_path))
    clock = clock or Clock()
    cfg = RoutingCfg(strategy=strategy, agents=budgets or {}, **cfg_kw)
    router = Router(store, cfg, agents, now=clock, ready=ready or (lambda a: (True, "")))
    return router, store, clock


def req(tmp_path: Path, role: str = "coder", **kw) -> AgentRequest:
    return AgentRequest(role=role, prompt="work", cwd=tmp_path, **kw)


def ok(text: str = "done", cost: float | None = 1.0, **raw) -> AgentResult:
    return AgentResult(text=text, session="s", cost_usd=cost, raw=raw)


def three(**scripts) -> dict[str, FakeAgent]:
    return {n: FakeAgent(scripts.get(n, {}), name=n) for n in ("claude", "codex", "cursor")}


# ---------------------------------------------------------------- limit detection


@pytest.mark.parametrize("text", [
    "Rate limit exceeded", "You've hit your usage limit", "429 Too Many Requests",
    "monthly quota reached", "out of credits", "API is overloaded", "Insufficient credit",
])
def test_limit_errors_detected(text):
    assert is_limit_error(text)


@pytest.mark.parametrize("text", ["", "SyntaxError in foo.py", "tests failed", "timed out"])
def test_ordinary_errors_not_limits(text):
    assert not is_limit_error(text)


# ---------------------------------------------------------------- picking


def test_headroom_prefers_the_agent_with_most_budget_left(tmp_path: Path):
    budgets = {n: AgentBudget(limit_usd=10, priority=i) for i, n in enumerate(
        ("claude", "codex", "cursor"), start=1)}
    router, store, _ = make_router(tmp_path, three(), budgets=budgets)
    store.record_usage("claude", "coder", "T", cost_usd=8, ts=NOW - 60)
    store.record_usage("codex", "coder", "T", cost_usd=2, ts=NOW - 60)
    store.record_usage("cursor", "coder", "T", cost_usd=5, ts=NOW - 60)
    assert router.pick("coder").name == "codex"
    assert [round(s.headroom, 2) for s in router.statuses()] == [0.2, 0.8, 0.5]


def test_unlimited_agents_tie_on_headroom_and_priority_breaks_it(tmp_path: Path):
    budgets = {"claude": AgentBudget(priority=2), "codex": AgentBudget(priority=1),
               "cursor": AgentBudget(priority=3)}
    router, *_ = make_router(tmp_path, three(), budgets=budgets)
    assert router.pick("coder").name == "codex"


def test_priority_strategy_ignores_headroom(tmp_path: Path):
    budgets = {"claude": AgentBudget(priority=1, limit_usd=10),
               "codex": AgentBudget(priority=2, limit_usd=10)}
    router, store, _ = make_router(tmp_path, {k: v for k, v in three().items()
                                              if k != "cursor"},
                                   budgets=budgets, strategy="priority")
    store.record_usage("claude", "coder", "T", cost_usd=9, ts=NOW - 5)
    assert router.pick("coder").name == "claude"


def test_usage_outside_the_window_does_not_count(tmp_path: Path):
    budgets = {"claude": AgentBudget(limit_usd=10, window_hours=5)}
    router, store, _ = make_router(tmp_path, {"claude": FakeAgent(name="claude")}, budgets=budgets)
    store.record_usage("claude", "coder", "T", cost_usd=10, ts=NOW - 6 * 3600)
    assert router.status("claude").headroom == 1.0
    store.record_usage("claude", "coder", "T", cost_usd=10, ts=NOW - 3600)
    assert router.status("claude").headroom == 0.0


def test_budget_exhausted_agent_is_skipped_and_all_exhausted_raises(tmp_path: Path):
    budgets = {"claude": AgentBudget(limit_usd=5), "codex": AgentBudget(limit_usd=5)}
    agents = {k: v for k, v in three().items() if k != "cursor"}
    router, store, _ = make_router(tmp_path, agents, budgets=budgets)
    store.record_usage("claude", "coder", "T", cost_usd=5, ts=NOW - 1)
    assert router.pick("coder").name == "codex"
    store.record_usage("codex", "coder", "T", cost_usd=6, ts=NOW - 1)
    with pytest.raises(NoAgentAvailable, match="budget window used up"):
        router.pick("coder")


def test_not_ready_agent_is_skipped_with_reason(tmp_path: Path):
    def ready(agent):
        return (False, "run `agent login`") if agent.name == "cursor" else (True, "")

    router, *_ = make_router(tmp_path, three(), ready=ready,
                             budgets={"cursor": AgentBudget(priority=0)})
    assert router.pick("coder").name != "cursor"
    only = {"cursor": FakeAgent(name="cursor")}
    r2, *_ = make_router(tmp_path / "x", only, ready=ready) if (tmp_path / "x").mkdir() is None else (None,)
    with pytest.raises(NoAgentAvailable, match="agent login"):
        r2.pick("coder")


def test_avoid_is_a_soft_preference_for_independent_review(tmp_path: Path):
    budgets = {"claude": AgentBudget(priority=1), "codex": AgentBudget(priority=2)}
    agents = {k: v for k, v in three().items() if k != "cursor"}
    router, *_ = make_router(tmp_path, agents, budgets=budgets)
    c = router.pick("reviewer", avoid={"claude"})
    assert c.name == "codex" and c.independent
    solo, *_ = make_router(tmp_path / "s", {"claude": FakeAgent(name="claude")}) \
        if (tmp_path / "s").mkdir() is None else (None,)
    c2 = solo.pick("reviewer", avoid={"claude"})
    assert c2.name == "claude" and not c2.independent


def test_prefer_keeps_a_ticket_on_its_agent_if_eligible(tmp_path: Path):
    budgets = {"claude": AgentBudget(priority=1), "codex": AgentBudget(priority=2)}
    agents = {k: v for k, v in three().items() if k != "cursor"}
    router, *_ = make_router(tmp_path, agents, budgets=budgets)
    assert router.pick("coder", prefer="codex").name == "codex"
    router.store.set_cooldown("codex", NOW + 600, "limit", 1)
    assert router.pick("coder", prefer="codex").name == "claude"


def test_unrouted_role_is_rejected(tmp_path: Path):
    router, *_ = make_router(tmp_path, three())
    assert router.routes("coder") and not router.routes("planner")
    with pytest.raises(ValueError, match="not routed"):
        router.pick("planner")


# ---------------------------------------------------------------- cooldown + record


def test_limit_failure_puts_agent_on_doubling_cooldown_and_success_clears_it(tmp_path: Path):
    router, store, clock = make_router(tmp_path, three(), cooldown_minutes=30,
                                       cooldown_max_hours=2)
    assert router.record_failure("claude", "coder", "T", "Rate limit exceeded")
    first = store.get_cooldown("claude")
    assert first["until"] == pytest.approx(NOW + 30 * 60) and first["strikes"] == 1
    clock.t += 31 * 60
    assert router.status("claude").cooldown_until is None  # expired
    router.record_failure("claude", "coder", "T", "usage limit reached")
    assert store.get_cooldown("claude")["until"] == pytest.approx(clock.t + 60 * 60)
    router.record_failure("claude", "coder", "T", "usage limit reached")
    assert store.get_cooldown("claude")["until"] == pytest.approx(clock.t + 2 * 60 * 60)  # capped
    assert router.record("claude", "coder", "T", 100, ok()) is False
    assert store.get_cooldown("claude") is None


def test_non_limit_failure_does_not_cool_down(tmp_path: Path):
    router, store, _ = make_router(tmp_path, three())
    assert not router.record_failure("claude", "coder", "T", "tests failed")
    assert store.get_cooldown("claude") is None


def test_cost_uses_reported_cost_else_tokens_else_text_length(tmp_path: Path):
    budgets = {"codex": AgentBudget(usd_per_mtok_in=2, usd_per_mtok_out=10)}
    router, *_ = make_router(tmp_path, three(), budgets=budgets)
    assert router.estimate_cost("claude", ok(cost=0.5), 4000)[0] == 0.5
    c, tin, tout = router.estimate_cost(
        "codex", ok(cost=None, usage={"input_tokens": 1_000_000, "output_tokens": 100_000}), 0)
    assert (c, tin, tout) == (3.0, 1_000_000, 100_000)
    c2, tin2, tout2 = router.estimate_cost("codex", ok("x" * 400, cost=None), 4000)
    assert (tin2, tout2) == (1000, 100) and c2 == pytest.approx(0.002 + 0.001)


# ---------------------------------------------------------------- run_routed


def test_run_routed_records_usage_and_returns_the_agent(tmp_path: Path):
    agents = three(claude={"coder": [ok("built it", cost=0.7)]})
    router, store, _ = make_router(tmp_path, agents, budgets={"claude": AgentBudget(priority=1)})
    out = run_routed(router, req(tmp_path), "T-1")
    assert out.agent == "claude" and out.result.text == "built it" and not out.switched
    assert store.usage_since("claude", 0)["cost_usd"] == pytest.approx(0.7)


def test_falls_through_to_next_agent_on_a_limit_and_drops_the_session(tmp_path: Path):
    agents = three(
        claude={"coder": [AgentResult(text="", ok=False, error="usage limit reached")]},
        codex={"coder": [ok("codex did it")]},
    )
    budgets = {"claude": AgentBudget(priority=1), "codex": AgentBudget(priority=2)}
    router, store, _ = make_router(tmp_path, agents, budgets=budgets)
    out = run_routed(router, req(tmp_path, session="claude-session"), "T-2", prefer="claude")
    assert out.agent == "codex" and out.switched
    assert agents["codex"].calls[0].session is None  # sessions do not cross tools
    assert store.get_cooldown("claude") is not None


def test_exception_limit_also_falls_through(tmp_path: Path):
    agents = three(claude={"coder": [AgentError("429 Too Many Requests")]},
                   codex={"coder": [ok("fine")]})
    budgets = {"claude": AgentBudget(priority=1), "codex": AgentBudget(priority=2)}
    router, *_ = make_router(tmp_path, agents, budgets=budgets)
    assert run_routed(router, req(tmp_path), "T-3").agent == "codex"


def test_non_limit_errors_are_raised_not_rerouted(tmp_path: Path):
    agents = three(claude={"coder": [AgentError("unreadable output")]},
                   codex={"coder": [ok()]})
    budgets = {"claude": AgentBudget(priority=1), "codex": AgentBudget(priority=2)}
    router, *_ = make_router(tmp_path, agents, budgets=budgets)
    with pytest.raises(AgentError, match="unreadable"):
        run_routed(router, req(tmp_path), "T-4")
    assert agents["codex"].calls == []


def test_all_agents_limited_raises_with_retry_time(tmp_path: Path):
    limit = AgentResult(text="", ok=False, error="rate limit")
    agents = {"claude": FakeAgent({"coder": [limit]}, name="claude"),
              "codex": FakeAgent({"coder": [limit]}, name="codex")}
    router, *_ = make_router(tmp_path, agents)
    with pytest.raises(NoAgentAvailable) as exc:
        run_routed(router, req(tmp_path), "T-5")
    assert exc.value.retry_at == pytest.approx(NOW + 30 * 60)


def test_structured_routed_run_validates_and_records(tmp_path: Path):
    plan = {"status": "needs_clarification",
            "questions": [{"question": "q", "proposed_default": "d"}]}
    agents = three(claude={"coder": [json.dumps(plan)]})
    router, *_ = make_router(tmp_path, agents, budgets={"claude": AgentBudget(priority=1)})
    out = run_routed(router, req(tmp_path, schema="plan"), "T-6", structured=True)
    assert out.result.data == plan


def test_reviewer_avoids_the_coders_agent_when_it_can(tmp_path: Path):
    agents = three(codex={"reviewer": [ok("lgtm")]})
    budgets = {"claude": AgentBudget(priority=1), "codex": AgentBudget(priority=2)}
    router, *_ = make_router(tmp_path, agents, budgets=budgets)
    out = run_routed(router, req(tmp_path, role="reviewer"), "T-7", avoid={"claude"})
    assert out.agent == "codex" and out.independent


# ---------------------------------------------------------------- cursor driver


CURSOR_OK = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                        "result": "patched", "session_id": "chat-7", "duration_ms": 2500})


class Recorder:
    def __init__(self, result: ProcResult):
        self.result, self.calls = result, []

    def __call__(self, cmd, cwd, env, timeout_s, stdin_text):
        self.calls.append({"cmd": cmd, "cwd": cwd, "env": env})
        return self.result


def test_cursor_read_only_command(tmp_path: Path):
    r = Recorder(ProcResult(0, CURSOR_OK, ""))
    out = CursorAgent("agent", runner=r).run(req(tmp_path, role="reviewer", system="Be strict.",
                                                 model="sonnet-4"))
    cmd = r.calls[0]["cmd"]
    assert cmd[:5] == ["agent", "-p", "--trust", "--output-format", "json"]
    assert cmd[cmd.index("--mode") + 1] == "ask" and "--force" not in cmd
    assert cmd[cmd.index("--model") + 1] == "sonnet-4"
    assert cmd[-1].startswith("Be strict.") and cmd[-1].endswith("work")
    assert out.ok and out.text == "patched" and out.session == "chat-7"
    assert out.duration_s == pytest.approx(2.5)


def test_cursor_write_command_is_forced_but_sandboxed_and_resumes(tmp_path: Path):
    r = Recorder(ProcResult(0, CURSOR_OK, ""))
    CursorAgent("agent", runner=r).run(req(tmp_path, read_only=False, session="chat-7"))
    cmd = r.calls[0]["cmd"]
    assert "--force" in cmd and cmd[cmd.index("--sandbox") + 1] == "enabled"
    assert "--mode" not in cmd and cmd[cmd.index("--resume") + 1] == "chat-7"


def test_cursor_not_logged_in_gives_a_clear_error(tmp_path: Path):
    r = Recorder(ProcResult(1, "", "Error: Not logged in"))
    with pytest.raises(AgentError, match="agent login"):
        CursorAgent("agent", runner=r).run(req(tmp_path))


def test_cursor_error_result_and_garbage(tmp_path: Path):
    bad = json.dumps({"subtype": "error", "is_error": True, "result": "usage limit reached"})
    out = CursorAgent("agent", runner=Recorder(ProcResult(1, bad, ""))).run(req(tmp_path))
    assert not out.ok and is_limit_error(out.error)
    with pytest.raises(AgentError, match="unreadable"):
        CursorAgent("agent", runner=Recorder(ProcResult(0, "hello", ""))).run(req(tmp_path))


def test_cursor_env_only_keeps_cursor_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "c")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    r = Recorder(ProcResult(0, CURSOR_OK, ""))
    CursorAgent("agent", runner=r).run(req(tmp_path))
    env = r.calls[0]["env"]
    assert env["CURSOR_API_KEY"] == "c" and "ANTHROPIC_API_KEY" not in env


def test_build_router_uses_configured_agents(tmp_path: Path):
    from devflow.graphify import GraphResult
    from devflow.init import run_init
    from devflow.loadconfig import load_config
    from tests.conftest import git_init

    repo = git_init(tmp_path / "r")
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths=['tests']\n")
    run_init(repo, graph_builder=lambda r: GraphResult(False, False, message="x"),
             which={"claude": "/c", "codex": "/x", "agent": "/a"}.get)
    cfg = load_config(repo)
    assert set(cfg.routing.agents) == {"claude", "codex", "cursor"}
    assert cfg.routing.agents["claude"].priority == 1 and cfg.agents["reviewer"] == "codex"
    with Store(state_db_path(repo)) as store:
        router = build_router(cfg, store)
        assert list(router.agents) == ["claude", "codex", "cursor"]


def test_cursor_usage_is_normalised_for_the_router(tmp_path: Path):
    body = json.dumps({"subtype": "success", "is_error": False, "result": "x", "session_id": "c",
                       "usage": {"inputTokens": 8222, "outputTokens": 94, "cacheReadTokens": 1}})
    out = CursorAgent("agent", runner=Recorder(ProcResult(0, body, ""))).run(req(tmp_path))
    assert out.raw["usage"] == {"input_tokens": 8222, "output_tokens": 94}
    router, *_ = make_router(tmp_path, {"cursor": FakeAgent(name="cursor")},
                             budgets={"cursor": AgentBudget(usd_per_mtok_in=3, usd_per_mtok_out=15)})
    cost, tin, tout = router.estimate_cost("cursor", out, 0)
    assert (tin, tout) == (8222, 94) and cost == pytest.approx(8222 / 1e6 * 3 + 94 / 1e6 * 15)


def test_the_real_five_hour_limit_message_is_a_limit_error():
    # Seen live on #66: this was not recognised, so Claude was never put on cooldown.
    from devflow.router import is_limit_error
    assert is_limit_error("claude coder failed: success: You've hit your 5-hour limit · resets 6:30pm")


# ---------------------------------------------------------------- capabilities


def caps_router(tmp_path, **budgets):
    agents = {"claude": FakeAgent({"coder": ["ok"]}, name="claude"),
              "codex": FakeAgent({"coder": ["ok"]}, name="codex")}
    return make_router(tmp_path, agents, budgets=budgets)[0]


def test_default_capabilities_codex_and_cursor_are_restricted(tmp_path):
    router = caps_router(tmp_path)
    assert router.capabilities("claude") == {"sockets", "browser", "network"}
    assert router.capabilities("codex") == frozenset()
    assert router.capabilities("cursor") == frozenset()
    assert router.capabilities("someone-new") >= {"sockets"}  # unknown agents are not restricted


def test_configured_capabilities_override_the_defaults(tmp_path):
    router = caps_router(tmp_path, codex=AgentBudget(capabilities=("sockets",)))
    assert router.capabilities("codex") == {"sockets"}


def test_pick_with_a_requirement_skips_agents_that_lack_it(tmp_path):
    router = caps_router(tmp_path, codex=AgentBudget(priority=1), claude=AgentBudget(priority=5))
    assert router.pick("coder").name == "codex"  # no requirement: priority/headroom decides
    assert router.pick("coder", require={"sockets"}).name == "claude"
    assert router.capable_agents({"sockets"}) == ["claude"]


def test_a_requirement_beats_the_session_preference(tmp_path):
    router = caps_router(tmp_path)
    assert router.pick("coder", prefer="codex", require={"browser"}).name == "claude"


def test_no_capable_agent_is_a_distinct_error_not_a_pause(tmp_path):
    from devflow.router import CapabilityUnavailable
    router = caps_router(tmp_path, claude=AgentBudget(capabilities=()))
    with pytest.raises(CapabilityUnavailable, match="sockets"):
        router.pick("coder", require={"sockets"})


def test_capable_agent_on_cooldown_pauses_instead_of_falling_back_to_a_restricted_one(tmp_path):
    router, store, clock = make_router(
        tmp_path, {"claude": FakeAgent({"coder": ["ok"]}, name="claude"),
                   "codex": FakeAgent({"coder": ["ok"]}, name="codex")})
    store.set_cooldown("claude", clock.t + 600, "usage limit", 1)
    with pytest.raises(NoAgentAvailable):
        router.pick("coder", require={"sockets"})
    assert router.pick("coder").name == "codex"  # unrestricted work still proceeds


def test_run_routed_carries_the_requirement(tmp_path):
    router = caps_router(tmp_path, codex=AgentBudget(priority=1))
    out = run_routed(router, req(tmp_path, requires=("browser",)), "#1")
    assert out.agent == "claude"


def test_a_capable_agent_that_was_tried_and_limited_means_wait_not_a_capability_error(tmp_path):
    # Found by the sandbox: run_routed excludes an agent that hit its limit, and the router used to
    # then conclude that nobody had the capability and escalate instead of pausing.
    from devflow.router import CapabilityUnavailable
    router = caps_router(tmp_path)
    with pytest.raises(NoAgentAvailable):
        router.pick("coder", require={"sockets"}, exclude=["claude"])
    router = caps_router(tmp_path, claude=AgentBudget(capabilities=()))
    with pytest.raises(CapabilityUnavailable):  # nobody can ever do it: still a config problem
        router.pick("coder", require={"sockets"}, exclude=["claude"])


def test_run_routed_pauses_when_the_only_capable_agent_hits_its_limit(tmp_path):
    agents = {"claude": FakeAgent({"coder": [AgentResult(text="", ok=False, error="usage limit reached")]},
                                  name="claude"),
              "codex": FakeAgent({"coder": ["would succeed but lacks sockets"]}, name="codex")}
    router, _, _ = make_router(tmp_path, agents)
    with pytest.raises(NoAgentAvailable):
        run_routed(router, req(tmp_path, requires=("sockets",)), "#1")
    assert agents["codex"].calls == []
