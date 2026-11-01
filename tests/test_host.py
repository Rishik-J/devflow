import json
import subprocess

import pytest

from devflow import commands
from devflow.host import (
    STATUS_MARKER,
    Comment,
    DryRunHost,
    FakeHost,
    GhHost,
    HostError,
    repo_slug,
    upsert_status,
)


class Gh:
    """Canned `gh` runner: records every call, returns queued stdout."""

    def __init__(self, *outputs: str, code: int = 0, err: str = ""):
        self.outputs, self.calls, self.code, self.err = list(outputs), [], code, err

    def __call__(self, cmd, input_text=None, cwd=None):
        self.calls.append((cmd, input_text))
        out = self.outputs.pop(0) if self.outputs else ""
        return subprocess.CompletedProcess(cmd, self.code, out, self.err)


def test_repo_slug_formats():
    assert repo_slug("https://github.com/Rishik-J/Enterprise-Prediction-Markets.git") == \
        "Rishik-J/Enterprise-Prediction-Markets"
    assert repo_slug("git@github.com:acme/widgets.git") == "acme/widgets"
    assert repo_slug("https://github.com/acme/widgets") == "acme/widgets"
    with pytest.raises(HostError):
        repo_slug("not a remote")


def test_get_issue_parses_labels_and_author():
    raw = json.dumps({"number": 7, "title": "T", "body": None, "state": "OPEN",
                      "labels": [{"name": "devflow"}], "author": {"login": "rishik"}})
    gh = Gh(raw)
    issue = GhHost("a/b", gh).get_issue(7)
    assert (issue.number, issue.title, issue.body, issue.labels, issue.state, issue.author) == (
        7, "T", "", ["devflow"], "open", "rishik")
    assert gh.calls[0][0][:4] == ["gh", "issue", "view", "7"] and "-R" in gh.calls[0][0]


def test_comments_handles_paginated_concatenated_arrays():
    page1 = json.dumps([{"id": 1, "user": {"login": "a"}, "body": "x",
                         "created_at": "2026-11-01T10:00:00Z"}])
    page2 = json.dumps([{"id": 2, "user": None, "body": "/approve",
                         "created_at": "2026-11-01T11:00:00Z"}])
    comments = GhHost("a/b", Gh(page1 + "\n" + page2)).comments(3)
    assert [(c.id, c.author, c.body) for c in comments] == [(1, "a", "x"), (2, "", "/approve")]
    assert comments[0].created_at < comments[1].created_at


def test_comment_posts_json_body_on_stdin_not_in_argv():
    gh = Gh("123\n")
    cid = GhHost("a/b", gh).comment(5, 'tricky "quotes" $(rm -rf /) `x`')
    cmd, stdin = gh.calls[0]
    assert cid == 123 and json.loads(stdin) == {"body": 'tricky "quotes" $(rm -rf /) `x`'}
    assert "tricky" not in " ".join(cmd)


def test_update_comment_patches():
    gh = Gh("")
    GhHost("a/b", gh).update_comment(9, "new")
    cmd, stdin = gh.calls[0]
    assert "PATCH" in cmd and "repos/a/b/issues/comments/9" in cmd and json.loads(stdin)["body"] == "new"


def test_create_pr_reuses_an_existing_open_pr():
    listing = json.dumps([{"number": 4, "url": "u", "headRefName": "h", "baseRefName": "main"}])
    gh = Gh(listing)
    pr = GhHost("a/b", gh).create_pr("h", "main", "t", "b")
    assert pr.number == 4 and len(gh.calls) == 1  # no create call


def test_create_pr_creates_with_body_on_stdin():
    view = json.dumps({"number": 12, "url": "https://x/pull/12", "headRefName": "h",
                       "baseRefName": "main", "state": "OPEN", "mergedAt": None})
    gh = Gh("[]", "https://github.com/a/b/pull/12\n", view)
    pr = GhHost("a/b", gh).create_pr("h", "main", "Title", "Body with `ticks`")
    create = next(c for c in gh.calls if c[0][1:3] == ["pr", "create"])
    assert create[1] == "Body with `ticks`" and "--body-file" in create[0]
    assert pr.number == 12 and pr.state == "open"


def test_get_pr_reports_merged():
    view = json.dumps({"number": 1, "url": "u", "headRefName": "h", "baseRefName": "m",
                       "state": "MERGED", "mergedAt": "2026-11-01T00:00:00Z"})
    assert GhHost("a/b", Gh(view)).get_pr(1).state == "merged"


@pytest.mark.parametrize("rollup,expected", [
    ([], "none"),
    ([{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}], "success"),
    ([{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}], "pending"),
    ([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}], "failure"),
    ([{"state": "ERROR"}], "failure"),
    ([{"conclusion": "FAILURE"}, {"status": "IN_PROGRESS"}], "failure"),
])
def test_checks_rollup(rollup, expected):
    out = json.dumps({"statusCheckRollup": rollup})
    assert GhHost("a/b", Gh(out)).checks(1) == expected


def test_merge_uses_squash_and_errors_are_wrapped():
    gh = Gh("")
    GhHost("a/b", gh).merge_pr(3)
    assert gh.calls[0][0][1:3] == ["pr", "merge"] and "--squash" in gh.calls[0][0]
    with pytest.raises(HostError, match="not mergeable"):
        GhHost("a/b", Gh(code=1, err="not mergeable")).merge_pr(3)


def test_upsert_status_creates_then_updates_only_our_own_comment():
    host = FakeHost(me="bot")
    host.say(1, "someone", f"{STATUS_MARKER}\nforged")
    upsert_status(host, 1, "one")
    upsert_status(host, 1, "two")
    mine = [c for c in host.comments(1) if c.author == "bot"]
    assert len(mine) == 1 and mine[0].body.endswith("two")
    forged = next(c for c in host.comments(1) if c.author == "someone")
    assert forged.body.endswith("forged")


def test_dry_run_host_reads_through_and_never_writes():
    inner = FakeHost()
    inner.add_issue(1, "T")
    dry = DryRunHost(inner)
    assert dry.get_issue(1).title == "T"
    dry.comment(1, "hello")
    dry.create_pr("h", "main", "t", "b")
    dry.merge_pr(5)
    assert inner.comments(1) == [] and not inner.prs and not inner.merged
    assert len(dry.writes) == 3


# ---------------------------------------------------------------- commands


def c(body: str, author: str = "alice", cid: int = 1, t: float = 1.0) -> Comment:
    return Comment(cid, author, body, t)


def test_parse_commands():
    assert commands.parse(c("/approve")).kind == "approve"
    cmd = commands.parse(c("/answer yes\n\nand also no floats"))
    assert cmd.kind == "answer" and cmd.text == "yes\nand also no floats"
    assert commands.parse(c("/REJECT too risky")).text == "too risky"
    assert commands.parse(c("\n\n/resume")).kind == "resume"


@pytest.mark.parametrize("body", ["looks good, /approve", "approve", "/merge", "", "  ", "/",
                                  f"{STATUS_MARKER}\n/approve"])
def test_not_commands(body):
    assert commands.parse(c(body)) is None


def test_pending_filters_by_author_consumption_kind_and_orders_oldest_first():
    comments = [c("/approve", "alice", 3, 3.0), c("/approve", "mallory", 2, 2.0),
                c("/answer hi", "alice", 1, 1.0), c("/approve", "alice", 4, 4.0)]
    got = commands.pending(comments, {"alice"}, consumed={4})
    assert [(x.kind, x.comment_id) for x in got] == [("answer", 1), ("approve", 3)]
    only = commands.pending(comments, {"alice"}, set(), kinds=("approve",))
    assert [x.comment_id for x in only] == [3, 4]


def test_create_issue_sends_labels_and_body_on_stdin():
    view = json.dumps({"number": 41, "title": "T", "body": "B", "labels": [{"name": "devflow"}],
                       "state": "OPEN", "author": {"login": "me"}})
    gh = Gh("https://github.com/a/b/issues/41\n", view)
    issue = GhHost("a/b", gh).create_issue("T", "B with `ticks`", ["devflow", "devflow:discovered"])
    cmd, stdin = gh.calls[0]
    assert cmd[:3] == ["gh", "issue", "create"] and cmd.count("--label") == 2 and "-R" in cmd
    assert stdin == "B with `ticks`" and "ticks" not in " ".join(cmd)  # body travels on stdin
    assert issue.number == 41


def test_list_open_issues_and_ensure_label():
    listing = json.dumps([{"number": 3, "title": "A", "body": None, "labels": [{"name": "x"}],
                           "state": "OPEN", "author": None}])
    gh = Gh(listing, "")
    issues = GhHost("a/b", gh).list_open_issues("devflow")
    assert issues[0].number == 3 and issues[0].body == "" and issues[0].labels == ["x"]
    assert "--label" in gh.calls[0][0] and "devflow" in gh.calls[0][0]
    GhHost("a/b", gh).ensure_label("devflow", "0e8a16", "desc")
    label_cmd = gh.calls[-1][0]
    assert label_cmd[1:3] == ["label", "create"] and "--force" in label_cmd
