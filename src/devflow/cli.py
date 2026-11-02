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
    cursor_bin = "agent" if has("agent") else "cursor-agent"
    cursor_installed = has(cursor_bin)
    cursor_ready, cursor_why = (False, "not installed")
    if cursor_installed:
        from .agents import CursorAgent

        cursor_ready, cursor_why = CursorAgent(cursor_bin).check_ready()
    checks = [
        ("git", has("git"), "required"),
        ("gh (authenticated)", gh_ok, "required for PRs"),
        ("claude", has("claude"), "coding agent"),
        ("codex", has("codex"), "coding/review agent"),
        ("cursor (logged in)", cursor_ready,
         "coding agent" if cursor_ready else cursor_why or "coding agent"),
        ("graphify", has("graphify"), "code graph: `uv tool install graphifyy`"),
        ("openclaw", has("openclaw"), "planner / triage / notifications"),
    ]
    for name, ok, note in checks:
        print(f"  {'ok ' if ok else 'MISSING'}  {name:<20} {note}")
    coding = has("claude") or has("codex") or cursor_ready
    return 0 if has("git") and gh_ok and coding else 1


def _agents(args: argparse.Namespace) -> int:
    from .detect import repo_root
    from .loadconfig import ConfigError, load_config
    from .router import build_router
    from .store import Store, state_db_path

    root = repo_root(Path(args.path).resolve())
    if root is None:
        print("error: not inside a git repository", file=sys.stderr)
        return 2
    try:
        cfg = load_config(root)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    with Store(state_db_path(root)) as store:
        router = build_router(cfg, store)
        print(f"routing: {cfg.routing.strategy}; roles: {', '.join(cfg.routing.roles)}")
        print(f"  {'agent':<8} {'state':<14} {'used':>9} {'cap':>9} {'headroom':>9} {'runs':>5}")
        for s in router.statuses():
            if not s.ready:
                state = "not ready"
            elif s.cooldown_until:
                state = f"cooldown {max(0, int((s.cooldown_until - router.now()) / 60))}m"
            else:
                state = "ready"
            cap = f"${s.limit_usd:.2f}" if s.limit_usd else "none"
            print(f"  {s.name:<8} {state:<14} {'$' + format(s.used_usd, '.2f'):>9} {cap:>9} "
                  f"{s.headroom:>9.0%} {s.runs:>5}" + (f"   <- {s.reason}" if not s.ready else ""))
    return 0


def _ticket(args: argparse.Namespace) -> int:
    from .detect import repo_root
    from .runner import ownership
    root = repo_root(Path(args.path).resolve())
    if root is None:
        raise ValueError("not inside a git repository")
    with ownership(root):
        return _ticket_owned(args)


def _ticket_owned(args: argparse.Namespace) -> int:
    import time

    from .agents import AgentError
    from .detect import repo_root
    from .host import HostError
    from .loadconfig import ConfigError
    from .runtime import build_engine

    root = repo_root(Path(args.path).resolve())
    if root is None:
        print("error: not inside a git repository", file=sys.stderr)
        return 2
    try:
        engine, store, _ = build_engine(root, dry_run=args.dry_run, log=print)
    except (ConfigError, HostError, AgentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    outcome = None
    try:
        while True:
            outcome = engine.run(args.issue)
            print(f"#{args.issue}: {outcome.status} - {outcome.ticket.state.value}"
                  + (f" ({outcome.reason})" if outcome.reason else ""))
            if outcome.status != "paused" or not args.watch:
                break
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("stopped; run again to resume")
    finally:
        if args.dry_run:
            print("dry run: would have written to GitHub:")
            for w in getattr(engine.host, "writes", []):
                print(f"  - {w}")
            for w in getattr(engine.board, "writes", []):
                print(f"  - {w}")
            engine.cleanup_dry_run()
        store.close()
    if outcome is None:
        return 130
    return {"done": 0, "paused": 0, "escalated": 3}.get(outcome.status, 1)


def _board_create(args: argparse.Namespace) -> int:
    from . import board_setup
    from .board import BoardError
    from .detect import repo_root
    from .host import repo_slug
    from .loadconfig import ConfigError, load_config

    root = repo_root(Path(args.path).resolve())
    if root is None:
        print("error: not inside a git repository", file=sys.stderr)
        return 2
    try:
        cfg = load_config(root)
        if cfg.board.enabled:
            print(f"error: this repo already uses project #{cfg.board.project}", file=sys.stderr)
            return 2
        slug = repo_slug(cfg.remote)
        owner = args.owner or slug.split("/")[0]
        title = args.title or f"{cfg.name}: devflow queue"
        made = board_setup.create_project(owner, title, slug)
        board_setup.write_config(root, made.number, owner)
    except (ConfigError, BoardError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"created project #{made.number}: {made.url}")
    print("statuses: " + ", ".join(board_setup.required_statuses()) + "; priority: P0-P3")
    print(f"linked to {slug} and written to .devflow/config.toml")
    print("next: give an issue the 'devflow' label, move it to Ready, run `devflow board`")
    return 0


def _board(args: argparse.Namespace) -> int:
    from .board import BoardError, DryRunBoard
    from .detect import repo_root
    from .host import GhHost, HostError, repo_slug
    from .loadconfig import ConfigError, load_config
    from .runtime import make_board
    from .scheduler import select
    from .store import Store, state_db_path

    root = repo_root(Path(args.path).resolve())
    if root is None:
        print("error: not inside a git repository", file=sys.stderr)
        return 2
    try:
        cfg = load_config(root)
        board = make_board(cfg)
        if board is None:
            print("no board configured: set [github] project = N in .devflow/config.toml",
                  file=sys.stderr)
            return 2
        host = GhHost(repo_slug(cfg.remote))
        with Store(state_db_path(root)) as store:
            sel = select(DryRunBoard(board) if args.dry_run else board, host, store, cfg.board)
    except (ConfigError, BoardError, HostError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"board: project #{cfg.board.project}, label {cfg.board.label!r}, "
          f"max_active {cfg.board.max_active}")
    print(f"working now: {sel.working}" + (f" ({', '.join('#' + str(n) for n in sel.resume)})"
                                           if sel.resume else ""))
    print("ready, in the order they would run:")
    for item in sel.ready:
        print(f"  #{item.issue:<5} {item.priority or '-':<4} {item.title}")
    if not sel.ready:
        print("  (none)")
    for number, why in sel.skipped:
        print(f"  skipped #{number}: {why}")
    print(f"next: {'#' + str(sel.pick.issue) if sel.pick else 'nothing'} - {sel.reason}")
    return 0


def _tickets(args: argparse.Namespace) -> int:
    from .detect import repo_root
    from .store import Store, state_db_path

    root = repo_root(Path(args.path).resolve())
    if root is None or not state_db_path(root).exists():
        print("no tickets yet")
        return 0
    with Store(state_db_path(root)) as store:
        for t in store.list_tickets():
            pr = f" PR #{t.sessions['pr']}" if t.sessions.get("pr") else ""
            print(f"{t.id:>6}  {t.state.value:<26} attempts {t.implement_attempts} "
                  f"rounds {t.review_round}{pr}  {t.title}")
    return 0


def _setup_openclaw(args: argparse.Namespace) -> int:
    from . import openclaw_setup

    try:
        result = openclaw_setup.setup(args.model)
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(result.detail)
    return 0


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

    p_ticket = sub.add_parser("ticket", help="work one GitHub issue until it needs you or is done")
    p_ticket.add_argument("issue", type=int)
    p_ticket.add_argument("path", nargs="?", default=".")
    p_ticket.add_argument("--dry-run", action="store_true",
                          help="read GitHub but only log what would be written (agents still run)")
    p_ticket.add_argument("--watch", type=int, default=0, metavar="SECONDS",
                          help="keep polling for your /approve etc. every N seconds")
    p_ticket.set_defaults(func=_ticket)

    p_board = sub.add_parser("board", help="show the GitHub Project queue and what runs next")
    p_board.add_argument("path", nargs="?", default=".")
    p_board.add_argument("--dry-run", action="store_true")
    p_board.set_defaults(func=_board)

    p_create = sub.add_parser("board-create", help="create and link this repo's GitHub Project")
    p_create.add_argument("--title", help="Project title (default: '<repo>: devflow queue')")
    p_create.add_argument("--owner", help="user or org that will own it (default: the repo owner)")
    p_create.add_argument("path", nargs="?", default=".")
    p_create.set_defaults(func=_board_create)

    p_tickets = sub.add_parser("tickets", help="list tickets devflow is working on")
    p_tickets.add_argument("path", nargs="?", default=".")
    p_tickets.set_defaults(func=_tickets)

    p_oc = sub.add_parser("setup-openclaw", help="create the OpenClaw agent used for planning")
    p_oc.add_argument("--model", default="claude-cli/claude-sonnet-5-5")
    p_oc.set_defaults(func=_setup_openclaw)

    p_agents = sub.add_parser("agents", help="show coding-agent routing: readiness, usage, headroom")
    p_agents.add_argument("path", nargs="?", default=".")
    p_agents.set_defaults(func=_agents)

    p_graph = sub.add_parser("graph",help="keep the code graph fresh; print agent context")
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

    from .runner import command as run_queue
    queue = sub.add_parser("run", help="work Ready tickets and resume approvals in the configured Project")
    queue.add_argument("path", nargs="?", default=".")
    queue.add_argument("--dry-run", action="store_true", help="preview queue without launching agents")
    queue.add_argument("--watch", type=int, default=0, help="poll interval in seconds; omit for one sweep")
    queue.add_argument("--max-tickets", type=int, default=10)
    queue.add_argument("--max-minutes", type=float, default=60)
    queue.add_argument("--max-cost-usd", type=float, default=0,
                       help="recorded cost ceiling for this invocation; 0 disables it")
    queue.set_defaults(func=run_queue)

    resume = sub.add_parser("continue", help="acknowledge the three-ticket checkpoint")
    resume.add_argument("path", nargs="?", default=".")
    def continue_queue(args):
        from .checkpoint import acknowledge
        from .detect import repo_root
        root = repo_root(Path(args.path).resolve())
        if root is None:
            raise ValueError("not inside a git repository")
        print("checkpoint acknowledged" if acknowledge(root) else "no checkpoint pending")
        return 0
    resume.set_defaults(func=continue_queue)

    from .learn.cli import run as learn_run
    from .learn.cli import stats as learn_stats

    learn = sub.add_parser("learn", help="inspect, approve, retire or publish repository lessons")
    actions = learn.add_subparsers(dest="learn_action", required=True)
    for action in ("list", "approve", "reject", "publish", "retro"):
        command = actions.add_parser(action)
        if action in ("approve", "reject"):
            command.add_argument("lesson_id")
        if action == "retro":
            command.add_argument("issue", type=int)
            command.add_argument("--model", help="model override for this retro run only")
        command.add_argument("path", nargs="?", default=".")
        command.set_defaults(func=learn_run)
    stats = sub.add_parser("stats", help="summarise ticket results and recorded model usage")
    stats.add_argument("path", nargs="?", default=".")
    stats.set_defaults(func=learn_stats)

    from .telemetry_cli import command as trace_command
    tracing = sub.add_parser("telemetry", help="inspect, replay and test workflow traces")
    tracing_actions = tracing.add_subparsers(dest="telemetry_action", required=True)
    for action in ("status", "metrics", "replay", "demo"):
        trace_parser = tracing_actions.add_parser(action)
        trace_parser.add_argument("path", nargs="?", default=".")
        if action == "replay":
            trace_parser.add_argument("--limit", type=int, default=1000)
        trace_parser.set_defaults(func=trace_command)

    args = parser.parse_args(argv)
    from . import telemetry
    from .detect import repo_root
    trace_root = repo_root(Path(getattr(args, "path", ".")).resolve())
    telemetry.configure(trace_root)
    try:
        return args.func(args)
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        telemetry.flush()


if __name__ == "__main__":
    raise SystemExit(main())
