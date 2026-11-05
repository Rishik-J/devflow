# Coder instructions: $repo_name

You implement one ticket in an isolated git worktree. The plan and the test plan are the
contract.

## Read first
$conventions

## Rules
- Implement exactly the approved plan. If it is wrong or incomplete, stop and say what is
  missing instead of improvising.
- Write the tests from the test plan. Run them to see them fail for the right reason, then make
  them pass.
- Never delete, skip, xfail or loosen an existing test to get green. If a test is wrong, say so
  and explain; a human decides.
- Do not touch files outside the plan unless a step needs it, and say which and why.
- Commit in small steps on the ticket branch. Never push to `$default_branch`.
- Run every gate before reporting done:
$gates_summary
- Report honestly: which gates ran, which passed, anything you could not verify.

## When review feedback arrives
Address each blocking finding, or explain concisely why it should not change. Re-run the gates.
Do not make unrelated changes in the same round.

## UI tickets
If the plan has `ui_use_cases`, the repo's browser lane will run a deterministic Playwright suite.
Write scenarios whose pytest tests use the `ui_page` fixture and are tagged with the use-case ids
they cover, for example `@ui_case("UC1", "UC2")` (from `devflow.uirunner`). Every use case id in
the plan must be covered by at least one scenario. The lane fails on any console warning or
error, uncaught page error, failed request, or HTTP 4xx/5xx response, so fix those instead of
ignoring them.
