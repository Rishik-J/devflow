# Setup and operating guide

This guide takes an installed devflow CLI from its first repository to routine queue execution.
For installation and prerequisites, start with the [README](../README.md#quick-start).

## 1. Prepare the tools

Authenticate GitHub using `gh auth login`, then grant Project access with:

```bash
gh auth refresh -h github.com -s project
```

Install and authenticate at least one supported coding CLI: `claude`, `codex`, or Cursor's
`agent`. Set up OpenClaw with a working provider/model and start its gateway. Check models with
`openclaw models list`; create the dedicated agent with:

```bash
devflow setup-openclaw --model YOUR_PROVIDER/YOUR_MODEL
devflow doctor
```

Model identifiers depend on the provider configured in OpenClaw. `setup-openclaw` has a
built-in default, but supplying an identifier from your own model list is more portable.
The command does not update an agent that already exists.

## 2. Initialize the target repository

```bash
cd /path/to/your/repo
git status
devflow init
```

Use a repository with an initial commit and an `origin` GitHub remote. Initialization detects
projects and proposes commands; it does not guarantee those commands pass. Inspect
`.devflow/config.toml` and `.devflow/context/repo-brief.md`. Add the project-specific facts the
brief should teach every agent: important boundaries, local services and deployment assumptions.

For example, edit the generated project entry to configure unit and integration gates:

```toml
[[project]]
path = "."
kind = "python"
setup = ["uv sync --all-extras"]

[project.gates]
lint = ["uv run ruff check ."]
unit = ["uv run pytest tests/unit"]
integration = ["uv run pytest tests/integration"]
```

This is an example for a uv-managed Python project. Replace paths and commands with the ones
your repo actually uses. Node.js, Rust, Go and monorepo projects can have their own entries.
Gates execute code in the checkout, including configured dependency-install commands.
Integration tests that need services or test credentials need a repo-specific setup.

Baseline comparison distinguishes existing failures from failures introduced by the ticket.
It does not repair an already failing base. Review the diagnostics before relying on a baseline.

Run `devflow init` again to add missing generated files while preserving existing files.
`devflow init --force` overwrites generated settings and prompts; save customizations first.

## 3. Connect the Project

Create a Project and write its config automatically:

```bash
devflow board-create --title "Engineering queue"
gh label create devflow --color 0e8a16 --description "Worked by devflow" --force
```

For an existing Project, edit the generated file:

```toml
[github]
project = 6
owner = "your-github-login"
label = "devflow"
max_active = 1
followups_per_ticket = 3
```

Use your own Project number and owner. The default Status names are **Backlog**, **Ready**,
**In progress**, **In review** and **Done**. Map different names through `[github.statuses]`:

```toml
[github.statuses]
backlog = "Backlog"
ready = "Todo"
in_progress = "In Progress"
in_review = "Review"
done = "Done"
```

The scheduler reads P0–P3 from the Priority field, then uses issue number as the age tie-breaker.
In an issue body, `Blocked by #12` or `Depends on #12` defers that issue until #12 is closed.
The queue considers labelled Ready issues in the configured repository, not every issue on a
multi-repository Project. New follow-ups are filed into Backlog.

## 4. Run the first ticket

Choose a small, clearly bounded issue. Add it to the Project, apply the `devflow` label and move
it to Ready. Preview selection, then run one ticket:

```bash
devflow board
devflow run --dry-run
devflow ticket 12 --watch 20
```

Read the plan in the issue. Comment `/approve`, `/reject <reason>` or `/answer <clarification>`.
The implementation and review cycle resumes when devflow reads your command. Review the PR
and its CI checks, then comment `/approve` on the PR to permit merging.

The engine pins merging to the reviewed commit. If the branch changes after review, it
escalates. A rejected plan or escalated run remains in the local database, and `/resume`
continues the ticket after the underlying problem has been addressed.

## 5. Work the queue

```bash
devflow run --watch 20 --max-tickets 10 --max-minutes 60 --max-cost-usd 5
```

Without `--watch`, devflow performs a single sweep and exits. With it, the runner polls approvals
and Ready work. Tickets waiting for a person do not consume the active-work slot. The current
runner executes work sequentially; increasing `max_active` does not start parallel worker processes.

Time, cost and STOP checks happen between stages. To pause the queue:

```bash
touch .devflow/STOP
```

Delete the STOP file when ready, then run the same command again. Ctrl-C interrupts the runner;
saved stages can be resumed, but inspect any in-progress tool action before retrying.

## 6. Tune agent routing

The generated config lists installed coding tools. For example:

```toml
[routing]
roles = ["coder", "reviewer", "test_planner"]
strategy = "headroom"
cooldown_minutes = 30
cooldown_max_hours = 6

[routing.agents.claude]
priority = 1
window_hours = 5
limit_usd = 10

[routing.agents.codex]
priority = 2
window_hours = 5
limit_usd = 10
```

Edit the existing tables. `headroom` prefers the tool with the most local budget remaining;
`priority` prefers the configured ordering. Zero caps disable local budget ceilings. Rate-limit
responses trigger cooldowns and fallback to another available coding tool. A tool switch starts
a new session because CLI sessions are not portable.

The reviewer prefers a different tool from the coder when available. This is tool-level
separation, not a guarantee that different tools use different underlying models.

## 7. Review lessons and share context

```bash
devflow learn list
devflow learn approve L-xxxxxxxx
devflow learn reject L-xxxxxxxx
devflow learn publish
devflow stats
```

Read `.devflow/learnings/ledger.jsonl` for scope, source tickets and evidence. Candidates do not
reach normal agent prompts until promoted. Human `/learn` statements and explicit approvals
activate lessons locally. Publishing opens a reviewable PR; it does not merge it or approve
candidate lessons automatically.

To retry a retro after a provider error, or capture a finished ticket from before learning was enabled:

```bash
devflow learn retro 12
# Optional: use another configured OpenClaw model for this run only.
devflow learn retro 12 --model YOUR_PROVIDER/YOUR_MODEL
```

## Troubleshooting

| Symptom | Check |
|---|---|
| No eligible Ready tickets | Repository, label, Project status, dependencies and existing local ticket state |
| Project permission error | Run `gh auth refresh -h github.com -s project` |
| OpenClaw unavailable | Check `openclaw gateway status` and the provider's authentication or quota |
| All coding tools unavailable | Run `devflow agents`; inspect authentication, budgets and cooldowns |
| Review requires integration | Add an actual integration command or explicitly change `[review] require_integration` |
| Another runner owns the repo | Stop the other process or wait; an OS lock releases when its process exits |
| STOP prevents progress | Remove `.devflow/STOP` when ready to resume |
| Branch moved after review | Inspect the new commit and obtain a fresh review before merging |
| Lessons are not appearing in prompts | Check status, role, file scope and `[learning] enabled` |

Use `devflow --help`, `devflow tickets`, `devflow agents`, and `devflow stats` to inspect state.
Do not delete the local database as a routine retry strategy: it contains session and approval history.


## Optional: start local workflow tracing

The devflow checkout includes a complete Docker-based Langfuse setup:

```bash
# Run from your devflow source checkout, passing your target repository:
./scripts/observability up --capture-content /absolute/path/to/your/repo
```

This generates unique local credentials, connects the target repo and waits for Langfuse at
[localhost:3300](http://localhost:3300). Your normal devflow runs then emit detailed traces.
Use `./scripts/observability status`, `logs`, or `stop` to operate the stack. See the
[observability guide](observability.md) for privacy controls and capture limits.
