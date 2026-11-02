# Contributing to devflow

Thank you for helping make repository-aware engineering workflows more reliable.

## Before making changes

Check existing issues and open an issue for a substantial behavior or architecture change.
Include the problem, expected behavior and a small example. A focused bug fix can go straight
to a pull request with a reproduction.

## Local development

Python 3.11+, Git and uv are sufficient for the default test suite. Coding-agent accounts and
GitHub access are needed only for live checks and actual ticket execution.

```bash
git clone https://github.com/Rishik-J/devflow.git
cd devflow
uv tool install --editable .
uv run --no-project --with ruff ruff check .
uv run --no-project --with pytest --with-editable . python -m pytest -q
```

Create a branch for your change. Keep the diff focused, document user-facing behavior changes,
and add regression coverage when changing state transitions, approvals, git operations,
scheduling or agent output parsing. Tests should use disposable repositories and fake agents
unless a live service is the behavior being verified.

## Architecture boundaries

- The engine owns state, gates, git operations and host writes.
- Agent drivers handle CLI invocation and output normalization.
- Model decisions must satisfy the relevant schema before advancing a stage.
- Configured test commands come from the primary checkout, not a ticket's editable branch.
- Review approval names a commit; merging must preserve that relationship.
- Follow-up issues belong in Backlog. A person promotes them to Ready.

See [DESIGN.md](DESIGN.md) and the implementation in `src/devflow/` for context.

## Live tests

```bash
DEVFLOW_LIVE=1 uv run --no-project --with pytest --with-editable . \
  python -m pytest tests/test_live_agents.py -q
```

These tests call authenticated coding-agent CLIs and incur usage. They are skipped in normal CI.
Avoid posting credentials, full environment dumps, private ticket contents or provider tokens
in bug reports. Include redacted diagnostics and the tool versions needed to reproduce a failure.

## Pull requests

Explain the problem and resulting behavior, then list the checks you ran. Call out any change
to approval semantics, persistence, retry limits or tool permissions. A passing test suite is
necessary, but a review should also verify that the tests exercise the actual failure mode.

Contributions are provided under the repository's [MIT license](LICENSE).
