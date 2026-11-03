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
import posixpath
import re
import shutil
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
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


def graph_freshness(root: Path) -> str:
    """Read-only check: 'fresh', 'stale' or 'unbuilt'. Never raises, writes nothing."""
    try:
        if not graph_path(root).exists():
            return "unbuilt"
        current = fingerprint(root)
        return "fresh" if current and current == _stored(root) else "stale"
    except Exception:  # noqa: BLE001 - a check must never break a ticket
        return "stale"


def graph_age_seconds(root: Path) -> int | None:
    """Age of graph.json from its mtime, for diagnostics only (None if missing)."""
    try:
        return max(0, int(time.time() - graph_path(root).stat().st_mtime))
    except OSError:
        return None


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
    current, built = "", False
    try:
        current = fingerprint(root)
        if not force and bool(current) and graph_path(root).exists() and current == _stored(root):
            nodes, edges = _counts(root)
            return _report(GraphStatus(False, True, nodes, edges, "up to date", current, "fresh"))
        try:
            result = builder(root)
        except Exception:  # noqa: BLE001 - a broken builder is "unavailable"; text never recorded
            return _report(GraphStatus(False, False, message="graph builder error",
                                       fingerprint=current, state="unavailable"))
        if not result.ok:
            state = "failed" if result.ran and current else "unavailable"
            return _report(GraphStatus(result.ran, False, message=result.message or "build failed",
                                       fingerprint=current, state=state))
        built = True
        # Fingerprint again: the build itself must not have changed the tree it describes.
        (root / GRAPH_DIR).mkdir(exist_ok=True)
        (root / GRAPH_DIR / FINGERPRINT_FILE).write_text(current or fingerprint(root))
        return _report(GraphStatus(True, True, result.nodes, result.edges, "rebuilt", current,
                                   "refreshed" if current else "unavailable"))
    except Exception:  # noqa: BLE001 - git timeout, unreadable files, failed write; never recorded
        # The freshness check itself broke: report it on this span instead of raising.
        return _report(GraphStatus(built, False, message="graph freshness check error",
                                   fingerprint=current,
                                   state="failed" if built and current else "unavailable"))


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


# --------------------------------------------------------------------------------------------
# Impact engine: deterministic, LLM-free blast radius computed from graph.json alone.
# Edges point from the dependent to the dependency (caller -> callee), so "who is affected by a
# change" is a reverse walk.
# --------------------------------------------------------------------------------------------

CALLER_RELATIONS = frozenset(
    {"calls", "indirect_call", "uses", "references", "inherits", "method"}
)
IMPORT_RELATIONS = frozenset({"imports", "imports_from"})
CONSUMER_RELATIONS = CALLER_RELATIONS | IMPORT_RELATIONS | {"re_exports"}
TEST_HOPS = 2


class GraphUnavailable(RuntimeError):
    """graph.json is missing or unreadable."""


@dataclass(frozen=True)
class Symbol:
    id: str
    label: str
    file: str
    line: int
    role: str  # "defined" | "touched"

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "file": self.file, "line": self.line,
                "role": self.role}


@dataclass(frozen=True)
class Caller:
    id: str
    symbol: str
    file: str
    hop: int
    relation: str

    def to_dict(self) -> dict:
        return {"id": self.id, "symbol": self.symbol, "file": self.file, "hop": self.hop,
                "relation": self.relation}


class SymbolList(list):
    """A sorted list of Symbol that also carries the files the graph knows nothing about."""

    unknown_files: list[str]

    def __init__(self, items=(), unknown_files=()):
        super().__init__(items)
        self.unknown_files = list(unknown_files)


@dataclass(frozen=True)
class BlastRadius:
    files: list[str]
    unknown_files: list[str]
    depth: int
    symbols: list[Symbol]
    callers: dict[int, list[Caller]]
    dependent_modules: list[dict]
    external_contracts: list[dict]


@dataclass(frozen=True)
class ImpactReport:
    files: list[str]
    unknown_files: list[str]
    depth: int
    graph_state: str
    symbols: list[Symbol]
    callers: dict[int, list[Caller]]
    dependent_modules: list[dict]
    external_contracts: list[dict]
    test_coverage: dict[str, list[str]] = field(default_factory=dict)
    uncovered: list[str] = field(default_factory=list)

    @property
    def node_count(self) -> int:
        """Symbols + callers + dependent modules."""
        return (len(self.symbols) + sum(len(cs) for cs in self.callers.values())
                + len(self.dependent_modules))

    @property
    def edge_count(self) -> int:
        """One edge per caller entry."""
        return sum(len(cs) for cs in self.callers.values())

    def to_dict(self) -> dict:
        return {
            "files": list(self.files),
            "unknown_files": list(self.unknown_files),
            "depth": self.depth,
            "graph_state": self.graph_state,
            "symbols": [s.to_dict() for s in self.symbols],
            "callers": {str(h): [c.to_dict() for c in cs] for h, cs in sorted(self.callers.items())},
            "dependent_modules": [dict(m) for m in self.dependent_modules],
            "external_contracts": [
                {**c, "consumers": list(c["consumers"])} for c in self.external_contracts
            ],
            "test_coverage": {k: list(v) for k, v in sorted(self.test_coverage.items())},
            "uncovered": list(self.uncovered),
        }

    def to_markdown(self) -> str:
        out = ["# Impact report", ""]
        out.append(f"- Files: {', '.join(f'`{f}`' for f in self.files) or '(none)'}")
        if self.unknown_files:
            out.append("- Not in the graph (new files?): "
                       + ", ".join(f"`{f}`" for f in self.unknown_files))
        out.append(f"- Depth: {self.depth}")
        out.append(f"- Graph state: {self.graph_state}")

        out += ["", "## Symbols"]
        out += [f"- `{s.label}` ({s.role}) {s.file}:{s.line}" for s in self.symbols] or ["- none"]

        out += ["", "## Callers"]
        any_callers = False
        for hop, cs in sorted(self.callers.items()):
            for c in cs:
                any_callers = True
                out.append(f"- hop {hop}: `{c.symbol}` in {c.file} ({c.relation})")
        if not any_callers:
            out.append("- none")

        out += ["", "## Dependent modules"]
        out += [f"- {m['file']} (hop {m['hop']})" for m in self.dependent_modules] or ["- none"]

        out += ["", "## External contracts"]
        out += [f"- `{c['label']}` in {c['file']}: used by {', '.join(c['consumers'])}"
                for c in self.external_contracts] or ["- none"]

        out += ["", "## Test coverage"]
        out += [f"- {sid}: {', '.join(tests) if tests else 'NONE'}"
                for sid, tests in sorted(self.test_coverage.items())] or ["- none"]

        out += ["", "## Uncovered"]
        out += [f"- {sid}" for sid in self.uncovered] or ["- none"]
        return "\n".join(out) + "\n"


class _Index:
    def __init__(self, graph: dict):
        self.nodes: dict[str, dict] = {n["id"]: n for n in graph.get("nodes", []) if "id" in n}
        self.rev: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        self.fwd: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        containers: set[str] = set()
        rationale: set[str] = set()
        for e in graph.get("links", graph.get("edges", [])):
            s, t, rel = e.get("source"), e.get("target"), str(e.get("relation", ""))
            if s not in self.nodes or t not in self.nodes:
                continue
            efile = str(e.get("source_file") or self.nodes[s].get("source_file") or "")
            self.rev[t].append((s, rel, efile))
            self.fwd[s].append((t, rel, efile))
            if rel == "contains":
                containers.add(s)
            elif rel == "rationale_for":
                rationale.add(s)
        self.rationale = rationale
        self.file_nodes = {
            nid for nid in containers
            if (n := self.nodes[nid]).get("source_file")
            and str(n.get("label", "")) in _file_labels(str(n["source_file"]))
        }

    def is_code(self, nid: str) -> bool:
        n = self.nodes[nid]
        return (bool(n.get("source_file")) and n.get("file_type") in (None, "", "code")
                and nid not in self.rationale)

    def is_symbol(self, nid: str) -> bool:
        return self.is_code(nid) and nid not in self.file_nodes

    def file_of(self, nid: str) -> str:
        return str(self.nodes[nid].get("source_file") or "")

    def symbol(self, nid: str, role: str) -> Symbol:
        n = self.nodes[nid]
        return Symbol(nid, str(n.get("label", nid)), self.file_of(nid), _line(n), role)


def _file_labels(path: str) -> set[str]:
    base = posixpath.basename(path)
    return {path, base, posixpath.splitext(base)[0]}


def _line(node: dict) -> int:
    loc = str(node.get("source_location") or "")
    digits = loc.lstrip("Ll")
    head = ""
    for ch in digits:
        if not ch.isdigit():
            break
        head += ch
    return int(head) if head else 0


def _normalize_files(root: Path, files) -> list[str]:
    roots = {str(root), str(root.resolve())}
    out: set[str] = set()
    for raw in files:
        f = str(raw).replace("\\", "/")
        if posixpath.isabs(f):
            for r in roots:
                r = r.replace("\\", "/").rstrip("/")
                if f.startswith(r + "/"):
                    f = f[len(r) + 1:]
                    break
        f = posixpath.normpath(f)
        if f and f != ".":
            out.add(f)
    return sorted(out)


def _index(root: Path) -> _Index:
    g = load_graph(root)
    if g is None:
        raise GraphUnavailable(f"no readable graph at {graph_path(root)}")
    return _Index(g)


def _symbols(ix: _Index, files: list[str]) -> SymbolList:
    wanted = set(files)
    found: dict[str, Symbol] = {}
    known: set[str] = set()
    for nid in ix.nodes:
        f = ix.file_of(nid)
        if f in wanted:
            known.add(f)
            if ix.is_symbol(nid):
                found[nid] = ix.symbol(nid, "defined")
    for nid in list(ix.nodes):
        for tgt, _rel, efile in ix.fwd.get(nid, ()):
            if efile in wanted and tgt not in found and ix.is_symbol(tgt) \
                    and ix.file_of(tgt) not in wanted:
                found[tgt] = ix.symbol(tgt, "touched")
    ordered = sorted(found.values(), key=lambda s: (s.file, s.line, s.label, s.id))
    return SymbolList(ordered, [f for f in files if f not in known])


def symbols_for_files(root: Path, files) -> SymbolList:
    """Symbols defined in (or touched by) the given files. Unknown files go to `.unknown_files`."""
    return _symbols(_index(root), _normalize_files(root, files))


def _reverse_bfs(ix: _Index, seeds: set[str], depth: int, skip: set[str]) -> dict[str, tuple[int, str]]:
    """node -> (shortest hop, relation) over inbound CALLER_RELATIONS edges. Seeds excluded."""
    seen = set(seeds) | skip
    best: dict[str, tuple[int, str]] = {}
    frontier = sorted(seeds)
    for hop in range(1, depth + 1):
        found: dict[str, str] = {}
        for nid in frontier:
            for src, rel, _ in ix.rev.get(nid, ()):
                if (rel in CALLER_RELATIONS and src not in seen and ix.is_code(src)
                        and (src not in found or rel < found[src])):
                    found[src] = rel
        for src, rel in found.items():
            best[src] = (hop, rel)
        seen.update(found)
        frontier = sorted(found)
        if not frontier:
            break
    return best


def _blast(ix: _Index, files: list[str], depth: int) -> BlastRadius:
    if depth < 1:
        raise ValueError("depth must be >= 1")
    syms = _symbols(ix, files)
    wanted = set(files)
    defined = {s.id for s in syms if s.role == "defined"}
    in_files = {nid for nid in ix.nodes if ix.file_of(nid) in wanted}
    reached = _reverse_bfs(ix, defined, depth, in_files)

    callers: dict[int, list[Caller]] = {h: [] for h in range(1, depth + 1)}
    for nid, (hop, rel) in reached.items():
        n = ix.nodes[nid]
        callers[hop].append(Caller(nid, str(n.get("label", nid)), ix.file_of(nid), hop, rel))
    for cs in callers.values():
        cs.sort(key=lambda c: (c.file, c.symbol, c.id))

    mods: dict[str, int] = {}
    for hop, cs in callers.items():
        for c in cs:
            if c.file not in wanted:
                mods[c.file] = min(hop, mods.get(c.file, hop))
    for tgt, edges in ix.rev.items():
        if ix.file_of(tgt) in wanted:
            for src, rel, efile in edges:
                f = ix.file_of(src) or efile
                if rel in IMPORT_RELATIONS and f and f not in wanted and src not in ix.rationale:
                    mods[f] = 1

    contracts = []
    for s in syms:
        if s.role != "defined" or s.label.lstrip(".").startswith("_"):
            continue
        consumers = {
            ix.file_of(src) for src, rel, _ in ix.rev.get(s.id, ())
            if rel in CONSUMER_RELATIONS and src in ix.nodes and ix.is_code(src)
            and ix.file_of(src) != s.file
        }
        if consumers:
            contracts.append({"id": s.id, "label": s.label, "file": s.file,
                              "consumers": sorted(consumers)})

    return BlastRadius(
        files=files, unknown_files=list(syms.unknown_files), depth=depth, symbols=list(syms),
        callers=callers,
        dependent_modules=[{"file": f, "hop": h} for f, h in sorted(mods.items())],
        external_contracts=contracts,
    )


def blast_radius(root: Path, files, depth: int = 2) -> BlastRadius:
    """Callers of the modified files' symbols by hop, dependent modules and external contracts."""
    if depth < 1:
        raise ValueError("depth must be >= 1")
    return _blast(_index(root), _normalize_files(root, files), depth)


def _is_test_file(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (any(p in ("tests", "test") for p in parts[:-1])
            or (name.startswith("test_") and name.endswith(".py"))
            or name.endswith("_test.py"))


def _coverage(ix: _Index, ids: list[str], modified: set[str]) -> dict[str, list[str]]:
    modified_tests = {f for f in modified if _is_test_file(f)}
    out: dict[str, list[str]] = {}
    for sid in ids:
        tests = set(modified_tests)
        if sid in ix.nodes:
            for nid in _reverse_bfs(ix, {sid}, TEST_HOPS, set()):
                f = ix.file_of(nid)
                if _is_test_file(f):
                    tests.add(f)
            if _is_test_file(ix.file_of(sid)):
                tests.add(ix.file_of(sid))
        out[sid] = sorted(tests)
    return out


def test_coverage_map(root: Path, symbols, files=()) -> dict[str, list[str]]:
    """symbol id -> sorted test files reaching it within 2 hops (or modified test files)."""
    ids = sorted({s.id if hasattr(s, "id") else str(s) for s in symbols})
    return _coverage(_index(root), ids, set(_normalize_files(root, files)))


test_coverage_map.__test__ = False  # not a pytest test, despite the name


def impact(root: Path, files, depth: int = 2) -> ImpactReport:
    """Compose symbols, blast radius and test coverage into one deterministic report."""
    if depth < 1:
        raise ValueError("depth must be >= 1")
    ix = _index(root)
    norm = _normalize_files(root, files)
    b = _blast(ix, norm, depth)
    ids = sorted(s.id for s in b.symbols if s.role == "defined")
    cov = _coverage(ix, ids, set(norm))
    return ImpactReport(
        files=b.files, unknown_files=b.unknown_files, depth=depth,
        graph_state=graph_freshness(root), symbols=b.symbols, callers=b.callers,
        dependent_modules=b.dependent_modules, external_contracts=b.external_contracts,
        test_coverage=cov, uncovered=[i for i in ids if not cov[i]],
    )


# --------------------------------------------------------------------------------------------
# Ticket ego-network slice: a small, bounded view of the graph around what a ticket mentions.
# Ticket text is untrusted: it is only matched against graph labels and paths, never rendered.
# --------------------------------------------------------------------------------------------

SLICE_MAX_SEEDS = 8
SLICE_HOP_CAP = 10  # nodes per seed, per direction, per hop
SLICE_BUDGET_CHARS = 3000
_TRUNC = "... (truncated)"
_PATH_RE = re.compile(r"[\w.\-]+(?:/[\w.\-]+)+|[\w\-]+\.[A-Za-z]{1,5}\b")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


@dataclass(frozen=True)
class SliceResult:
    seeds: list[str]
    nodes: int
    text: str


def _clean(value: object, limit: int = 80) -> str:
    s = re.sub(r"[\s`]+", " ", str(value)).strip()
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _sym_name(label: str) -> str:
    return label.strip().lstrip(".").removesuffix("()").strip()


def _slice_seeds(root: Path, ix: _Index, text: str) -> list[str]:
    files: dict[str, str] = {}
    for nid in ix.nodes:
        f = ix.file_of(nid)
        if f and ix.is_code(nid):
            files[f] = f
    names: dict[str, list[str]] = defaultdict(list)
    for nid in ix.nodes:
        if ix.is_symbol(nid):
            names[_sym_name(str(ix.nodes[nid].get("label", "")))].append(nid)

    seeds: set[str] = set()
    rest = text
    matched_files: list[str] = []
    for tok in _PATH_RE.findall(text):
        norm = _normalize_files(root, [tok.strip(".")])
        if norm and norm[0] in files:
            matched_files.append(norm[0])
            rest = rest.replace(tok, " ")
    if matched_files:
        for s in _symbols(ix, sorted(set(matched_files))):
            if s.role == "defined":
                seeds.add(s.id)
    for tok in _IDENT_RE.findall(rest):
        parts = tok.split(".")
        for cand in {tok, parts[-1], parts[0]}:
            seeds.update(names.get(cand, ()))
    order = sorted(seeds, key=lambda i: (ix.file_of(i), _line(ix.nodes[i]),
                                         str(ix.nodes[i].get("label", "")), i))
    return order[:SLICE_MAX_SEEDS]


def _walk(ix: _Index, start: str, edges: dict, hops: int) -> list[tuple[int, str, str]]:
    seen = {start}
    frontier = [start]
    out: list[tuple[int, str, str]] = []
    for hop in range(1, hops + 1):
        found: dict[str, str] = {}
        for nid in frontier:
            for other, rel, _ in edges.get(nid, ()):
                if (other not in seen and other in ix.nodes and ix.is_code(other)
                        and rel != "contains" and (other not in found or rel < found[other])):
                    found[other] = rel
        ranked = sorted(found, key=lambda i: (ix.file_of(i), _line(ix.nodes[i]),
                                              str(ix.nodes[i].get("label", "")), i))[:SLICE_HOP_CAP]
        seen.update(ranked)
        out += [(hop, i, found[i]) for i in ranked]
        frontier = ranked
        if not frontier:
            break
    return out


def _fmt(ix: _Index, nid: str, hop: int, rel: str) -> str:
    n = ix.nodes[nid]
    return (f"- {_clean(n.get('label', nid))} ({_clean(ix.file_of(nid), 120)}:{_line(n)}) "
            f"via {_clean(rel, 30)}, hop {hop}")


def ticket_slice(root: Path, text: str, *, hops: int = 2,
                 budget_chars: int = SLICE_BUDGET_CHARS) -> SliceResult | None:
    """1-2 hop ego network around the files/symbols a ticket names; None when nothing matches."""
    try:
        if not text or not text.strip():
            return None
        hops = max(1, min(2, int(hops)))
        ix = _index(root)
        seeds = _slice_seeds(root, ix, text)
        if not seeds:
            return None
        tests = _coverage(ix, seeds, set())
        lines: list[str] = []
        seen_nodes: set[str] = set(seeds)
        for sid in seeds:
            n = ix.nodes[sid]
            lines.append(f"### {_clean(n.get('label', sid))} "
                         f"({_clean(ix.file_of(sid), 120)}:{_line(n)})")
            callers = _walk(ix, sid, ix.rev, hops)
            deps = _walk(ix, sid, ix.fwd, hops)
            seen_nodes.update(i for _, i, _ in callers + deps)
            lines.append("Callers (who is affected):")
            lines += [_fmt(ix, i, h, r) for h, i, r in callers] or ["- none"]
            lines.append("Dependencies:")
            lines += [_fmt(ix, i, h, r) for h, i, r in deps] or ["- none"]
            lines.append("Tests that exercise these:")
            lines += [f"- {_clean(f, 120)}" for f in tests.get(sid, [])] or ["- none"]
            lines.append("")
        body = "\n".join(lines).rstrip()
        if len(body) > budget_chars:
            body = body[: max(0, budget_chars - len(_TRUNC) - 1)].rstrip() + "\n" + _TRUNC
        labels = [_clean(ix.nodes[s].get("label", s)) for s in seeds]
        return SliceResult(seeds=labels, nodes=len(seen_nodes), text=body)
    except Exception:  # noqa: BLE001 - a slice is best effort and must never stop a ticket
        return None


# --------------------------------------------------------------------------------------------
# Reviewer blast-radius checklist: graph-derived, bounded, and sanitised.
# --------------------------------------------------------------------------------------------

CHECKLIST_BUDGET_CHARS = 4000


@dataclass(frozen=True)
class ChecklistResult:
    text: str
    callers_total: int
    callers_verified: int
    callers_unverified: int


def impacted_tests(report: ImpactReport, diff_files) -> list[str]:
    """Sorted union of covering test files and modified test files in the diff."""
    out = {f for tests in report.test_coverage.values() for f in tests}
    out.update(f for f in diff_files if _is_test_file(f))
    return sorted(out)


def reviewer_checklist(report: ImpactReport, diff_files,
                       *, budget_chars: int = CHECKLIST_BUDGET_CHARS) -> ChecklistResult:
    """Render a deterministic contract-verification checklist from an impact report."""
    diff = set(diff_files)
    all_callers = sorted((c for cs in report.callers.values() for c in cs),
                         key=lambda c: (c.hop, c.file, c.symbol, c.id))
    verified = sum(1 for c in all_callers if c.file in diff or _is_test_file(c.file))
    total = len(all_callers)
    lines: list[str] = []
    for c in sorted(report.external_contracts, key=lambda c: (str(c.get("file")), str(c.get("label")))):
        consumers = ", ".join(_clean(x, 60) for x in sorted(map(str, c.get("consumers", []))))
        lines.append(f"- [ ] Contract `{_clean(c.get('label'))}` in {_clean(c.get('file'), 120)} "
                     f"is unchanged for its consumers: {consumers or 'unknown'}")
    for c in sorted(report.callers.get(1, []), key=lambda c: (c.file, c.symbol, c.id)):
        if c.file in diff:
            continue
        lines.append(f"- [ ] Caller `{_clean(c.symbol)}` in {_clean(c.file, 120)} "
                     f"({_clean(c.relation, 30)}) still works with the change")
    for m in sorted(report.dependent_modules, key=lambda m: (str(m.get("file")), m.get("hop", 0))):
        if m.get("file") in diff:
            continue
        lines.append(f"- [ ] Dependent module {_clean(m.get('file'), 120)} "
                     f"(hop {_clean(m.get('hop'), 5)}) is not broken")
    labels = {s.id: s.label for s in report.symbols}
    for sid in sorted(report.uncovered):
        lines.append(f"- [ ] Symbol `{_clean(labels.get(sid, sid))}` has no covering test; "
                     "check it by reading the diff")
    if report.unknown_files:
        lines.append("- Not in the graph (may be incomplete): "
                     + ", ".join(_clean(f, 120) for f in sorted(report.unknown_files)))
    if not lines:
        lines = ["No blast radius found in the graph for this diff."]
    text = "\n".join(lines)
    if len(text) > budget_chars:
        text = text[: max(0, budget_chars - len(_TRUNC) - 1)].rstrip() + "\n" + _TRUNC
    return ChecklistResult(text, total, verified, total - verified)
