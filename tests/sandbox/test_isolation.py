"""The sandbox must be hermetic: no real agent CLI is reachable, only the stand-ins."""
import shutil


def test_only_the_stand_ins_are_on_path(make_sandbox):
    sb = make_sandbox()
    for tool in ("claude", "codex", "openclaw", "gh"):
        assert shutil.which(tool) == str(sb.bin / tool)
    assert shutil.which("git")  # real git is needed and available


def test_removing_a_stand_in_makes_the_tool_unavailable_not_real(make_sandbox):
    import os
    sb = make_sandbox()
    os.remove(sb.bin / "openclaw")
    assert shutil.which("openclaw") is None  # does not fall through to a real install
