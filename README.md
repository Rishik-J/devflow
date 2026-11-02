<div align="center">

# devflow

**From GitHub issue to reviewed pull request.**

Repository-aware engineering workflows, orchestrated with OpenClaw and executed by the coding agents you already use.

[![CI](https://github.com/Rishik-J/devflow/actions/workflows/ci.yml/badge.svg)](https://github.com/Rishik-J/devflow/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[Get started](#quick-start) · [Setup guide](docs/getting-started.md) · [Architecture](DESIGN.md) · [Contributing](CONTRIBUTING.md)

</div>

---

Devflow turns a GitHub Project into an engineering queue. OpenClaw refines requirements, asks clarifying questions and decides how to handle stalled work. Claude Code, Codex or Cursor implement changes in isolated git worktrees. An independent reviewer checks the result, while the engine runs tests, records state and controls GitHub writes.

Plans and merges require your approval by default. After a run, devflow captures evidence-backed lessons and makes relevant context available to later agents.

**Status:** early-stage software, version 0.1.0. The single-ticket workflow has been exercised on a real repository; queue execution, stop conditions and failure paths have automated integration coverage. Start with small tickets and review the generated plans and diffs.

## Why devflow

- **A repeatable ticket loop.** Refine → plan approval → test design → implement → verify → review → merge approval.
- **Context from the repository.** Detect projects, test commands and conventions during initialization. Graphify provides a local code graph, changed-file relationships and agent context.
- **Multiple coding agents.** Use Claude Code, Codex and Cursor through their terminal CLIs, with session resume and configurable usage-based routing.
- **Independent verification.** Run gates in a separate checkout, compare failures against the base commit, and return review findings to the coder.
- **Durable progress.** SQLite records transitions, artifacts and sessions so interrupted tickets can resume.
- **Bounded execution.** Review caps, run limits, exclusive repo ownership and a STOP file keep the loop manageable.
- **Repository learning.** Keep lessons with ticket evidence, select them by role and file scope, and publish context updates through pull requests.

## How it works

```mermaid
flowchart LR
    A[Ready issue] --> B[OpenClaw: refine plan]
    B --> C{Your approval}
    C --> D[Test design and implementation]
    D --> E[Engine: gates in clean checkout]
    E --> F[Independent review]
    F -->|Changes requested| D
    F -->|Approved| G{Your merge approval}
    G --> H[Merge reviewed commit]
    H --> I[Run record and lessons]
```

The engine owns state transitions, tests, commits, pushes and merges. Agents supply judgment through validated responses. The merge is pinned to the commit that was reviewed; an unexpected branch update blocks it.

## Requirements

| Component | Purpose |
|---|---|
| Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/) | Install and run devflow |
| Git | Isolated worktrees and branches |
| [GitHub CLI](https://cli.github.com/) | Issues, Projects, PRs and CI status |
| [OpenClaw](https://github.com/openclaw/openclaw) | Planning, triage and retrospective agents |
| [Graphify](https://github.com/Graphify-Labs/graphify) | Local code graph and graph queries |
| At least one coding-agent CLI | Claude Code (`claude`), Codex (`codex`) or Cursor (`agent`) |

macOS and Linux are supported targets. Install and authenticate your selected agent CLIs before running tickets. Two coding tools are recommended so the reviewer can use a different tool from the coder. Devflow does not include model subscriptions or API credits.

## Quick start

### 1. Install

```bash
uv tool install "git+https://github.com/Rishik-J/devflow.git"
uv tool install graphifyy
```

Devflow is installed from this repository; there is no official PyPI release documented here.

### 2. Connect GitHub and OpenClaw

```bash
gh auth login
gh auth refresh -h github.com -s project

# Start your configured OpenClaw gateway if it is not already running.
openclaw gateway
```

Run the gateway in a separate terminal, or use your existing OpenClaw service. In another terminal:

```bash
# Choose a provider/model supported by your OpenClaw installation.
openclaw models list
devflow setup-openclaw --model YOUR_PROVIDER/YOUR_MODEL
devflow doctor
```

`setup-openclaw` creates the dedicated `devflow` agent once. If it already exists, the command leaves it in place; change its model through OpenClaw when needed. Model credentials remain in the underlying tools.

### 3. Initialize a target repository

```bash
cd /path/to/your/repo
devflow init
```

Your target must be a git repository with a GitHub remote and an initial commit for ticket execution. Review `.devflow/config.toml`, especially the environment setup and test commands, before your first run.

### 4. Create the Project queue

```bash
devflow board-create --title "Engineering queue"
gh label create devflow --color 0e8a16 --description "Worked by devflow" --force
```

Create a small issue, give it the `devflow` label, add it to the Project and set its Status to **Ready**. The generated Project has Backlog, Ready, In progress, In review and Done, plus P0–P3 priorities.

### 5. Preview and run

```bash
devflow board
devflow run --dry-run

devflow run --watch 20 --max-tickets 3 --max-minutes 60 --max-cost-usd 5
```

The queue resumes existing tickets and processes eligible Ready issues. It pauses at human gates, continues with other eligible work, and polls for your replies in watch mode.

For a single issue:

```bash
devflow ticket 12 --watch 20
```

## Human approvals

Reply in GitHub with one of these comments. Only configured approvers count; by default, that is the authenticated `gh` user.

| Comment | Where | Effect |
|---|---|---|
| `/approve` | Issue | Accept the proposed plan, or clarification defaults |
| `/answer …` | Issue | Answer a clarification |
| `/reject …` | Issue | Request a revised plan |
| `/approve` | PR | Authorize merge after review and CI checks |
| `/resume` | Issue | Resume an escalated ticket |
| `/learn gotcha: …` | Issue or PR | Record a human-confirmed lesson |

An approval is consumed once. An issue-level plan approval does not authorize the later merge.

## Configure each repository

`devflow init` generates the following files:

```text
.devflow/
├── config.toml             # Agents, gates, limits and Project settings
├── context/repo-brief.md    # Shared repository context
├── prompts/                # Role instructions tuned to the repository
└── schemas/                # Structured plan, test, review and lesson contracts
```

It detects Python, Node.js, Rust and Go projects, including monorepos. Detection is a starting point: fill in missing integration commands and verify setup commands. Existing files are preserved; `init --force` replaces generated files, including customized configuration.

Commit the shareable `.devflow` files. Graph caches and `.devflow/state/` remain local through git excludes installed by initialization.

Important settings include:

```toml
[human_gates]
plan_approval = true
merge_approval = true
approvers = ["your-github-login"]

[review]
require_integration = true

[learning]
enabled = true
promote_after = 2
budget_chars = 4000
```

These are excerpts to edit in the generated file, not extra tables to append. Setting `require_integration = true` blocks review until at least one integration gate is configured. The default is `false`.

See the [setup guide](docs/getting-started.md) for gate configuration, an existing Project, routing and troubleshooting.

## Code graph and learning

Graphify builds a graph on first use in each checkout. The engine checks freshness before agent requests and rebuilds when the source fingerprint changes. AST graph updates run locally without an LLM; the agent providers still receive the context used for model calls.

```bash
devflow graph status
devflow graph refresh
devflow graph context --files src/example.py

devflow learn list
devflow learn retro 12
devflow learn approve L-xxxxxxxx
devflow learn reject L-xxxxxxxx
devflow learn publish
```

Agent lessons begin as candidates. They become active after independent evidence from two tickets, or explicit human approval. Active lessons are selected by role and file scope for later prompts. Publishing opens a context-update PR containing only the repo brief and lesson ledger.

Context PRs distribute a reviewable snapshot; local lesson approval affects local runs immediately. Graphify MCP tools are injected for Claude and Codex. Cursor receives the context digest, and OpenClaw receives the checkout path and graph-query instructions.

## Execution controls

```bash
devflow tickets
devflow agents
devflow stats

# From the target repository:
touch .devflow/STOP
# Remove the file when ready to resume, then run devflow again.
```

STOP and run limits are checked between stages. A running stage can finish, so time and cost ceilings can overshoot by a stage. Cost tracking uses reported cost or token estimates and excludes unmetered OpenClaw calls; it is not a billing guarantee. `--max-cost-usd 0`, the default, disables that ceiling.

The default run limits are ten distinct tickets and sixty minutes. A lock shared by a repository's worktrees prevents concurrent queue and single-ticket execution. No daemon is installed automatically.

`devflow run --dry-run` previews queue selection without model calls. `devflow ticket N --dry-run` is different: it can call agents and create temporary local worktrees, but logs GitHub writes instead of performing them.

## Local traces and workflow metrics

Run Langfuse on Docker to inspect ticket timelines, stage latency, agent invocations, retries,
gate results, review loops and recorded usage:

```bash
# From this source checkout:
python3 infra/langfuse/setup.py /absolute/path/to/your/repo
docker compose -f infra/langfuse/compose.yaml --env-file infra/langfuse/.env up -d

cd /absolute/path/to/your/repo
devflow telemetry demo
devflow telemetry metrics
```

Open [localhost:3300](http://localhost:3300); local credentials are in
`infra/langfuse/login.json` in the devflow checkout. Future runs in connected repositories emit
nested spans automatically and keep a local JSONL log if export fails. Add `--capture-content`
to setup to record redacted prompts, responses, tool payloads and review artifacts. Content
capture is opt-in per repository. See the [observability guide](docs/observability.md)
for setup, replay, scope and cost-accounting details.

## Development

```bash
git clone https://github.com/Rishik-J/devflow.git
cd devflow
uv tool install --editable .

uv run --no-project --with ruff ruff check .
uv run --no-project --with pytest --with-editable . python -m pytest -q
```

Most tests use temporary git repositories and fake agents. Live tests are opt-in, require authenticated CLIs and incur model usage:

```bash
DEVFLOW_LIVE=1 uv run --no-project --with pytest --with-editable . \
  python -m pytest tests/test_live_agents.py -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and [DESIGN.md](DESIGN.md) for the architecture and original implementation plan.

## License

[MIT](LICENSE) © 2026 Rishik J.
