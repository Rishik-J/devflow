from pathlib import Path

from devflow import diffguard
from tests.conftest import commit_all, git


def branch(repo: Path) -> Path:
    git(repo, "checkout", "-q", "-b", "devflow/1-x")
    return repo


def kinds(repo: Path) -> set[str]:
    return {f.kind for f in diffguard.analyze(repo, "main")}


def test_clean_change_has_no_findings(base_repo: Path):
    branch(base_repo)
    (base_repo / "src" / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    commit_all(base_repo)
    assert diffguard.analyze(base_repo, "main") == []


def test_deleted_test_file_blocks(base_repo: Path):
    branch(base_repo)
    (base_repo / "tests" / "test_calc.py").unlink()
    commit_all(base_repo)
    found = diffguard.analyze(base_repo, "main")
    assert [f.kind for f in found] == ["deleted_test"]
    assert diffguard.blocking(found)


def test_added_skip_blocks(base_repo: Path):
    branch(base_repo)
    p = base_repo / "tests" / "test_calc.py"
    p.write_text("import pytest\n\n@pytest.mark.skip\n" + p.read_text())
    commit_all(base_repo)
    assert "skipped_test" in kinds(base_repo)


def test_js_skip_and_xfail_variants(base_repo: Path):
    branch(base_repo)
    (base_repo / "tests" / "a.test.ts").write_text("it.skip('x', () => {})\n")
    (base_repo / "tests" / "test_b.py").write_text("import pytest\npytest.xfail('later')\n")
    commit_all(base_repo)
    skips = [f for f in diffguard.analyze(base_repo, "main") if f.kind == "skipped_test"]
    assert {f.file for f in skips} == {"tests/a.test.ts", "tests/test_b.py"}


def test_assertions_removed_is_a_warning_not_a_block(base_repo: Path):
    branch(base_repo)
    (base_repo / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    add(1, 2)\n"
    )
    commit_all(base_repo)
    found = diffguard.analyze(base_repo, "main")
    assert [f.kind for f in found] == ["fewer_assertions"]
    assert diffguard.blocking(found) == []


def test_replacing_assertions_is_fine(base_repo: Path):
    branch(base_repo)
    p = base_repo / "tests" / "test_calc.py"
    p.write_text(p.read_text().replace("== 3", "== 3 and True").replace("== 0", "== 0 or False"))
    commit_all(base_repo)
    assert "fewer_assertions" not in kinds(base_repo)


def test_protected_paths_block(base_repo: Path):
    branch(base_repo)
    (base_repo / ".github" / "workflows" / "ci.yml").write_text("name: changed\n")
    (base_repo / ".devflow").mkdir()
    (base_repo / ".devflow" / "config.toml").write_text("x = 1\n")
    commit_all(base_repo)
    prot = [f.file for f in diffguard.analyze(base_repo, "main") if f.kind == "protected_path"]
    assert sorted(prot) == [".devflow/config.toml", ".github/workflows/ci.yml"]


def test_non_test_files_may_contain_skip_words(base_repo: Path):
    branch(base_repo)
    (base_repo / "src" / "util.py").write_text("# @pytest.mark.skip is documented here\n")
    commit_all(base_repo)
    assert diffguard.analyze(base_repo, "main") == []


def test_path_classifiers():
    assert diffguard.is_test_file("services/api/tests/unit/test_x.py")
    assert diffguard.is_test_file("web/foo.spec.ts")
    assert not diffguard.is_test_file("src/protest.py")
    assert diffguard.is_protected(".github/workflows/ci.yml")
