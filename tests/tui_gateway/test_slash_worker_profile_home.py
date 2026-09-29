"""Profile and argv propagation through the real slash worker constructor."""
from types import SimpleNamespace
import sys

import pytest


@pytest.mark.parametrize("scoped", [False, True])
def test_slash_worker_uses_requested_profile_and_keeps_argv(tmp_path, monkeypatch, scoped):
    from tui_gateway import server

    inherited = tmp_path / "default"
    requested = tmp_path / "work"
    inherited.mkdir()
    requested.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(inherited))
    captured = {}

    def spawn(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return SimpleNamespace(stdout=iter(()), stderr=iter(()))

    monkeypatch.setattr(server.subprocess, "Popen", spawn)
    worker = server._SlashWorker("test-session", "test-model",
                                 profile_home=str(requested) if scoped else None)
    assert worker.proc is not None
    assert captured["env"]["HERMES_HOME"] == str(requested if scoped else inherited)
    assert captured["env"]["PATH"]
    assert captured["argv"][:3] == [sys.executable, "-m", "tui_gateway.slash_worker"]
    assert captured["argv"][3:] == ["--session-key", "test-session", "--model", "test-model"]
