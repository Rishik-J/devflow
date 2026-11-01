"""devflow command line."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .init import InitError, run_init


def _init(args: argparse.Namespace) -> int:
    try:
        r = run_init(Path(args.path).resolve(), force=args.force, use_graph=not args.no_graph)
    except InitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"devflow init: {r.root}")
    if r.profile.monorepo:
        print(f"  monorepo: {len(r.profile.projects)} projects detected")
    for p in r.profile.projects:
        print(f"  - {p.path} ({p.kind}): gates {', '.join(p.gates) or 'none'}")
    print(f"  {r.graph.summary() if r.graph else 'Code graph: skipped (--no-graph).'}")
    print(f"  created {len(r.created)} file(s), left {len(r.skipped)} existing file(s) alone")
    if r.needs_input:
        print("\nNeeds your input (edit .devflow/config.toml):")
        for item in r.needs_input:
            print(f"  * {item}")
    return 0


def _doctor(_: argparse.Namespace) -> int:
    def has(tool: str) -> bool:
        return shutil.which(tool) is not None

    gh_ok = False
    if has("gh"):
        gh_ok = (
            subprocess.run(["gh", "auth", "status"], capture_output=True, check=False).returncode
            == 0
        )
    checks = [
        ("git", has("git"), "required"),
        ("gh (authenticated)", gh_ok, "required for PRs"),
        ("claude", has("claude"), "coding agent"),
        ("codex", has("codex"), "coding/review agent"),
        ("graphify", has("graphify"), "code graph: `uv tool install graphifyy`"),
        ("openclaw", has("openclaw"), "planner / triage / notifications"),
    ]
    for name, ok, note in checks:
        print(f"  {'ok ' if ok else 'MISSING'}  {name:<20} {note}")
    required = has("git") and gh_ok and (has("claude") or has("codex"))
    return 0 if required else 1


def _graph(args: argparse.Namespace) -> int:
    from . import graphctx
    from .detect import repo_root

    root = repo_root(Path(args.path).resolve())
    if root is None:
        print("error: not inside a git repository", file=sys.stderr)
        return 2
    cmd = args.graph_cmd
    if cmd == "status":
        fresh = graphctx.is_fresh(root)
        exists = graphctx.graph_path(root).exists()
        print("graph: " + ("up to date" if fresh else "stale" if exists else "not built"))
        return 0 if fresh else 1
    if cmd == "refresh":
        st = graphctx.ensure_fresh(root, force=args.force)
        label = "rebuilt" if st.refreshed else st.message
        print(f"graph: {label} ({st.nodes} nodes, {st.edges} edges)" if st.ok
              else f"graph: FAILED ({st.message})")
        return 0 if st.ok else 1
    if cmd == "context":
        graphctx.ensure_fresh(root)
        print(graphctx.digest(root, args.files or None))
        return 0
    binary = shutil.which("graphify")
    if not binary:
        print("error: graphify not installed (uv tool install graphifyy)", file=sys.stderr)
        return 2
    return subprocess.run([binary, "hook", args.action], cwd=root, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devflow", description=__doc__)
    parser.add_argument("--version", action="version", version=f"devflow {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="tune the workflow to a repo (detect, graph, write config)")
    p_init.add_argument("path", nargs="?", default=".", help="repo path (default: cwd)")
    p_init.add_argument("--force", action="store_true", help="overwrite existing .devflow files")
    p_init.add_argument("--no-graph", action="store_true", help="skip building the code graph")
    p_init.set_defaults(func=_init)

    p_doc = sub.add_parser("doctor", help="check required tools")
    p_doc.set_defaults(func=_doctor)

    p_graph = sub.add_parser("graph", help="keep the code graph fresh; print agent context")
    gsub = p_graph.add_subparsers(dest="graph_cmd", required=True)
    for name, help_text in (
        ("status", "is the graph up to date with the working tree?"),
        ("refresh", "rebuild the graph if the tree changed (use --force to always rebuild)"),
        ("context", "print the digest agents receive (use --files to add a blast radius)"),
    ):
        g = gsub.add_parser(name, help=help_text)
        g.add_argument("path", nargs="?", default=".", help="repo or worktree (default: cwd)")
        g.set_defaults(func=_graph, graph_cmd=name)
        if name == "refresh":
            g.add_argument("--force", action="store_true")
        if name == "context":
            g.add_argument("--files", nargs="*", default=[], help="repo-relative paths")
    g_hooks = gsub.add_parser("hooks", help="install/remove graphify's git hooks (rebuild on commit)")
    g_hooks.add_argument("action", choices=["install", "uninstall", "status"])
    g_hooks.add_argument("path", nargs="?", default=".")
    g_hooks.set_defaults(func=_graph, graph_cmd="hooks")

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
