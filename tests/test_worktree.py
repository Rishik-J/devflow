from pathlib import Path

import pytest

from devflow import worktree as wt
from tests.conftest import git


def test_slug_and_branch_name():
    assert wt.slugify("Fix: the Kalshi WS crossed-book!!") == "fix-the-kalshi-ws-crossed-book"
    assert wt.slugify("???") == "ticket"
    assert len(wt.slugify("a" * 200)) <= 40
    assert wt.branch_name(12, "Add X") == "devflow/12-add-x"


def test_create_branches_from_main(base_repo: Path, tmp_path: Path):
    root = tmp_path / "wts"
    w = wt.create(base_repo, root, 7, "Add feature", "main", fetch=False)
    assert w.path == root / "7-add-feature" and w.path.is_dir()
    assert w.branch == "devflow/7-add-feature"
    assert git(w.path, "rev-parse", "--abbrev-ref", "HEAD") == "devflow/7-add-feature"
    assert w.base_sha == git(base_repo, "rev-parse", "main")
    assert (w.path / "src" / "calc.py").exists()


def test_create_is_idempotent_for_resume(base_repo: Path, tmp_path: Path):
    root = tmp_path / "wts"
    a = wt.create(base_repo, root, 7, "Add feature", "main", fetch=False)
    (a.path / "note.txt").write_text("work in progress")
    b = wt.create(base_repo, root, 7, "Add feature", "main", fetch=False)
    assert b.path == a.path and (b.path / "note.txt").exists()


def test_recreate_after_directory_removed_reuses_branch(base_repo: Path, tmp_path: Path):
    root = tmp_path / "wts"
    a = wt.create(base_repo, root, 8, "Retry", "main", fetch=False)
    wt.remove(base_repo, a.path)
    assert not a.path.exists()
    b = wt.create(base_repo, root, 8, "Retry", "main", fetch=False)
    assert b.path.exists() and b.branch == a.branch


def test_remove_deletes_only_devflow_branches(base_repo: Path, tmp_path: Path):
    root = tmp_path / "wts"
    a = wt.create(base_repo, root, 9, "Gone", "main", fetch=False)
    wt.remove(base_repo, a.path, delete_branch=True)
    assert "devflow/9-gone" not in git(base_repo, "branch", "--list")
    assert "main" in git(base_repo, "branch", "--list")


def test_worktree_on_wrong_branch_is_refused(base_repo: Path, tmp_path: Path):
    root = tmp_path / "wts"
    a = wt.create(base_repo, root, 10, "Wrong", "main", fetch=False)
    git(a.path, "checkout", "-q", "-b", "other")
    with pytest.raises(wt.WorktreeError, match="expected"):
        wt.create(base_repo, root, 10, "Wrong", "main", fetch=False)


def test_missing_base_ref(tmp_path: Path):
    from tests.conftest import git_init

    empty = git_init(tmp_path / "empty")
    with pytest.raises(wt.WorktreeError, match="does not exist"):
        wt.create(empty, tmp_path / "wts", 1, "x", "main", fetch=False)


def test_list_worktrees(base_repo: Path, tmp_path: Path):
    wt.create(base_repo, tmp_path / "wts", 11, "Listed", "main", fetch=False)
    names = [p.name for p in wt.list_worktrees(base_repo)]
    assert "11-listed" in names
