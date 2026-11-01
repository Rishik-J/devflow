import json
import subprocess
from pathlib import Path

import pytest

from devflow import followups
from devflow.board import (
    BoardError,
    BoardItem,
    DryRunBoard,
    FakeBoard,
    GhBoard,
    status_for,
)
from devflow.host import FakeHost, HostError
from devflow.loadconfig import DEFAULT_STATUSES, BoardCfg, ConfigError, load_config
from devflow.scheduler import dependencies, priority_rank, select
from devflow.states import State, Ticket
from devflow.store import Store, state_db_path

# Shapes follow `gh project ... --format json`. They are recorded from the documented output,
# not from a live project; `devflow board` against a real Project is the check that confirms them.
VIEW = {"id": "PVT_abc", "number": 3, "title": "devflow"}
FIELDS = {"fields": [
    {"id": "PVTF_t", "name": "Title", "type": "ProjectV2Field"},
    {"id": "PVTSSF_s", "name": "Status", "type": "ProjectV2SingleSelectField", "options": [
        {"id": "o_backlog", "name": "Backlog"}, {"id": "o_ready", "name": "Ready"},
        {"id": "o_prog", "name": "In progress"}, {"id": "o_rev", "name": "In review"},
        {"id": "o_done", "name": "Done"}]},
], "totalCount": 2}
ITEMS = {"items": [
    {"id": "PVTI_1", "title": "Add x", "status": "Ready", "priority": "P1", "labels": ["devflow"],
     "content": {"type": "Issue", "number": 12, "title": "Add x", "repository": "a/b"}},
    {"id": "PVTI_2", "title": "A PR", "status": "In review",
     "content": {"type": "PullRequest", "number": 5, "repository": "a/b"}},
    {"id": "PVTI_3", "title": "Elsewhere", "status": "Ready",
     "content": {"type": "Issue", "number": 9, "repository": "other/repo"}},
    {"id": "PVTI_4", "title": "Draft", "status": "Ready", "content": {"type": "DraftIssue"}},
], "totalCount": 4}


class Gh:
    def __init__(self, responses: dict[str, object] | None = None, code: int = 0, err: str = ""):
        self.responses = {"view": VIEW, "field-list": FIELDS, "item-list": ITEMS,
                          "item-add": {"id": "PVTI_new"}, "item-edit": {}, **(responses or {})}
        self.calls: list[list[str]] = []
        self.code, self.err = code, err

    def __call__(self, cmd, input_text=None):
        self.calls.append(cmd)
        body = json.dumps(self.responses.get(cmd[2], {}))
        return subprocess.CompletedProcess(cmd, self.code, body, self.err)

    def edits(self):
        return [c for c in self.calls if c[2] == "item-edit"]


def board(gh: Gh) -> GhBoard:
    return GhBoard("a/b", "owner", 3, runner=gh)


# ---------------------------------------------------------------- GhBoard


def test_items_keep_only_this_repos_issues():
    got = board(Gh()).items()
    assert [(i.issue, i.status, i.priority, i.labels) for i in got] == [
        (12, "Ready", "P1", ["devflow"])]
    assert got[0].item_id == "PVTI_1" and got[0].title == "Add x"


def test_set_status_edits_with_the_right_ids():
    gh = Gh()
    board(gh).set_status(12, "In progress")
    edit = gh.edits()[0]
    assert edit[edit.index("--id") + 1] == "PVTI_1"
    assert edit[edit.index("--project-id") + 1] == "PVT_abc"
    assert edit[edit.index("--field-id") + 1] == "PVTSSF_s"
    assert edit[edit.index("--single-select-option-id") + 1] == "o_prog"


def test_project_metadata_is_fetched_once():
    gh = Gh()
    b = board(gh)
    b.set_status(12, "Done")
    b.set_status(12, "In review")
    assert sum(1 for c in gh.calls if c[2] == "view") == 1
    assert sum(1 for c in gh.calls if c[2] == "field-list") == 1


def test_add_issue_adds_then_sets_status():
    gh = Gh()
    board(gh).add_issue(40, "https://github.com/a/b/issues/40", "Backlog")
    add = next(c for c in gh.calls if c[2] == "item-add")
    assert add[add.index("--url") + 1].endswith("/issues/40")
    edit = gh.edits()[0]
    assert edit[edit.index("--id") + 1] == "PVTI_new"
    assert edit[edit.index("--single-select-option-id") + 1] == "o_backlog"


def test_unknown_status_lists_what_exists():
    with pytest.raises(BoardError, match=r"no option 'Shipped'.*Backlog"):
        board(Gh()).set_status(12, "Shipped")


def test_issue_not_on_the_board_and_missing_status_field():
    with pytest.raises(BoardError, match="not on project #3"):
        board(Gh()).set_status(999, "Done")
    no_status = {"fields": [{"id": "x", "name": "Title"}]}
    with pytest.raises(BoardError, match="no 'Status' field"):
        board(Gh({"field-list": no_status})).meta()


def test_missing_project_scope_gets_a_hint():
    gh = Gh(code=1, err="error: your authentication token is missing required scopes [project]")
    with pytest.raises(BoardError, match=r"gh auth refresh -s project"):
        board(gh).items()


def test_status_for_state():
    s = DEFAULT_STATUSES
    assert status_for(State.REFINING, s) == "In progress"
    assert status_for(State.AWAITING_MERGE_APPROVAL, s) == "In review"
    assert status_for(State.MERGED, s) == "Done"
    assert status_for(State.ESCALATED, s) == "In progress"
    assert status_for(State.CANCELLED, s) == "Backlog"
    assert {status_for(st, s) for st in State} <= set(s.values())


def test_dry_run_board_logs_instead_of_writing():
    inner = FakeBoard()
    inner.put(1, "Ready")
    dry = DryRunBoard(inner)
    dry.set_status(1, "Done")
    dry.add_issue(2, "u", "Backlog")
    assert inner.status_of(1) == "Ready" and inner.status_of(2) is None
    assert dry.writes == ["board: #1 -> Done", "board: add #2 as Backlog"]
    assert [i.issue for i in dry.items()] == [1]


# ---------------------------------------------------------------- config


def test_board_config_defaults_and_overrides(tmp_path: Path):
    from devflow.graphify import GraphResult
    from devflow.init import run_init
    from tests.conftest import git_init

    repo = git_init(tmp_path / "r")
    run_init(repo, graph_builder=lambda r: GraphResult(False, False, message="x"), which=lambda b: None)
    path = repo / ".devflow" / "config.toml"
    assert load_config(repo).board == BoardCfg()
    assert not load_config(repo).board.enabled
    path.write_text(path.read_text() + '\n[github]\nproject = 3\nowner = "me"\nmax_active = 2\n'
                    '[github.statuses]\nready = "Todo"\n')
    cfg = load_config(repo)
    assert cfg.board.enabled and cfg.board.project == 3 and cfg.board.owner == "me"
    assert cfg.board.max_active == 2 and cfg.board.statuses["ready"] == "Todo"
    assert cfg.board.statuses["done"] == "Done"
    path.write_text(path.read_text() + 'bogus = 1\n[github.statuses]\n')  # duplicate table
    with pytest.raises(ConfigError):
        load_config(repo)


def test_unknown_status_key_is_rejected(tmp_path: Path):
    from devflow.graphify import GraphResult
    from devflow.init import run_init
    from tests.conftest import git_init

    repo = git_init(tmp_path / "r")
    run_init(repo, graph_builder=lambda r: GraphResult(False, False, message="x"), which=lambda b: None)
    path = repo / ".devflow" / "config.toml"
    path.write_text(path.read_text() + '\n[github]\nproject = 1\n[github.statuses]\nshipped = "X"\n')
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(repo)


# ---------------------------------------------------------------- scheduler


@pytest.fixture
def world(tmp_path: Path):
    store = Store(state_db_path(tmp_path))
    host, brd = FakeHost(), FakeBoard()
    yield store, host, brd, BoardCfg(project=3)
    store.close()


def add(host, brd, n, *, status="Ready", priority="", body="", labels=("devflow",), state="open"):
    host.add_issue(n, f"ticket {n}", body, state=state)
    brd.put(n, status, priority=priority, title=f"ticket {n}", labels=list(labels))


def test_dependency_parsing():
    assert dependencies("Blocked by #3, depends on #7 and BLOCKED BY #3") == [3, 7]
    assert dependencies("see #9 for context") == [] and dependencies("") == []


def test_priority_rank_orders_p0_first_and_unranked_last():
    assert [priority_rank(p) for p in ("P0", "p1", "2", "", "High")] == [0, 1, 2, 10000, 10000]


def test_picks_by_priority_then_age(world):
    store, host, brd, cfg = world
    add(host, brd, 5, priority="P2")
    add(host, brd, 3, priority="P1")
    add(host, brd, 4, priority="P1")
    add(host, brd, 6)
    sel = select(brd, host, store, cfg)
    assert [i.issue for i in sel.ready] == [3, 4, 5, 6] and sel.pick.issue == 3
    assert "P1" in sel.reason


def test_only_ready_and_labelled_open_issues(world):
    store, host, brd, cfg = world
    add(host, brd, 1, status="Backlog")
    add(host, brd, 2, labels=("bug",))
    add(host, brd, 3, state="closed")
    add(host, brd, 4)
    sel = select(brd, host, store, cfg)
    assert [i.issue for i in sel.ready] == [4]
    reasons = dict(sel.skipped)
    assert "label" in reasons[2] and "closed" in reasons[3] and 1 not in reasons


def test_blocked_tickets_wait_for_their_dependencies(world):
    store, host, brd, cfg = world
    host.add_issue(10, "dep open", "")
    host.add_issue(11, "dep done", "", state="closed")
    add(host, brd, 1, body="Blocked by #10")
    add(host, brd, 2, body="Depends on #11")
    add(host, brd, 3, body="Blocked by #404")  # cannot be read: treated as blocking
    sel = select(brd, host, store, cfg)
    assert [i.issue for i in sel.ready] == [2]
    assert dict(sel.skipped)[1] == "blocked by #10" and dict(sel.skipped)[3] == "blocked by #404"


def test_work_in_progress_limit_and_waiting_tickets_do_not_count(world):
    store, host, brd, cfg = world
    add(host, brd, 1)
    store.save_ticket(Ticket(id="#7", issue=7, state=State.IMPLEMENTING))
    sel = select(brd, host, store, cfg)
    assert sel.pick is None and sel.resume == [7] and "max_active = 1" in sel.reason
    store.save_ticket(Ticket(id="#7", issue=7, state=State.AWAITING_MERGE_APPROVAL))
    sel = select(brd, host, store, cfg)
    assert sel.pick.issue == 1 and sel.resume == []  # a human gate does not block the queue
    assert select(brd, host, store, BoardCfg(project=3, max_active=2)).pick.issue == 1


def test_tickets_devflow_already_has_are_not_picked_again(world):
    store, host, brd, cfg = world
    add(host, brd, 1)
    add(host, brd, 2)
    store.save_ticket(Ticket(id="#1", issue=1, state=State.MERGED))
    store.save_ticket(Ticket(id="#2", issue=2, state=State.AWAITING_PLAN_APPROVAL))
    sel = select(brd, host, store, cfg)
    assert sel.pick is None and "no eligible" in sel.reason
    assert "finished" in dict(sel.skipped)[1] and "in progress" in dict(sel.skipped)[2]


def test_unreadable_issue_is_skipped_not_fatal(world):
    store, host, brd, cfg = world
    brd.put(77, "Ready", labels=["devflow"])  # on the board but not readable on the host
    add(host, brd, 1)
    sel = select(brd, host, store, cfg)
    assert sel.pick.issue == 1 and "cannot read" in dict(sel.skipped)[77]


# ---------------------------------------------------------------- follow-ups


PLAN = {"follow_ups": [{"title": "Cache the config lookup", "why": "It is read on every call."}]}
REVIEW = {"findings": [
    {"id": "a", "severity": "major", "blocking": False, "file": "src/x.py", "line": 4,
     "summary": "divide() does not handle NaN", "suggested_fix": "check math.isnan"},
    {"id": "b", "severity": "nit", "blocking": False, "file": "src/x.py", "summary": "naming"},
    {"id": "c", "severity": "minor", "blocking": False, "file": "src/y.py", "summary": "stale comment"},
    {"id": "d", "severity": "blocker", "blocking": True, "file": "src/z.py", "summary": "was fixed"},
]}


def test_candidates_come_from_planner_and_nonblocking_findings_only():
    got = followups.collect(PLAN, REVIEW)
    assert [c.title for c in got] == ["Cache the config lookup", "divide() does not handle NaN",
                                      "stale comment"]
    assert got[1].why.endswith("Suggested: check math.isnan") and "line 4" in got[1].why
    assert followups.collect(None, None) == []


def test_filing_creates_backlog_tickets_with_labels_and_provenance(world):
    store, host, brd, cfg = world
    parent = Ticket(id="#1", issue=1, title="Add multiply")
    report = followups.file_followups(host, brd, store, cfg, parent, followups.collect(PLAN, REVIEW))
    assert len(report.filed) == 3
    issue = report.filed[0].issue
    assert issue.labels == ["devflow", "devflow:discovered"]
    assert "Found while working on #1 (Add multiply)" in issue.body
    assert "followup parent=#1 key=plan:" in issue.body and "Backlog" not in issue.labels
    assert brd.status_of(issue.number) == "Backlog"
    assert {"devflow", "devflow:discovered"} <= host.labels


def test_followups_are_never_ready(world):
    store, host, brd, cfg = world
    parent = Ticket(id="#1", issue=1, title="t")
    followups.file_followups(host, brd, store, cfg, parent, followups.collect(PLAN, REVIEW))
    assert all(i.status == "Backlog" for i in brd.items())


def test_filing_is_idempotent_and_capped_and_deduplicated(world):
    store, host, brd, cfg = world
    parent = Ticket(id="#1", issue=1, title="t")
    cands = followups.collect(PLAN, REVIEW)
    first = followups.file_followups(host, brd, store, BoardCfg(project=3, followups_per_ticket=2),
                                     parent, cands)
    assert len(first.filed) == 2 and first.skipped == [("stale comment", "over the cap of 2 per ticket")]
    again = followups.file_followups(host, brd, store, cfg, parent, cands)
    assert [t for t, _ in again.skipped][:2] == ["Cache the config lookup", "divide() does not handle NaN"]
    assert len(again.filed) == 1 and again.filed[0].candidate.title == "stale comment"
    host.add_issue(300, "Cache config lookups", "")
    dup = followups.file_followups(host, None, store, cfg, Ticket(id="#2", issue=2, title="u"),
                                   followups.collect(PLAN, None))
    assert not dup.filed and "open issue already covers" in dup.skipped[0][1]


def test_title_similarity():
    assert followups.similar("Cache the config lookup", "cache config lookups!")
    assert not followups.similar("Cache the config lookup", "Add retry to the uploader")
    assert followups.similar("###", "###") and not followups.similar("###", "???")


def test_host_errors_surface_from_the_fake_when_missing(world):
    _, host, _, _ = world
    with pytest.raises(HostError):
        host.get_issue(12345)


def test_board_item_defaults():
    assert BoardItem(1, "Ready").labels == []


def test_stemming_treats_plurals_as_the_same_word():
    assert followups.tokens("Cache lookups") == followups.tokens("cache lookup")
    assert "class" in followups.tokens("Classes class")  # "ss" words are left alone
