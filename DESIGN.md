# devflow design

## Principle

Code owns state, gates and facts. Agents own judgment. A ticket moves through a state machine
in ordinary code; agents are steps inside it and return structured output (JSON against a
schema). Nothing advances because an agent says "done". It advances because a check the engine
ran itself passed.

## Pipeline

```
NEW > REFINING > AWAITING_PLAN_APPROVAL > TEST_DESIGN > IMPLEMENTING > GATES > IN_REVIEW
        ^   |            (human)                ^            ^  |        |
        |   v                                   |            |  v        v
 AWAITING_CLARIFICATION                         +---- CHANGES_REQUESTED  AWAITING_MERGE_APPROVAL
                                                                             (human) > MERGED
Any live state > ESCALATED (notify the human) or CANCELLED
```

`src/devflow/states.py` is the single source of truth for this table, including the retry caps
(review rounds, failed-gate attempts). A cap raises `LimitExceeded` and the engine escalates
instead of looping.

## Who does what

| Stage | Code (deterministic) | Agent (judgment) |
|---|---|---|
| Refine | fetch ticket and graph context, validate plan against `plan.schema.json` | planner: ambiguities, clarifying questions, plan |
| Test design | traceability check: every acceptance criterion maps to a test | test planner: unit and integration cases |
| Implement | worktree per ticket, launch `claude -p` / `codex exec`, keep the session id and resume it with feedback, timeouts | triage: retry, replan or escalate |
| Gates | run lint / typecheck / unit / integration / build from a clean checkout; check tests were not deleted, skipped or weakened | none |
| Review | separate checkout, run the integration command, parse findings against `review.schema.json` | reviewer: independent agent, ideally a different model from the coder |
| Triage | round counting and caps | decide which findings are blocking and what the coder is told |
| Merge | only if CI green, review approved, no blocking findings, and (initially) a human yes | none |

## Rules the engine enforces

- Stages talk through schemas, not free text.
- Tests are designed before code. A criterion with no test sends the plan back to refinement.
- "Done" is never trusted; gates re-run from a clean checkout.
- The reviewer is a separate session in a separate checkout and runs the integration tests
  itself.
- Human gates (plan approval, merge approval) are states. Turning one off auto-passes it with
  `actor="auto"`, so the audit trail is identical.
- Caps and stuck-detection escalate to a person with one message: what happened, options,
  recommendation.

## Per-repo init

`devflow init` makes the workflow repo-specific without code changes:

1. Detect projects, gate commands, CI hints and convention files (`detect.py`).
2. Build the code graph (`graphify update`, local AST only). Planner, test planner and reviewer
   prompts point at it for blast-radius questions.
3. Pick coder and reviewer agents (different models when both are installed).
4. Render `.devflow/config.toml`, prompts and schemas. Report what it could not detect.

Re-running is safe. Existing files are kept; `--force` regenerates them.

## Where OpenClaw fits

OpenClaw is the interface and the judgment layer, not the loop driver: it carries clarification
questions and escalations to you over iMessage, hosts the planner and triage agents, and calls
the engine as a tool. Its model calls can stall for minutes and its sessions can reject
overlapping messages, so a retrying deterministic loop is the safer place for control flow.

## Build order

1. `init`, `doctor`, state machine (done).
2. Gate runner and worktrees; run one ticket by hand.
3. Coder driver (Claude Code and Codex, session resume) and the feedback loop.
4. Reviewer driver, PR creation through `gh`, merge rules.
5. Notifier (OpenClaw / iMessage) and ticket intake. Then loosen human gates one at a time.
