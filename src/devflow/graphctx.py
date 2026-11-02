"""Keep the code graph fresh and turn it into context for the orchestrator and sub-agents.

Freshness is fingerprint-based, not watcher-based: the fingerprint is the HEAD commit plus a
hash of every uncommitted and untracked change. Before an agent starts, and after each round of
edits, `ensure_fresh` rebuilds the graph only if the fingerprint moved. An incremental
`graphify update` takes about half a second and uses no LLM, so this is cheap enough to run at
every stage boundary. A ticket worktree has no graph of its own (graphify-out/ is git-excluded),
so the first call there builds it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import telemetry
from .graphify import GraphResult, build_graph

GRAPH_DIR = "graphify-out"
FINGERPRINT_FILE = ".devflow_fingerprint"
DEFAULT_BUDGET_CHARS = 6000
OWN_OUTPUT = (f"{GRAPH_DIR}/", ".devflow/state/")


def graph_path(root: Path) -> Path:
    return root / GRAPH_DIR / "graph.json"


def _git(root: Path, *args: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60, check=False
    )
    return proc.stdout if proc.returncode == 0 else None


def fingerprint(root: Path) -> str:
    """Identify the exact state of the working tree. Empty string if not a git repo."""
    head = _git(root, "rev-parse", "HEAD")
    if head is None:
        return ""
    diff = _git(root, "diff", "HEAD") or ""
    untracked = _git(root, "ls-files", "--others", "--exclude-standard") or ""
    parts = [head.strip(), diff]
    for rel in sorted(untracked.splitlines()):
        if rel.startswith(OWN_OUTPUT):
            continue  # what we generate must never make the tree look changed
        try:
            parts.append(rel + ":" + hashlib.sha256((root / rel).read_bytes()).hexdigest())
        except OSError:
            parts.append(rel)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _stored(root: Path) -> str:
    try:
        return (root / GRAPH_DIR / FINGERPRINT_FILE).read_text().strip()
    except OSError:
        return ""


def is_fresh(root: Path) -> bool:
    current = fingerprint(root)
    return bool(current) and graph_path(root).exists() and current == _stored(root)


@dataclass
class GraphStatus:
    refreshed: bool
    ok: bool
    nodes: int = 0
    edges: int = 0
    message: str = ""
    fingerprint: str = ""
    state: str = "unavailable"  # fresh | refreshed | failed | unavailable


def _report(status: GraphStatus) -> GraphStatus:
    """Annotate the graph.refresh span. Never includes the free-text message."""
    known = status.state in ("fresh", "refreshed", "failed")
    counts = {"nodes": status.nodes, "edges": status.edges} if known else {}
    telemetry.annotate(refreshed=status.state == "refreshed", status=status.state,
                       fingerprint=status.fingerprint, **counts)
    return status


@telemetry.operation("graph.refresh")
def ensure_fresh(
    root: Path, *, force: bool = False,
    builder: Callable[[Path], GraphResult] = build_graph,
) -> GraphStatus:
    """Rebuild the graph if (and only if) the tree changed since it was last built."""
    current = fingerprint(root)
    if not force and bool(current) and graph_path(root).exists() and current == _stored(root):
        nodes, edges = _counts(root)
        return _report(GraphStatus(False, True, nodes, edges, "up to date", current, "fresh"))
    result = builder(root)
    if not result.ok:
        state = "failed" if result.ran and current else "unavailable"
        return _report(GraphStatus(result.ran, False, message=result.message or "build failed",
                                   fingerprint=current, state=state))
    # Fingerprint again: the build itself must not have changed the tree it describes.
    (root / GRAPH_DIR).mkdir(exist_ok=True)
    (root / GRAPH_DIR / FINGERPRINT_FILE).write_text(current or fingerprint(root))
    return _report(GraphStatus(True, True, result.nodes, result.edges, "rebuilt", current,
                               "refreshed" if current else "unavailable"))


def _counts(root: Path) -> tuple[int, int]:
    try:
        g = json.loads(graph_path(root).read_text())
    except (OSError, json.JSONDecodeError):
        return 0, 0
    return len(g.get("nodes", [])), len(g.get("links", g.get("edges", [])))


def load_graph(root: Path) -> dict | None:
    try:
        return json.loads(graph_path(root).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _is_real(node: dict) -> bool:
    return bool(node.get("source_file")) and not str(node.get("label", "")).startswith("_")


def digest(
    root: Path, files: list[str] | None = None, *, budget_chars: int = DEFAULT_BUDGET_CHARS
) -> str:
    """A compact, deterministic summary of the graph for an agent's prompt."""
    g = load_graph(root)
    if g is None:
        return "Code graph: unavailable. Fall back to reading files directly."
    nodes = {n["id"]: n for n in g.get("nodes", [])}
    links = g.get("links", g.get("edges", []))

    degree: Counter[str] = Counter()
    adjacency: dict[str, set[str]] = defaultdict(set)
    relations: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for e in links:
        s, t = e.get("source"), e.get("target")
        if s in nodes and t in nodes and s != t:
            degree[s] += 1
            degree[t] += 1
            adjacency[s].add(t)
            adjacency[t].add(s)
            relations[(s, t)][str(e.get("relation", "related"))] += 1

    out = [f"# Code graph ({len(nodes)} nodes, {len(links)} edges)", ""]
    out.append(
        "Query it instead of grepping: `graphify query \"<question>\"`, "
        "`graphify path \"A\" \"B\"`, `graphify explain \"X\"` (or the graphify MCP tools)."
    )

    communities = Counter(
        str(n.get("community_name") or n.get("community")) for n in nodes.values() if _is_real(n)
    )
    if communities:
        out += ["", "## Main areas (community: nodes)"]
        out += [f"- {name}: {count}" for name, count in communities.most_common(12)]

    gods = [nid for nid, _ in degree.most_common(40) if _is_real(nodes[nid])][:10]
    if gods:
        out += ["", "## Most connected symbols (change these carefully)"]
        out += [f"- `{nodes[n]['label']}` in {nodes[n]['source_file']} ({degree[n]} links)"
                for n in gods]

    wanted = set(files or [])
    if wanted:
        seeds = {nid for nid, n in nodes.items() if n.get("source_file") in wanted}
        touching: dict[str, Counter[str]] = defaultdict(Counter)
        for nid in seeds:
            for other in adjacency[nid]:
                f = nodes[other].get("source_file")
                if f and f not in wanted:
                    key = (nid, other) if (nid, other) in relations else (other, nid)
                    touching[f].update(relations.get(key, Counter(related=1)))
        out += ["", f"## Blast radius of {', '.join(sorted(wanted))}"]
        if not seeds:
            out.append("- none of these files are in the graph yet (new files?)")
        elif not touching:
            out.append("- no other file is linked to these files")
        else:
            ranked = sorted(touching.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
            for f, rel in ranked[:15]:
                kinds = ", ".join(f"{k} x{v}" for k, v in rel.most_common(3))
                out.append(f"- {f}: {kinds}")
            if len(ranked) > 15:
                out.append(f"- ... and {len(ranked) - 15} more files")

    text = "\n".join(out) + "\n"
    if len(text) > budget_chars:
        text = text[: budget_chars - 40].rsplit("\n", 1)[0] + "\n... (truncated to budget)\n"
    return text


def write_context(root: Path, files: list[str] | None = None, *, out: Path | None = None) -> Path:
    """Write the digest where agents can read it. Per-run output, so it is not tracked."""
    path = out or root / ".devflow" / "state" / "context" / "graph.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(digest(root, files))
    return path


def mcp_config(root: Path) -> dict | None:
    """Claude `--mcp-config` payload that serves this checkout's graph. None if unavailable."""
    binary = shutil.which("graphify-mcp")
    if not binary or not graph_path(root).exists():
        return None
    return {"mcpServers": {"graphify": {"command": binary, "args": [str(graph_path(root))]}}}


def codex_overrides(root: Path) -> list[str]:
    """`-c key=value` pairs that give a codex run the same graphify MCP server."""
    binary = shutil.which("graphify-mcp")
    if not binary or not graph_path(root).exists():
        return []
    return [
        f"mcp_servers.graphify.command={json.dumps(binary)}",
        f"mcp_servers.graphify.args={json.dumps([str(graph_path(root))])}",
    ]


def agent_preamble(root: Path, files: list[str] | None = None) -> str:
    """Text prepended to every agent prompt: the graph digest plus how to use the tools."""
    return (
        "## Repository context (code graph)\n"
        "A knowledge graph of this repo is available. Use it to find where things live and "
        "what a change touches before reading files or searching. It was refreshed for the "
        "current state of this checkout.\n\n" + digest(root, files)
    )
