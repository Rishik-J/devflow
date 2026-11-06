# The UI lane

devflow splits verification into two lanes. The **backend lane** is the existing set of gates
(`lint`, `unit`, `integration`). The **UI lane** runs a deterministic headless-browser suite when
a diff touches user-facing files. No LLM drives the browser. After the suite passes, an LLM
*evaluator* maps its output to the use cases the plan promised, and the result becomes a
**Feature Use-Case Verification** table in the gate evidence, the review prompt and the PR
review comment.

Without a `[ui]` section in `.devflow/config.toml` nothing changes: no lane, no extra agent calls,
no new payload keys.

## Configure it

```toml
[ui]
command = "DEVFLOW_UI_LANE=1 python -m pytest tests/ui -p devflow.uirunner"
# paths = ["web/**", "*.html"]   # optional; replaces the defaults
# timeout_minutes = 10           # optional; default 10
# enabled = true                 # defaults to true when `command` is set
```

- `command` runs in the ticket worktree with the project environment (secrets scrubbed).
- `paths` are globs matched against changed files. Without a `/` a pattern matches the file
  name at any depth. `**` crosses directories. The defaults are `src/devflow/ui.py`, `site/**`,
  `**/static/**` and `*.html|*.css|*.js|*.jsx|*.ts|*.tsx|*.vue|*.svelte`.
- `timeout_minutes` kills the whole process group when exceeded, and the lane fails.
- Invalid values raise a `ConfigError` that starts with `[ui]`.
- `enabled = true` with no `command`: UI diffs are recorded as `ui:browser lane` under
  "not configured" (like a missing gate) and do not fail the ticket.

Install Playwright and a browser through the project's `setup` commands, for example
`pip install playwright && playwright install chromium`. Playwright is not a dependency of devflow
and the default `python -m pytest` never needs it.

## What the lane checks

The lane fails, and the ticket goes back to IMPLEMENTING with the reasons, when:

- the command exits non-zero or times out;
- it writes no report, or an invalid one;
- any case fails;
- any `ui_use_cases` id in the plan is covered by no case;
- Playwright or the browser is missing (`UI lane unavailable: ...`). It never passes silently.

UI lane failures are strict and are not compared with the base commit.

### Environment passed to the command

| Variable | Meaning |
| --- | --- |
| `DEVFLOW_UI_REPORT` | Path of the JSON report to write (outside the worktree) |
| `DEVFLOW_UI_ARTIFACTS` | Directory for screenshots and DOM snapshots (outside the worktree) |
| `DEVFLOW_UI_PORT` | A free local port, if your server needs one |

### Report contract

```json
{"cases": [{"id": "tests/ui/test_x.py::test_y", "use_case_ids": ["UC1"], "ok": true,
            "duration_s": 0.4, "errors": [], "console": [{"type": "error", "text": "..."}],
            "page_errors": [], "network_failures": [{"url": "...", "status": 500}],
            "screenshot": "/tmp/.../a.png", "dom": "/tmp/.../a.html"}],
 "unavailable": "optional reason the browser could not run"}
```

`devflow.uilane.load_report` parses it strictly.

## Writing scenarios

`devflow.uirunner` is a pytest plugin (`-p devflow.uirunner`). Ask for the `ui_page` fixture and
tag each scenario with the plan use-case ids it covers:

```python
from devflow.uirunner import ui_case

@ui_case("UC1", "UC2")
def test_board(ui_page, my_server):
    ui_page.goto(my_server)
    ui_page.locator(".card").wait_for()
```

The runner records console **warnings and errors**, uncaught page errors, failed requests and
responses with status >= 400. Any of them fails the case at teardown even when the test's own
assertions passed, and the message names the item. Requests the page cancelled itself
(`ERR_ABORTED`, e.g. on reload) are ignored. A scenario that deliberately provokes an error
response declares it with the `ui_collector` fixture:
`ui_collector.expect_http_error("/api/ticket/999", 404)`.

Use ephemeral ports, seeded data and explicit locators; do not sleep. The repo's own scenarios are
in `tests/ui/` and run with `DEVFLOW_UI_LANE=1 python -m pytest tests/ui -p devflow.uirunner`.
`tests/ui` is excluded from default collection unless `DEVFLOW_UI_LANE=1` is set.

## Planning and review

When `[ui]` is enabled and a plan touches user-facing files, the plan must include 3 to 6
`ui_use_cases` (`id`, `title`, `kind`, `description`, `criterion_ids`). Otherwise it is sent back
to REFINING with the reasons. In review, after the lane passes, the `ui_evaluator` role is called
once with the use-case matrix, the acceptance criteria and a bounded digest of the report. It
has no browser. It can add blocking findings; it cannot override the lane. A `pass` for a use case
with no passing case in the report becomes `missing`.

Existing repositories should run `devflow init` to add missing `ui_evaluator` prompt and schema
without overwriting local edits. Review the shipped planner and coder templates and merge their
UI-use-case instructions into any existing repo-local copies. `devflow init --force` overwrites
repo-local prompts and configuration, so use it only after reviewing or backing up those edits.
The `ui_evaluator` role is routed like `reviewer` when your `[routing] roles` does not list it.
