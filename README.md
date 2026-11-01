# devflow

A reusable ticket-to-merge workflow for any git repo. Deterministic code owns state, gates and
git. Agents (OpenClaw, Claude Code, Codex) are called only where judgment is needed: refining a
plan, writing code, reviewing it, and triaging feedback. See [DESIGN.md](DESIGN.md).

## Status

| Piece | State |
|---|---|
| `devflow init` (detect repo, build code graph, write `.devflow/`) | done |
| `devflow doctor` (check required tools) | done |
| Ticket state machine with retry caps | done |
| Foundations: SQLite store, config loader, schema validation, worktrees, env setup, gate runner, diff guard | done |
| Code graph kept fresh on every edit + agent context (`devflow graph ...`) | done, not yet wired into agents |
| Agent drivers: Claude Code, Codex, OpenClaw verified live; Cursor written, needs `agent login` to verify | done |
| Usage-based routing of coding work across claude / codex / cursor (`devflow agents`) | done |
| Stages, engine, GitHub Projects, learning loop, `devflow run` | not built yet (see plan) |

## Install

```bash
uv tool install --editable /Users/rjdev/Desktop/devflow   # puts `devflow` on PATH
devflow doctor
```

Editable means changes to this folder take effect immediately. After changing dependencies in
`pyproject.toml`, run `uv tool install --editable --force /Users/rjdev/Desktop/devflow`.

## Use it on a repo

```bash
cd /path/to/any/git/repo
devflow init            # or: devflow init /path/to/repo
```

`init` is tuned per repo. It detects Python, Node, Rust and Go projects (including monorepos),
their lint / typecheck / unit / integration / build commands, CI hints and convention files. It
builds the local code graph with `graphify update` (AST only, no LLM, nothing uploaded) and
writes, inside the target repo:

```
.devflow/config.toml          gates per project, limits, human gates, agent choices
.devflow/prompts/*.md         planner, test planner, coder, reviewer, triage, filled with repo facts
.devflow/schemas/*.json       contracts between stages (plan, test plan, review)
```

Commit `.devflow/` so the team shares the same workflow. `init` never overwrites existing files
(use `--force`), keeps `graphify-out/` and `.devflow/state/` out of `git status` through
`.git/info/exclude`, and ends with a list of things it could not detect (usually the
integration-test command).

## Code graph

```bash
devflow graph status                      # up to date with the working tree?
devflow graph refresh                     # rebuild only if the tree changed (--force to always)
devflow graph context --files src/a.py    # the digest agents get, with a blast radius for those files
devflow graph hooks install               # optional: also rebuild on commit/checkout/merge
```

Freshness is a fingerprint (HEAD plus a hash of every uncommitted and untracked change), so the
engine calls `ensure_fresh` before each agent starts and after each round of edits. An
incremental rebuild takes about a second and uses no LLM. A new worktree has no graph, so the
first call there builds it. Agents get the digest (`agent_preamble`) and the graphify tools
(`mcp_config` for Claude, `codex_overrides` for Codex) from `graphctx.py`.

## Coding agents and routing

`devflow doctor` checks which tools are installed and logged in. Coding-heavy roles (coder,
reviewer, test planner) are routed across `claude`, `codex` and `cursor` (the Cursor CLI, which
installs as `agent`) by `[routing]` in `.devflow/config.toml`:

- Each agent has a budget window (`window_hours`, `limit_usd`; 0 = no local cap). The agent with
  the most headroom gets the next job (`strategy = "priority"` uses the priority order instead).
- A rate or usage limit puts that agent on a cooldown that doubles on repeats (capped), and the
  job falls through to the next agent in the same call. Sessions are not portable across tools,
  so a switched job starts a fresh session.
- The reviewer prefers a different tool from the coder. `devflow agents` shows readiness, usage,
  headroom and cooldowns.

None of the tools reports a true credit balance, so usage is the real cost when the tool gives
one and a token-based estimate otherwise. Treat `limit_usd` as your own comfort limit.

Live checks of the real CLIs: `DEVFLOW_LIVE=1 pytest tests/test_live_agents.py`.

## Develop

```bash
cd /Users/rjdev/Desktop/devflow
uv run --no-project --with pytest --with-editable . python -m pytest -q
uv run --no-project --with ruff ruff check .
```

Per-repo behavior lives in templates (`src/devflow/templates`) and schemas
(`src/devflow/schemas`), so improving a prompt improves every repo the next time it is
initialized with `--force`.
