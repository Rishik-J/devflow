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
| Agent drivers: Claude Code, Codex, Cursor and OpenClaw, all verified live | done |
| Usage-based routing of coding work across claude / codex / cursor (`devflow agents`) | done |
| Single-ticket pipeline: refine, test design, implement, gates, review, merge (`devflow ticket N`) | done; verified live with real agents on a scratch repo |
| Gates judged against the base commit (pre-existing failures do not block tickets) | done |
| GitHub Projects board, scheduler, follow-up tickets (`devflow board`) | built and tested against recorded `gh` output; not yet run against a live Project |
| Learning loop, `devflow run` | not built yet (see plan) |

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

## Running a ticket

```bash
devflow setup-openclaw           # once: creates the OpenClaw agent used for planning and triage
devflow init                     # once per repo
devflow ticket 12 --dry-run      # reads the issue, logs what it would write to GitHub
devflow ticket 12 --watch 30     # work the issue; poll for your comments every 30s
devflow tickets                  # where every ticket stands
```

It stops whenever it needs you, and you answer with a comment on the issue (or the PR for merge):
`/approve`, `/reject <why>`, `/answer <text>`, `/resume`. Only logins in `approvers` count
(default: the `gh` user), and each comment is used once. Plan and merge approval can be switched
off in `[human_gates]`. Failures from before the ticket (a linter nobody enforces) are ignored;
only new ones are blamed on it. The reviewer never repeats the gates: the engine runs them in a
clean checkout and hands over the results as facts.

## The board

Run `gh auth refresh -h github.com -s project` once, then `devflow board-create`. It makes a
private Project with the five Status options and a Priority field (P0-P3), links it to the repo,
and writes the config for you. To use a Project you already have, set this in
`.devflow/config.toml` instead (its Status field needs the same five options):

```toml
[github]
project = 3
owner = "your-login"
```

You move a ticket to **Ready** (and give it the `devflow` label); devflow works it, moves it
through In progress and In review to Done, and reads `Priority` (P0 before P1) and
`Blocked by #N` in the issue body to choose the order. Work it needs doing that it noticed
(the planner's `follow_ups` and the reviewer's non-blocking major or minor findings) is filed as
new issues in **Backlog**, never Ready, at most three per ticket, skipping near-duplicates of
open issues. `devflow board` shows the queue and what would run next; `--dry-run` is read-only.

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
