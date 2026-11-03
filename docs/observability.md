# Workflow observability with local Langfuse

Devflow emits structured spans and operational events through a small OTLP/HTTP JSON exporter.
The included Docker Compose stack runs Langfuse locally. It uses Langfuse's open-source
observability features; no Langfuse Cloud account or Enterprise license is required.

## Start the stack

From a clone of the devflow source repository, run:

```bash
./scripts/observability up --capture-content /absolute/path/to/your/repo
```

This is a Docker-only setup: the script builds the small initializer image from
`infra/langfuse/Dockerfile`, generates private credentials inside that container, connects the
selected repositories and starts the included Compose stack. No host Python, API-key copying,
or separate Langfuse checkout is needed for this setup. Docker with Compose v2 must be running.
The script waits for the Langfuse health endpoint before reporting readiness.

Omit `--capture-content` for metadata-only tracing in a new repository. Omit the repository
path to connect this devflow checkout. To connect several repositories to the same local
project, supply several paths (quote paths containing spaces):

```bash
./scripts/observability up --capture-content "/path/first repo" /path/second-repo
# Connect another repository without restarting containers:
./scripts/observability connect --capture-content /path/another-repo
```

Each user gets unique generated credentials and local persistent Docker volumes. Repeated
setup reuses credentials and preserves the repository's existing content-capture setting.
The setup container runs as the invoking user and writes valid host-absolute trace-log paths.
Its build context includes only the setup program and Dockerfile, excluding generated secrets.
The container mounts the chosen repository and local credential directory only while setting up;
it is removed after setup. The Docker socket is not mounted inside any container.

The setup container and Langfuse services are bundled here; devflow itself, OpenClaw and the
coding-agent CLIs continue running on the host with their existing authentication. Their
normal installation and login prerequisites still apply, including each user's own model
subscription or API access. Windows users should run the wrapper in WSL2
with Docker Desktop integration; the supported shell environments are macOS and Linux.

The earlier manual startup remains supported when Python 3.11+ is available:

```bash
python3 infra/langfuse/setup.py --capture-content /absolute/path/to/your/repo
docker compose -f infra/langfuse/compose.yaml --env-file infra/langfuse/.env up -d --wait
```

Open [Langfuse](http://localhost:3300). Login details are in `infra/langfuse/login.json` locally.
The project is **Devflow Workflows**. The login file and `.env` are ignored by git and mode 0600;
do not commit them. Keep `.env` when restarting the stack because it contains the storage and
encryption credentials for its persistent volumes.

Web access is bound to `127.0.0.1:3300`. The object-store endpoint is bound to
`127.0.0.1:3900`. PostgreSQL, Redis and ClickHouse have no published host ports, so an existing
repo database on port 5432 can remain running. The stack uses named volumes and a separate
Compose project, `devflow-observability`.

## Verify connectivity

```bash
cd /absolute/path/to/your/repo
devflow telemetry status
devflow telemetry demo
```

The demo sends a clearly labelled synthetic trace; it launches no coding agents and changes
no ticket or code. It is excluded from the local workflow metrics. Its exported trace ID can
be searched in Langfuse.

Then use devflow normally:

```bash
devflow ticket 12 --watch 20
# or
devflow run --watch 20
```

Telemetry is loaded automatically from `.devflow/state/telemetry.json` in the selected repo.
An explicit `DEVFLOW_TELEMETRY_CONFIG` path can override that location. This file contains the
local collector endpoint, API keys and log location. Remove it or set `enabled` to `false` to
disable tracing for that repository.

## What is captured

| Span/event | What it tells you |
|---|---|
| `ticket.run` | One invocation/resume of a ticket, final state and done/paused/escalated result |
| `stage.*` | Stage duration, attempt number, review round, next state and waiting/error status |
| `agent.<driver>.<role>` | Named agent execution, handoff source/target, resume session, prompt hash, available token usage and reported cost |
| `provider.attempt` | Devflow's OpenClaw adapter attempt; gateway-internal retries are not individually visible |
| `schema.repair` | Number of validation errors that triggered another structured response |
| `routing.selected` | Selected coding agent and fallback index |
| `usage.recorded` | Recorded coding usage and whether the cost came from reported data or an estimate |
| `tool.shell`, `gate.result` | Gate/setup execution time, exit status, gate type, project and timeout outcome |
| `tool.git`, `tool.github`, `tool.github_project` | Git and host operation latency and exception type |
| `worktree.*` | Checkout preparation overhead |
| `graph.refresh` | Graph freshness check: `devflow.graph.refreshed` (true only when a rebuild succeeded), `.status`, `.nodes`, `.edges`, `.fingerprint` |
| `graph.stale` | Event emitted when the planner request starts and the graph is stale or unbuilt (before the engine refreshes it): `devflow.graph.status` (`stale`/`unbuilt`), `devflow.graph.fingerprint`, `devflow.graph.stored_fingerprint` (12-char prefixes, stored one empty when absent), `devflow.graph.age_seconds` (omitted when unbuilt). Also prints one `devflow: graph warning: ...` stderr line |
| `stall.detected` | Event emitted when a finished stage ran longer than the p99 of its own history: `devflow.stage`, `devflow.duration_seconds`, `devflow.threshold_p99_seconds`, `devflow.baseline_samples` |
| `stage.result` | Artifact success and review/blocking finding counts |
| `workflow.summary` | Overall recorded duration, human waiting time and review rounds when a retro runs |
| `learning.retro` | Retrospective execution time and failure status |

Each ticket invocation has a fresh trace; resumed turns share a session identifier of
`<repository>:issue-<number>`. Repository and issue metadata propagate to child spans. This lets
you inspect one attempt or group multiple attempts for the same ticket.

### Repository and graph provenance

The `ticket.run` root span (and later child spans, via `langfuse.trace.metadata.*`) carries
bounded, allowlisted metadata, each key also mirrored as `devflow.<key>`. It is emitted in both
metadata-only and content-capture modes:

- `base_sha`: the ticket's base commit (hex), set after the worktree is created, or from the
  stored ticket on resume.
- `remote`: sanitized `host/owner/repo`. Userinfo, port, query and fragment are removed; an
  unparseable remote is omitted.
- `graph_status`, `graph_fingerprint`, `graph_nodes`, `graph_edges`: the latest graph check for
  an agent request. The fingerprint is a 12-character hex prefix (empty outside a git repo);
  counts are integers and only present when known.

- `graph_staleness`: `fresh`, `stale` or `unbuilt`, checked once at the start of the planner
  request before the graph is refreshed. **fresh**: graph.json exists and its stored fingerprint
  matches the working tree. **stale**: graph.json exists but the stored fingerprint is missing or
  differs (HEAD moved or the tree changed). **unbuilt**: graph.json is missing. The engine still
  auto-refreshes the graph afterwards, so this means "stale when planning began"; the post-refresh
  outcome is in `graph_status`.

Graph states: **fresh** (no-op check, existing graph is current), **refreshed** (a rebuild
succeeded), **failed** (the builder ran but failed; counts are 0, never shown as fresh) and
**unavailable** (graphify missing, not a git repo, or the builder raised). A graph problem never
stops a ticket; the agent prompt just says the code graph is unavailable.

Values are validated (hex, fixed enum, non-negative integers) and invalid values are dropped.
Graph contents, digests, prompts, issue text, file paths, error messages and credentials are
never added to this metadata. The fingerprint changes with the working tree, so use it to
compare traces, not as a grouping key or session id.

Agent invocations are typed as agents, stages as chains, and external actions as tools.
This avoids representing a multi-turn coding session as one model generation. Token usage and
costs are whatever the CLI reports or the existing router estimates; OpenClaw usage may remain
unmetered. Do not interpret CLI invocation counts as provider request counts, or recorded cost
as a billing total. Reported aggregate usage on an agent is diagnostic; per-model Langfuse
analytics require the individual generation data actually exposed by that CLI.

## Explore the workflow

Select **Devflow Workflows** in Langfuse and inspect traces or group by session. Useful starting
questions include:

- Which stages dominate elapsed execution time?
- Which agent calls are slow, failing, or hitting structured-output repairs?
- How often does a ticket need another implementation or review round?
- Which gate types fail or time out most often?
- Does switching coding tools reduce retries or increase recorded cost?
- How much recorded ticket time is spent waiting for human decisions?

For a quick local aggregate:

```bash
devflow telemetry metrics
devflow telemetry metrics --issue 12
devflow stats
```

`telemetry metrics` summarizes the local span log: per-stage latency (`count`, `mean`, `max`, `p50`, `p90`, `p99`, linear interpolation), `stage_stalls` (count of `stall.detected` events per stage), CLI call counts,
recorded coding cost and ticket-turn outcomes. It excludes synthetic demo spans. Malformed, non-object or incomplete trace lines are ignored
for the aggregates and are not counted in `spans`. `stats` reads
the durable workflow database and summarizes tickets, reviews and lessons. Neither command
requires a model call.

`telemetry metrics --issue N` reports one ticket's latency from the sessions named
`<repo>:issue-N` (exact issue number; `issue-1` never matches `issue-12`). Each session gets one
summary combining all resumed invocations: `runs` and `outcomes` from `ticket.run` spans,
`active_run_seconds` (observed span time, overlapping runs merged), `elapsed_wall_seconds`
(observed, first run start to last run end) and `inferred_gap_seconds` (inferred: elapsed minus
active, never negative; human wait time outside the recorded runs is not observable). It also
reports stage duration count/mean/max/p50/p90/p99, agent duration count/mean/max, `stage_stalls` and agent call counts. Output contains only
counts, durations, outcome names and span names, never prompts, outputs, hashes or agent session
ids. Synthetic spans are excluded and counted. With no spans for the issue, zeroed output is
printed.

**Stall detection.** After each stage finishes, the engine compares its wall time with the p99 of
that stage's earlier `stage.*` spans in the local log. It reports a stall only when at least 5
earlier samples exist and the duration is strictly greater than their p99. Only non-synthetic
spans with status `ok` form the baseline, so `waiting` (human pause) and `error` spans are
ignored. A stall emits a `stall.detected` event (never named `stage.*`, so it is not counted as
a latency) and prints one `devflow: stall warning: ...` line to stderr. The check runs after the
stage completes, not while it runs, and never affects ticket state, stage timeouts or model
calls. It is skipped when telemetry is disabled or no log file is configured. With few samples
p99 is close to the maximum, so early detection is intentionally rare.

## Logs and export failures

Completed spans are appended to `.devflow/state/traces.jsonl`, then exported in batches to
Langfuse. The structured log is an append-only record, separate from the workflow database.
When export fails, the workflow continues and the local records remain available:

```bash
devflow telemetry replay --limit 1000
```

Replay sends the most recent records with their original trace/span IDs. Export uses the
Langfuse v4 ingestion header and treats OTLP partial rejections as failed exports. A local
log is a recovery source, not an automatic unlimited retry queue. The current setup does not
configure automatic log rotation or remote backups.

## Detailed prompts, responses and tool capture

Metadata-only capture remains the default. To explicitly enable content for selected repos:

```bash
python3 infra/langfuse/setup.py --capture-content /absolute/path/to/your/repo
devflow telemetry status /absolute/path/to/your/repo
```

Setup preserves an existing content setting when rerun. To disable content while retaining
operational tracing, set `"capture_content": false` in the repo's ignored
`.devflow/state/telemetry.json`. New CLI processes load the setting automatically; restart an
already-running `devflow run --watch` process to reload it. Existing records are unaffected.

With content enabled, observations contain:

- The supplied role/system instructions, task prompt, Graphify digest, repository brief,
  selected lessons and schema that actually went into each agent request.
- Final assistant responses, resume session IDs, sanitized errors, stage artifacts, plans,
  test designs, review findings, remediation instructions, schema validation errors, state
  transition reasons and routing preferences/selection.
- Gate/setup commands and output, timeout and exit status, Git arguments/results, GitHub and
  Project request arguments/body and responses. Environment dictionaries are never recorded.
- Claude Code exposed assistant messages, correlated tool inputs/results, tool-based subagent
  dispatches and reported per-message model/token data.
- Codex exposed command executions/output, MCP calls/results, file changes, web searches,
  collaboration dispatches and assistant messages.
- Cursor stream tool inputs/results and complete assistant messages.

The named agent observation itself represents the workflow handoff: it records the parent
stage, destination agent/role, delivered context and returned result. There is no duplicate
sibling dispatch node for that same execution. A full trace per invocation and stable ticket
session tie planning, coding, review and subsequent resumes together.

**Visibility limits:** CLI events are parsed after the subprocess completes. Internal event
observations are explicitly marked `devflow.reconstructed=true` and
`devflow.timing_precision=post_process`; use stage/agent spans for measured latency and the
internal event index for stream ordering. Internal provider prompts, hidden CLI system
instructions, compaction/history and gateway-internal retries are not exposed by these drivers.
Claude generations explicitly label that missing request context; their visible output/model
and reported usage are captured without pretending the entire provider request is available.
Codex/Cursor messages are events rather than invented provider generations. OpenClaw exposes
the delivered prompt, final payload, public run/session identifiers and available usage; its
private internal tools and nested sessions are not harvested. A tool-based child dispatch can
be captured without access to the child's internal execution. No hidden reasoning is collected.

Content is redacted **before** both JSONL persistence and OTLP export: structured credential
fields, known secret environment/config values, recognizable API tokens, authorization values,
private keys and URL passwords are masked. Free-form personal/confidential information is not
guaranteed masked. This is detailed local debugging data; the ignored logs and Langfuse Docker
volumes contain source snippets and task content. Collector credentials are not passed to gates.

Each input/output payload defaults to 200,000 characters. Set `max_content_characters` in the
repo telemetry config to adjust it (supported range 1,000–1,000,000). Oversized payloads have
an explicit truncated preview and original character count; operational attributes retain their
smaller limit. Content can therefore be incomplete even when capture is enabled. Logs remain
mode 0600 and excluded from git. Trace replay resends already-recorded content, so switching
content off does not erase historical logs or remove content from the local Langfuse database.

The existing dependency-free OTLP transport is retained rather than adding a second SDK tracer:
it already preserves devflow's trace IDs, local recovery logs and optional/offline operation.
Attribute mapping and observation types follow the current official Langfuse documentation.
This application invokes external CLI agents rather than an in-process LLM SDK, so an OpenAI or
LangChain auto-instrumentation wrapper would not reveal those subprocesses' internal requests.

## Operate and restart

```bash
# From the devflow checkout:
./scripts/observability status
./scripts/observability logs
./scripts/observability stop
./scripts/observability up
```

Stopping containers preserves data. Removing volumes deletes stored traces and account data.
Image tags for the web/worker follow Langfuse major version 4; review upstream migration notes
before intentionally pulling newer images. The local Compose stack is for development and
single-machine use, without high availability.

## Upstream references

- [Langfuse Docker Compose deployment](https://langfuse.com/self-hosting/deployment/docker-compose)
- [Headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization)
- [Tracing best practices](https://langfuse.com/docs/observability/best-practices)
- [Official Langfuse AI skill](https://github.com/langfuse/skills)
- [OTLP endpoint and attribute mapping](https://langfuse.com/integrations/native/opentelemetry)

The local Compose configuration is based on the upstream deployment, with private generated
credentials, different host ports and no exposure of internal databases. Enterprise-only
Langfuse features are not configured.

## Trace-completeness gate

`devflow.tracecheck.check_trace_completeness(rows)` validates exported span records offline.
For each engine invocation (one `ticket.run` root per `trace_id`) it checks that every span
reaches the root through existing parents (no orphans, dangling parents or cycles) and that all
records share one `langfuse.session.id`. Across the ticket it requires the `stage.refining`
(plan), `stage.test_design`, `stage.implementing`, `stage.gates` and `stage.in_review` spans as
direct children of the root with a matching `devflow.state`, and `agent.*`, `gate.result` and
`state.transition` spans attached under stage spans. The required set is the `REQUIRED_STAGES`
constant.

The trace-completeness gate runs against the real fake-agent lifecycle in `tests/test_telemetry.py` and is part of
the normal test suite and CI. Run it locally with no network or Langfuse:

```bash
python -m pytest tests/test_telemetry.py -k trace_completeness
```

A violation reads like `slice 2: required stage span 'stage.gates' (gates) missing` or
`slice 1: span 'stage.implementing' (id=ab12) parent 'zz99' not found`; the test failure lists all of them.
