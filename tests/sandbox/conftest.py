import pytest

from tests.sandbox.harness import Sandbox


@pytest.fixture
def make_sandbox(tmp_path, monkeypatch):
    """Factory: each test builds the sandbox it needs (gates on/off, config extras, agents)."""
    built = []

    def make(**kw) -> Sandbox:
        sb = Sandbox(tmp_path / f"case{len(built)}", monkeypatch, **kw)
        built.append(sb)
        return sb

    return make
