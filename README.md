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
| Code graph kept fresh on every edit + agent context (`devflow graph ...`) | wired into agent requests, including retro |
| Agent drivers: Claude Code, Codex, Cursor and OpenClaw, all verified live | done |
| Usage-based routing of coding work across claude / codex / cursor (`devflow agents`) | done |
| Single-ticket pipeline: refine, test design, implement, gates, review, merge (`devflow ticket N`) | done; verified live with real agents on a scratch repo |
| Gates judged against the base commit (pre-existing failures do not block tickets) | done |
| GitHub Projects board, scheduler, follow-up tickets (`devflow board`) | verified on Project #6 and real ticket #9 |
| Learning ledger, retro, lesson commands, context-update PRs, stats | implemented |
| `devflow run` queue runner, recorded-cost/time limits, STOP and exclusive ownership | implemented; queue preview verified on Project #6 |

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

## Learning from runs

Newly initialised repos configure `retro = "openclaw:devflow"` under `[agents]`.
For an existing repo, add that setting without overwriting your `[github]` settings.
Run records are stored locally even when the retro model fails. Lessons start as candidates;
they become active after independent evidence from two tickets or explicit human approval.
Active lessons are selected by role and file scope for later prompts.

```bash
devflow learn retro 9                     # replay a finished ticket, once per outcome
devflow learn retro 9 --model google-vertex/gemini-3.8-flash # one-run model override
devflow learn list
devflow learn approve L-xxxxxxxx          # activate after inspecting evidence in the ledger
devflow learn reject L-xxxxxxxx           # retire while preserving the record
devflow learn publish                     # open a context PR for human review
devflow stats
```

`learn publish` includes only `.devflow/context/repo-brief.md` and
`.devflow/learnings/ledger.jsonl`. It commits in a separate checkout, pushes a context branch,
and opens a PR without merging it. Runtime databases and transcripts remain local.
Lesson status changes currently affect local runs immediately; the PR distributes a reviewed
snapshot to other checkouts. Publishing does not itself approve candidate lessons.

## Running the queue

```bash
cd /path/to/initialised/repo
devflow run --dry-run              # preview Ready tickets; no model calls or GitHub writes
devflow run                        # one sweep, then exit
devflow run --watch 20 --max-tickets 10 --max-minutes 60 --max-cost-usd 5
```

The queue resumes existing tickets (including approvals and clarifications), then selects
labelled Ready issues by priority and dependencies. Waiting for a person does not block the next
Ready issue. Each sweep attempts a ticket once, so transient failures wait for the next sweep
instead of spinning. Watch mode polls until a limit, an interrupt, or STOP.

Create `.devflow/STOP` in the repo root to stop queue execution at the next stage boundary.
Remove it and rerun to resume from saved state. A stage already running can finish; STOP does
not cancel an in-flight model request, command, or merge. Time and recorded-cost ceilings have
the same boundary semantics and can overshoot by one stage. Costs exclude unmetered OpenClaw
calls and may use token estimates, so they are operational limits rather than billing guarantees.
Zero `--max-cost-usd` (the default) disables the cost ceiling. Defaults are 10 distinct tickets
and 60 minutes per invocation. The ownership lock lives in the common git directory, so another
`devflow run` or `devflow ticket` cannot concurrently mutate the same repo, even from a worktree.

This is an on-demand process for macOS/Linux. It has no automatically installed daemon; run it
in a terminal when wanted. Human plan and merge approvals still follow the repo configuration.
