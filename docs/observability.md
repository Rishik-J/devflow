# Workflow observability with local Langfuse

Devflow emits structured spans and operational events through a small OTLP/HTTP JSON exporter.
The included Docker Compose stack runs Langfuse locally. It uses Langfuse's open-source
observability features; no Langfuse Cloud account or Enterprise license is required.

## Start the stack

From the devflow source checkout:

```bash
python3 infra/langfuse/setup.py /absolute/path/to/your/repo
docker compose -f infra/langfuse/compose.yaml --env-file infra/langfuse/.env up -d
```

The setup script generates random credentials once, creates a local organization and project
through Langfuse's documented headless initialization, and enables telemetry for each repository
path supplied. You can supply multiple repo paths. Run it again to connect another repository.

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
| `agent.call` | Agent tool and role, resume flag, character counts, available token usage and reported cost |
| `provider.attempt` | Devflow's OpenClaw adapter attempt; gateway-internal retries are not individually visible |
| `schema.repair` | Number of validation errors that triggered another structured response |
| `routing.selected` | Selected coding agent and fallback index |
| `usage.recorded` | Recorded coding usage and whether the cost came from reported data or an estimate |
| `tool.shell`, `gate.result` | Gate/setup execution time, exit status, gate type, project and timeout outcome |
| `tool.git`, `tool.github`, `tool.github_project` | Git and host operation latency and exception type |
| `worktree.*`, `graph.refresh` | Checkout preparation and graph freshness overhead |
| `stage.result` | Artifact success and review/blocking finding counts |
| `workflow.summary` | Overall recorded duration, human waiting time and review rounds when a retro runs |
| `learning.retro` | Retrospective execution time and failure status |

Each ticket invocation has a fresh trace; resumed turns share a session identifier of
`<repository>:issue-<number>`. Repository and issue metadata propagate to child spans. This lets
you inspect one attempt or group multiple attempts for the same ticket.

The spans cover the devflow orchestration boundary. A CLI may make several internal model or
tool calls; they are not individual spans unless the CLI exposes them through a future
integration. Token usage and costs are whatever the CLI reports or the existing router
estimates. OpenClaw usage may remain unmetered. Do not interpret CLI invocation counts as
provider request counts, or the recorded cost as a billing total.

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
devflow stats
```

`telemetry metrics` summarizes the local span log: stage mean/max latency, CLI call counts,
recorded coding cost and ticket-turn outcomes. It excludes synthetic demo spans. `stats` reads
the durable workflow database and summarizes tickets, reviews and lessons. Neither command
requires a model call.

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

Prompts, model response text, shell command text/output, environment values and exception
messages are not exported. Exceptions record their type. Raw code and model content are not
required for this operational tracing. Credentials for the collector are not passed into
gate subprocesses. Operational attributes still include repo names, issue numbers, roles and
agent names.

## Operate and restart

```bash
# From the devflow checkout:
docker compose -f infra/langfuse/compose.yaml --env-file infra/langfuse/.env ps
docker compose -f infra/langfuse/compose.yaml --env-file infra/langfuse/.env stop
docker compose -f infra/langfuse/compose.yaml --env-file infra/langfuse/.env up -d
```

Stopping containers preserves data. Removing volumes deletes stored traces and account data.
Image tags for the web/worker follow Langfuse major version 4; review upstream migration notes
before intentionally pulling newer images. The local Compose stack is for development and
single-machine use, without high availability.

## Upstream references

- [Langfuse Docker Compose deployment](https://langfuse.com/self-hosting/deployment/docker-compose)
- [Headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization)
- [OTLP endpoint and attribute mapping](https://langfuse.com/integrations/native/opentelemetry)

The local Compose configuration is based on the upstream deployment, with private generated
credentials, different host ports and no exposure of internal databases. Enterprise-only
Langfuse features are not configured.
