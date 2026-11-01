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
| Agent drivers, stages, engine, GitHub Projects, learning loop, `devflow run` | not built yet (see plan) |

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

## Develop

```bash
cd /Users/rjdev/Desktop/devflow
uv run --no-project --with pytest --with-editable . python -m pytest -q
uv run --no-project --with ruff ruff check .
```

Per-repo behavior lives in templates (`src/devflow/templates`) and schemas
(`src/devflow/schemas`), so improving a prompt improves every repo the next time it is
initialized with `--force`.
