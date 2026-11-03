import json
import shutil
import socket
import subprocess
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


def test_raising_builder_reports_unavailable_on_span(repo: Path, trace_log: Path):
    def broken(root: Path):
        raise RuntimeError("secret-token-xyz")
    st = graphctx.ensure_fresh(repo, builder=broken)
    (attrs,) = refresh_spans(trace_log)
    assert st.state == "unavailable" and not st.ok
    assert attrs["devflow.graph.status"] == "unavailable"
    assert attrs["devflow.graph.refreshed"] is False
    assert len(attrs["devflow.graph.fingerprint"]) == 12
    assert "devflow.graph.nodes" not in attrs
    assert "secret-token-xyz" not in trace_log.read_text() and "error.type" not in attrs


def test_git_timeout_reports_unavailable_on_span(repo: Path, trace_log: Path, monkeypatch):
    import subprocess

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("git", 60)
    monkeypatch.setattr(graphctx, "fingerprint", timeout)
    st = graphctx.ensure_fresh(repo, builder=Builder())
    (attrs,) = refresh_spans(trace_log)
    assert st.state == "unavailable" and not st.ok
    assert attrs["devflow.graph.status"] == "unavailable"
    assert attrs["devflow.graph.refreshed"] is False
    assert attrs["devflow.graph.fingerprint"] == ""
    assert "devflow.graph.nodes" not in attrs and "error.type" not in attrs


def test_fingerprint_write_failure_reports_failed_on_span(repo: Path, trace_log: Path, monkeypatch):
    real = Path.write_text

    def deny(self, *a, **k):
        if self.name == graphctx.FINGERPRINT_FILE:
            raise PermissionError("secret-path")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "write_text", deny)
    st = graphctx.ensure_fresh(repo, builder=Builder())
    (attrs,) = refresh_spans(trace_log)
    assert st.state == "failed" and not st.ok
    assert attrs["devflow.graph.status"] == "failed"
    assert attrs["devflow.graph.refreshed"] is False
    assert attrs["devflow.graph.nodes"] == 0 and len(attrs["devflow.graph.fingerprint"]) == 12
    assert "secret-path" not in trace_log.read_text() and "error.type" not in attrs


# ---- graph_freshness (read-only staleness check) -------------------------------------------

def _snapshot(root: Path):
    out = root / "graphify-out"
    files = {str(p.relative_to(out)): (p.read_bytes(), p.stat().st_mtime_ns)
             for p in sorted(out.rglob("*")) if p.is_file()} if out.exists() else {}
    return files, git(root, "status", "--porcelain"), git(root, "rev-parse", "HEAD")


def test_freshness_unbuilt_fresh_and_stale_states(repo: Path):
    assert graphctx.graph_freshness(repo) == "unbuilt"
    b = Builder()
    graphctx.ensure_fresh(repo, builder=b)
    assert graphctx.graph_freshness(repo) == "fresh"
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    assert graphctx.graph_freshness(repo) == "stale"
    commit_all(repo, "next")
    assert graphctx.graph_freshness(repo) == "stale"
    git(repo, "reset", "-q", "--hard", "HEAD")
    graphctx.ensure_fresh(repo, builder=b, force=True)
    (repo / "src" / "new.py").write_text("x = 1\n")
    assert graphctx.graph_freshness(repo) == "stale"


def test_freshness_stale_when_fingerprint_missing_or_different(repo: Path):
    graphctx.ensure_fresh(repo, builder=Builder())
    fp = repo / "graphify-out" / graphctx.FINGERPRINT_FILE
    fp.write_text("deadbeef")
    assert graphctx.graph_freshness(repo) == "stale"
    fp.unlink()
    assert graphctx.graph_freshness(repo) == "stale"


def test_freshness_is_read_only_and_never_builds(repo: Path):
    b = Builder()
    before = _snapshot(repo)
    assert graphctx.graph_freshness(repo) == "unbuilt"
    assert _snapshot(repo) == before
    graphctx.ensure_fresh(repo, builder=b)
    before = _snapshot(repo)
    assert graphctx.graph_freshness(repo) == "fresh"
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    before = _snapshot(repo)
    assert graphctx.graph_freshness(repo) == "stale"
    assert _snapshot(repo) == before and b.calls == 1


def test_freshness_never_raises(tmp_path: Path, repo: Path, monkeypatch):
    assert graphctx.graph_freshness(tmp_path) == "unbuilt"
    write_graph(tmp_path, [node("a", "A", "x.py")], [])
    assert graphctx.graph_freshness(tmp_path) == "stale"
    graphctx.ensure_fresh(repo, builder=Builder())

    def boom(root):
        raise RuntimeError("git exploded")
    monkeypatch.setattr(graphctx, "fingerprint", boom)
    assert graphctx.graph_freshness(repo) == "stale"
    assert graphctx.graph_freshness(tmp_path / "missing") == "unbuilt"


def test_graph_age_comes_from_mtime_and_does_not_decide_freshness(repo: Path):
    import os
    import time
    assert graphctx.graph_age_seconds(repo) is None
    graphctx.ensure_fresh(repo, builder=Builder())
    old = time.time() - 5000
    os.utime(graphctx.graph_path(repo), (old, old))
    age = graphctx.graph_age_seconds(repo)
    assert isinstance(age, int) and 4990 <= age <= 5100
    assert graphctx.graph_freshness(repo) == "fresh"


# ---- impact engine -------------------------------------------------------------------------

def _n(nid, label, file, line=1, **kw):
    return {**node(nid, label, file), "source_location": f"L{line}", "file_type": "code", **kw}


def _e(src, tgt, rel="calls", file=None):
    return {"source": src, "target": tgt, "relation": rel, "source_file": file or src.split(":")[0]}


def _chain_graph(repo: Path, shuffle: bool = False):
    nodes = [
        _n("m.py", "m.py", "m.py"), _n("m.py:f", "f()", "m.py", 3), _n("m.py:_p", "_p()", "m.py", 9),
        _n("a.py:g", "g()", "a.py", 2), _n("b.py:h", "h()", "b.py", 2),
        _n("c.py:i", "i()", "c.py", 2), _n("imp.py:x", "x()", "imp.py"),
        _n("tests/test_m.py:t", "test_f()", "tests/test_m.py", 4),
        _n("tests/test_far.py:t", "test_far()", "tests/test_far.py", 4),
        _n("mid.py:k", "k()", "mid.py"), _n("mid2.py:k", "k2()", "mid2.py"),
        _n("rat", "why", "m.py", file_type="rationale"),
    ]
    nodes[0]["file_type"] = "code"
    links = [
        _e("m.py", "m.py:f", "contains"), _e("m.py", "m.py:_p", "contains"),
        _e("a.py:g", "m.py:f"), _e("b.py:h", "a.py:g"), _e("c.py:i", "b.py:h", "uses"),
        _e("a.py:g", "m.py:_p"), _e("m.py:f", "d.py:z"),
        _e("imp.py:x", "m.py", "imports"),
        _e("tests/test_m.py:t", "m.py:f"),
        _e("tests/test_far.py:t", "mid2.py:k"),
        _e("mid2.py:k", "a.py:g"),
        _e("rat", "m.py", "rationale_for"),
    ]
    nodes.append(_n("d.py:z", "z()", "d.py"))
    nodes.append(_n("", "nofile", ""))
    if shuffle:
        nodes.reverse()
        links.reverse()
    write_graph(repo, nodes, links)


def test_symbols_defined_and_touched_sorted(tmp_path: Path):
    _chain_graph(tmp_path)
    syms = graphctx.symbols_for_files(tmp_path, ["m.py"])
    assert [(s.id, s.role, s.line) for s in syms] == [
        ("d.py:z", "touched", 1), ("m.py:f", "defined", 3), ("m.py:_p", "defined", 9)]
    assert syms.unknown_files == []


def test_symbols_paths_unknown_and_dupes(tmp_path: Path):
    _chain_graph(tmp_path)
    a = graphctx.symbols_for_files(tmp_path, [str(tmp_path / "m.py"), "./m.py", "m.py", "new.py"])
    assert a == graphctx.symbols_for_files(tmp_path, ["m.py"])
    assert a.unknown_files == ["new.py"]


def test_blast_depth_and_shortest_hop(tmp_path: Path):
    _chain_graph(tmp_path)
    r1 = graphctx.blast_radius(tmp_path, ["m.py"], depth=1)
    assert {c.id for c in r1.callers[1]} == {"a.py:g", "tests/test_m.py:t"}
    r2 = graphctx.blast_radius(tmp_path, ["m.py"], depth=2)
    assert {c.id for c in r2.callers[2]} >= {"b.py:h", "mid2.py:k"}
    assert all(c.hop == 2 for c in r2.callers[2])
    with pytest.raises(ValueError):
        graphctx.blast_radius(tmp_path, ["m.py"], depth=0)
    with pytest.raises(ValueError):
        graphctx.blast_radius(tmp_path, ["m.py"], depth=-1)
    big = graphctx.blast_radius(tmp_path, ["m.py"], depth=9)
    ids = [c.id for cs in big.callers.values() for c in cs]
    assert len(ids) == len(set(ids)) and "m.py:f" not in ids


@pytest.mark.parametrize("rel", sorted(graphctx.CALLER_RELATIONS))
def test_each_caller_relation_traversed(tmp_path: Path, rel: str):
    write_graph(tmp_path, [_n("a:x", "X", "a.py"), _n("b:y", "Y", "b.py")], [_e("b:y", "a:x", rel)])
    (c,) = graphctx.blast_radius(tmp_path, ["a.py"], 1).callers[1]
    assert c.relation == rel and c.file == "b.py"


def test_unrelated_relation_not_a_caller(tmp_path: Path):
    write_graph(tmp_path, [_n("a:x", "X", "a.py"), _n("b:y", "Y", "b.py")],
                [_e("b:y", "a:x", "rationale_for")])
    assert graphctx.blast_radius(tmp_path, ["a.py"]).callers == {1: [], 2: []}


def test_cycle_terminates(tmp_path: Path):
    write_graph(tmp_path, [_n("a:a", "A", "a.py"), _n("b:b", "B", "b.py"), _n("c:c", "C", "c.py")],
                [_e("a:a", "b:b"), _e("b:b", "c:c"), _e("c:c", "a:a"), _e("a:a", "a:a")])
    r = graphctx.blast_radius(tmp_path, ["a.py"], depth=5)
    assert [(c.id, c.hop) for cs in r.callers.values() for c in cs] == [
        ("c:c", 1), ("b:b", 2)]


def test_dependent_modules(tmp_path: Path):
    _chain_graph(tmp_path)
    mods = graphctx.blast_radius(tmp_path, ["m.py"]).dependent_modules
    d = {m["file"]: m["hop"] for m in mods}
    assert d["imp.py"] == 1 and d["a.py"] == 1 and d["b.py"] == 2 and "m.py" not in d
    assert [m["file"] for m in mods] == sorted(d)


def test_external_contracts(tmp_path: Path):
    _chain_graph(tmp_path)
    cs = graphctx.blast_radius(tmp_path, ["m.py"]).external_contracts
    assert [(c["label"], c["consumers"]) for c in cs] == [("f()", ["a.py", "tests/test_m.py"])]
    write_graph(tmp_path, [_n("a:x", "X", "a.py"), _n("i:i", "i", "__init__.py")],
                [_e("i:i", "a:x", "re_exports")])
    assert [c["consumers"] for c in graphctx.blast_radius(tmp_path, ["a.py"]).external_contracts] \
        == [["__init__.py"]]
    write_graph(tmp_path, [_n("a:x", "X", "a.py"), _n("a:y", "Y", "a.py")], [_e("a:y", "a:x")])
    assert graphctx.blast_radius(tmp_path, ["a.py"]).external_contracts == []


def test_is_test_file():
    t = graphctx._is_test_file
    assert t("tests/x.py") and t("test/x.py") and t("a/test_x.py") and t("a/x_test.py")
    assert not t("contest.py") and not t("src/testing_utils.py")


def test_coverage_and_uncovered(tmp_path: Path):
    _chain_graph(tmp_path)
    cov = graphctx.test_coverage_map(tmp_path, ["m.py:f", "m.py:_p"])
    assert cov["m.py:f"] == ["tests/test_m.py"]
    assert cov["m.py:_p"] == []  # test_m reaches f, not _p
    far = graphctx.test_coverage_map(tmp_path, ["a.py:g"])
    assert far["a.py:g"] == ["tests/test_far.py"]  # 2 hops
    assert graphctx.test_coverage_map(tmp_path, ["a.py:g"], files=["tests/test_other.py"]) \
        ["a.py:g"] == ["tests/test_far.py", "tests/test_other.py"]
    rep = graphctx.impact(tmp_path, ["m.py"])
    assert rep.uncovered == ["m.py:_p"]
    assert "m.py:f" not in rep.uncovered


def test_three_hops_not_coverage(tmp_path: Path):
    write_graph(tmp_path, [_n("a:x", "X", "a.py"), _n("b:y", "Y", "b.py"), _n("c:z", "Z", "c.py"),
                           _n("t:t", "t", "tests/test_t.py")],
                [_e("b:y", "a:x"), _e("c:z", "b:y"), _e("t:t", "c:z")])
    assert graphctx.test_coverage_map(tmp_path, ["a:x"]) == {"a:x": []}


def test_impact_dict_markdown_deterministic(tmp_path: Path, monkeypatch):
    _chain_graph(tmp_path)
    a = graphctx.impact(tmp_path, ["m.py", "new.py"])
    assert set(a.to_dict()) == {"files", "unknown_files", "depth", "graph_state", "symbols",
                                "callers", "dependent_modules", "external_contracts",
                                "test_coverage", "uncovered"}
    assert a.graph_state == graphctx.graph_freshness(tmp_path)
    j, md = json.dumps(a.to_dict()), a.to_markdown()
    _chain_graph(tmp_path, shuffle=True)
    b = graphctx.impact(tmp_path, ["new.py", "m.py"])
    assert json.dumps(b.to_dict()) == j and b.to_markdown() == md
    for h in ("## Symbols", "## Callers", "## Dependent modules", "## External contracts",
              "## Test coverage", "## Uncovered", "new.py"):
        assert h in md

    def boom(*a, **k):
        raise AssertionError("no subprocess/network")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(socket, "socket", boom)
    graphctx.symbols_for_files(tmp_path, ["m.py"])
    graphctx.blast_radius(tmp_path, ["m.py"])
    graphctx.test_coverage_map(tmp_path, ["m.py:f"])


def test_markdown_empty(tmp_path: Path):
    write_graph(tmp_path, [_n("a:x", "X", "a.py")], [])
    md = graphctx.impact(tmp_path, ["zzz.py"]).to_markdown()
    assert "- none" in md and "zzz.py" in md


def test_impact_missing_graph_raises(tmp_path: Path):
    with pytest.raises(graphctx.GraphUnavailable):
        graphctx.impact(tmp_path, ["a.py"])


# ---- CLI ---------------------------------------------------------------------------------

def _cli(argv, repo, monkeypatch):
    from devflow.cli import main
    calls = []
    monkeypatch.setattr(graphctx, "ensure_fresh", lambda root, **k: calls.append(root))
    return main(["graph", "impact", *argv, "--path", str(repo)]), calls


def test_cli_impact_json_and_options(repo: Path, monkeypatch, capsys):
    _chain_graph(repo)
    rc, calls = _cli(["m.py"], repo, monkeypatch)
    out = capsys.readouterr().out
    d = json.loads(out)
    assert rc == 0 and calls and d["depth"] == 2 and "uncovered" in d
    assert len(d["callers"]["2"]) > 0
    rc, _ = _cli(["m.py", "a.py", "--depth", "1"], repo, monkeypatch)
    d1 = json.loads(capsys.readouterr().out)
    assert rc == 0 and d1["files"] == ["a.py", "m.py"] and d1["depth"] == 1
    rc, _ = _cli(["m.py", "--format", "markdown"], repo, monkeypatch)
    assert rc == 0 and capsys.readouterr().out.startswith("# Impact report")


def test_cli_impact_failures(repo: Path, tmp_path_factory, monkeypatch, capsys):
    rc, _ = _cli(["m.py"], repo, monkeypatch)
    cap = capsys.readouterr()
    assert rc != 0 and cap.out == "" and "error" in cap.err
    _chain_graph(repo)
    rc, _ = _cli(["m.py", "--depth", "0"], repo, monkeypatch)
    cap = capsys.readouterr()
    assert rc != 0 and cap.out == "" and "depth" in cap.err
    rc, _ = _cli(["m.py"], tmp_path_factory.mktemp("nogit"), monkeypatch)
    assert rc == 2
    from devflow.cli import main
    with pytest.raises(SystemExit) as ex:
        main(["graph", "impact"])
    assert ex.value.code != 0
    with pytest.raises(SystemExit):
        main(["graph", "impact", "m.py", "--format", "xml"])
