"""Build the local code graph with graphify. Local AST parse only: no LLM, nothing uploaded."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GraphResult:
    ran: bool
    ok: bool
    nodes: int = 0
    edges: int = 0
    message: str = ""

    def summary(self) -> str:
        if not self.ran:
            return f"Code graph: not built ({self.message})."
        if not self.ok:
            return f"Code graph: build failed ({self.message})."
        return (
            f"Code graph: {self.nodes} nodes, {self.edges} edges in graphify-out/. "
            'Query it with `graphify query "<question>"`; see graphify-out/GRAPH_REPORT.md.'
        )


def _count(graph_json: Path) -> tuple[int, int]:
    try:
        data = json.loads(graph_json.read_text())
    except (OSError, json.JSONDecodeError):
        return 0, 0
    edges = data.get("links", data.get("edges", []))
    return len(data.get("nodes", [])), len(edges)


def build_graph(root: Path, *, timeout: int = 900) -> GraphResult:
    binary = shutil.which("graphify")
    if not binary:
        return GraphResult(False, False, message="graphify not installed; `uv tool install graphifyy`")
    try:
        proc = subprocess.run(
            [binary, "update", str(root)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=root,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return GraphResult(True, False, message=str(exc))
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["unknown error"]
        return GraphResult(True, False, message=tail[0])
    nodes, edges = _count(root / "graphify-out" / "graph.json")
    return GraphResult(True, nodes > 0, nodes, edges, "" if nodes else "empty graph")
