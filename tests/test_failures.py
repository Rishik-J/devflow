"""Failure classification: why did the call fail, and what should the engine do about it."""
import pytest

from devflow.failures import INFRA, LIMIT, OTHER, SANDBOX, classify


@pytest.mark.parametrize("text,expected", [
    # the real #66 messages
    ("claude coder failed: success: You've hit your 5-hour limit · resets 6:30pm", LIMIT),
    ("All failures were sandbox-denied socket binds; browser execution remains unverified.", SANDBOX),
    ("PermissionError: [Errno 1] Operation not permitted", SANDBOX),
    ("socket bind to 127.0.0.1 blocked by the sandbox", SANDBOX),
    ("[openclaw] The CLI command failed.\n[openclaw] For help, run `openclaw doctor`.", INFRA),
    ("gh project item-list: GraphQL: API rate limit exceeded for user ID 1", LIMIT),
    ("HTTP 503 service unavailable", INFRA),
    ("AssertionError: expected 3 got 4", OTHER),
    ("", OTHER),
    (None, OTHER),
])
def test_classify(text, expected):
    assert classify(text) == expected


def test_sandbox_wins_over_other_signals():
    assert classify("429 limit reached; but also sandbox denied the socket") == SANDBOX


def test_ordinary_test_failure_mentioning_permission_is_not_a_sandbox_denial():
    assert classify("FAILED test_perms.py::test_file_mode - assert 0o644 == 0o600") == OTHER
