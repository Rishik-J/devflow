import json
import shutil
from pathlib import Path

import pytest

from devflow import graphctx
from devflow.graphify import GraphResult, build_graph
from tests.conftest import commit_all, git, git_init


def write_graph(root: Path, nodes: list[dict], links: list[dict]) -> None:
    out = root / "graphify-out"
    out.mkdir(exist_ok=True)
    (out / "graph.json").write_text(json.dumps({"nodes": nodes, "links": links}))


def node(nid: str, label: str, file: str, community: str = "core") -> dict:
    return {"id": nid, "label": label, "source_file": file, "community_name": community}


class Builder:
    """Stands in for graphify: writes a tiny graph and counts how often it was called."""

    def __init__(self, ok: bool = True):
        self.calls = 0
        self.ok = ok

    def __call__(self, root: Path) -> GraphResult:
        self.calls += 1
        if not self.ok:
            return GraphResult(True, False, message="boom")
        write_graph(root, [node("a", "A", "src/calc.py")], [])
        return GraphResult(True, True, nodes=1, edges=0)


@pytest.fixture
def repo(base_repo: Path) -> Path:
    return base_repo


def test_first_call_builds_then_is_fresh(repo: Path):
    b = Builder()
    first = graphctx.ensure_fresh(repo, builder=b)
    assert first.refreshed and first.ok and b.calls == 1
    second = graphctx.ensure_fresh(repo, builder=b)
    assert not second.refreshed and second.ok and b.calls == 1
    assert graphctx.is_fresh(repo)


def test_uncommitted_edit_triggers_rebuild(repo: Path):
    b = Builder()
    graphctx.ensure_fresh(repo, builder=b)
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    assert not graphctx.is_fresh(repo)
    assert graphctx.ensure_fresh(repo, builder=b).refreshed and b.calls == 2


def test_new_untracked_file_triggers_rebuild_and_content_change_too(repo: Path):
    b = Builder()
    graphctx.ensure_fresh(repo, builder=b)
    (repo / "src" / "new.py").write_text("x = 1\n")
    assert graphctx.ensure_fresh(repo, builder=b).refreshed
    (repo / "src" / "new.py").write_text("x = 2\n")
    assert graphctx.ensure_fresh(repo, builder=b).refreshed and b.calls == 3


def test_commit_triggers_rebuild(repo: Path):
    b = Builder()
    graphctx.ensure_fresh(repo, builder=b)
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a - -b\n")
    commit_all(repo)
    assert graphctx.ensure_fresh(repo, builder=b).refreshed


def test_graph_output_itself_does_not_make_it_stale(repo: Path):
    # graphify-out/ is git-excluded in a real repo; the fingerprint must ignore it.
    (repo / ".git" / "info" / "exclude").write_text("graphify-out/\n")
    b = Builder()
    graphctx.ensure_fresh(repo, builder=b)
    assert graphctx.is_fresh(repo)


def test_failed_build_is_not_marked_fresh(repo: Path):
    b = Builder(ok=False)
    st = graphctx.ensure_fresh(repo, builder=b)
    assert not st.ok and "boom" in st.message
    assert not graphctx.is_fresh(repo)
    assert graphctx.ensure_fresh(repo, builder=Builder()).refreshed


def test_force_rebuilds_even_when_fresh(repo: Path):
    b = Builder()
    graphctx.ensure_fresh(repo, builder=b)
    graphctx.ensure_fresh(repo, builder=b, force=True)
    assert b.calls == 2


def test_not_a_git_repo_always_rebuilds(tmp_path: Path):
    assert graphctx.fingerprint(tmp_path) == ""
    b = Builder()
    graphctx.ensure_fresh(tmp_path, builder=b)
    graphctx.ensure_fresh(tmp_path, builder=b)
    assert b.calls == 2


def test_digest_lists_areas_gods_and_blast_radius(tmp_path: Path):
    nodes = [node("a", "Core", "src/core.py", "engine"), node("b", "Caller1", "src/a.py", "api"),
             node("c", "Caller2", "src/b.py", "api"), node("d", "Lonely", "src/z.py", "misc"),
             node("t", "_private", "src/core.py", "engine")]
    links = [{"source": "b", "target": "a", "relation": "calls"},
             {"source": "c", "target": "a", "relation": "calls"},
             {"source": "c", "target": "a", "relation": "imports_from"},
             {"source": "t", "target": "a", "relation": "contains"}]
    write_graph(tmp_path, nodes, links)
    text = graphctx.digest(tmp_path, ["src/core.py"])
    assert "5 nodes, 4 edges" in text
    assert "api: 2" in text
    assert "`Core` in src/core.py" in text and "_private" not in text.split("Most connected")[1]
    blast = text.split("Blast radius")[1]
    assert "src/b.py: calls x1, imports_from x1" in blast and "src/a.py: calls x1" in blast
    assert blast.index("src/b.py") < blast.index("src/a.py")  # most-linked file first
    assert "src/z.py" not in blast


def test_digest_edge_cases(tmp_path: Path):
    assert "unavailable" in graphctx.digest(tmp_path)
    write_graph(tmp_path, [node("a", "A", "src/a.py")], [])
    assert "are in the graph yet" in graphctx.digest(tmp_path, ["src/new.py"])
    assert "no other file is linked" in graphctx.digest(tmp_path, ["src/a.py"])


def test_digest_respects_budget(tmp_path: Path):
    nodes = [node(f"n{i}", f"Sym{i}", f"src/f{i}.py", f"area{i}") for i in range(300)]
    links = [{"source": f"n{i}", "target": "n0", "relation": "calls"} for i in range(1, 300)]
    write_graph(tmp_path, nodes, links)
    natural = graphctx.digest(tmp_path, ["src/f0.py"])
    assert len(natural) < graphctx.DEFAULT_BUDGET_CHARS  # list caps keep it bounded on its own
    assert "... and 284 more files" in natural
    text = graphctx.digest(tmp_path, ["src/f0.py"], budget_chars=500)
    assert len(text) <= 500 and "truncated" in text


def test_write_context_goes_to_untracked_state_dir(tmp_path: Path):
    write_graph(tmp_path, [node("a", "A", "src/a.py")], [])
    path = graphctx.write_context(tmp_path, ["src/a.py"])
    assert path == tmp_path / ".devflow" / "state" / "context" / "graph.md"
    assert "Blast radius" in path.read_text()


def test_mcp_config_points_at_this_checkouts_graph(tmp_path: Path, monkeypatch):
    assert graphctx.mcp_config(tmp_path) is None and graphctx.codex_overrides(tmp_path) == []
    write_graph(tmp_path, [], [])
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    cfg = graphctx.mcp_config(tmp_path)
    assert cfg == {"mcpServers": {"graphify": {
        "command": "/bin/graphify-mcp", "args": [str(tmp_path / "graphify-out" / "graph.json")]}}}
    over = graphctx.codex_overrides(tmp_path)
    assert over[0] == 'mcp_servers.graphify.command="/bin/graphify-mcp"'
    assert json.loads(over[1].split("=", 1)[1]) == [str(tmp_path / "graphify-out" / "graph.json")]


def test_agent_preamble_includes_digest_and_tool_hint(tmp_path: Path):
    write_graph(tmp_path, [node("a", "A", "src/a.py")], [])
    text = graphctx.agent_preamble(tmp_path)
    assert "graphify query" in text and "1 nodes" in text


@pytest.mark.skipif(not shutil.which("graphify"), reason="graphify not installed")
def test_real_graphify_rebuilds_after_an_edit(tmp_path: Path):
    root = git_init(tmp_path / "real")
    (root / ".git" / "info" / "exclude").write_text("graphify-out/\n")
    (root / "m.py").write_text("def helper():\n    return 1\n\n\ndef main():\n    return helper()\n")
    commit_all(root)
    first = graphctx.ensure_fresh(root, builder=build_graph)
    assert first.refreshed and first.ok and first.nodes > 0
    assert not graphctx.ensure_fresh(root, builder=build_graph).refreshed
    (root / "m.py").write_text(
        "def helper():\n    return 1\n\n\ndef extra():\n    return 2\n\n\n"
        "def main():\n    return helper() + extra()\n"
    )
    second = graphctx.ensure_fresh(root, builder=build_graph)
    assert second.refreshed and second.nodes > first.nodes
    assert "extra" in (root / "graphify-out" / "graph.json").read_text()
    git(root, "status", "--short")


# ---- graph.refresh span provenance -------------------------------------------------------

@pytest.fixture
def trace_log(tmp_path):
    from devflow import telemetry
    log = tmp_path / "graph-traces.jsonl"
    telemetry.configure(settings={"enabled": True, "log_file": str(log)})
    yield log
    telemetry.configure(settings={})


def refresh_spans(log: Path) -> list[dict]:
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    return [r["attributes"] for r in rows if r["name"] == "graph.refresh"]


def test_span_marks_noop_check_as_fresh(repo: Path, trace_log: Path):
    b = Builder()
    graphctx.ensure_fresh(repo, builder=b)
    st = graphctx.ensure_fresh(repo, builder=b)
    first, second = refresh_spans(trace_log)
    assert b.calls == 1 and st.state == "fresh"
    assert second["devflow.graph.refreshed"] is False
    assert second["devflow.graph.status"] == "fresh"
    assert second["devflow.graph.nodes"] == 1 and second["devflow.graph.edges"] == 0
    fp = second["devflow.graph.fingerprint"]
    assert len(fp) == 12 and int(fp, 16) >= 0
    assert first["devflow.graph.status"] == "refreshed"


def test_span_marks_rebuild_as_refreshed(repo: Path, trace_log: Path):
    st = graphctx.ensure_fresh(repo, builder=Builder())
    (attrs,) = refresh_spans(trace_log)
    assert st.state == "refreshed" and st.refreshed and st.ok and st.nodes == 1
    assert st.edges == 0 and st.message == "rebuilt"
    assert attrs["devflow.graph.refreshed"] is True
    assert attrs["devflow.graph.status"] == "refreshed"
    assert attrs["devflow.graph.nodes"] == 1 and isinstance(attrs["devflow.graph.edges"], int)
    assert len(attrs["devflow.graph.fingerprint"]) == 12


def test_span_marks_failed_build_without_leaking_message(repo: Path, trace_log: Path):
    st = graphctx.ensure_fresh(repo, builder=Builder(ok=False))
    (attrs,) = refresh_spans(trace_log)
    assert st.state == "failed"
    assert attrs["devflow.graph.status"] == "failed"
    assert attrs["devflow.graph.refreshed"] is False
    assert attrs["devflow.graph.nodes"] == 0 and attrs["devflow.graph.edges"] == 0
    assert len(attrs["devflow.graph.fingerprint"]) == 12
    assert "boom" not in trace_log.read_text()


def test_span_marks_missing_builder_as_unavailable_without_counts(repo: Path, trace_log: Path):
    st = graphctx.ensure_fresh(repo, builder=lambda r: GraphResult(False, False))
    (attrs,) = refresh_spans(trace_log)
    assert st.state == "unavailable"
    assert attrs["devflow.graph.status"] == "unavailable"
    assert attrs["devflow.graph.refreshed"] is False
    assert "devflow.graph.nodes" not in attrs and "devflow.graph.edges" not in attrs


def test_non_git_directory_is_unavailable_with_empty_fingerprint(tmp_path: Path, trace_log: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    st = graphctx.ensure_fresh(plain, builder=lambda r: GraphResult(False, False))
    (attrs,) = refresh_spans(trace_log)
    assert st.state == "unavailable" and st.fingerprint == ""
    assert attrs["devflow.graph.status"] == "unavailable"
    assert attrs["devflow.graph.fingerprint"] == ""
    assert "devflow.graph.nodes" not in attrs
