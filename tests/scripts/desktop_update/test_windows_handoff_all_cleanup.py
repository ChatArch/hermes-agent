"""All real-Windows handoff harnesses bound cleanup of their own child."""

import subprocess

import pytest

from tests.scripts.desktop_update import windows_handoff_support as support
from tests.scripts.desktop_update import test_desktop_update_windows_marker as marker
from tests.scripts.desktop_update import test_desktop_update_windows_commit_outcome as outcome


class OwnedProcess:
    pid = 434343

    def __init__(self, mode):
        self.mode = mode
        self.waits = []
        self.kills = 0

    def communicate(self, timeout=None):
        self.waits.append(timeout)
        if self.mode == "normal":
            return "normal output", None
        if len(self.waits) == 1 or self.mode == "never-closes" or (self.mode == "needs-kill" and not self.kills):
            raise subprocess.TimeoutExpired("owned-powershell", timeout)
        return "bounded diagnostic", None

    def kill(self):
        self.kills += 1

    @property
    def returncode(self):
        return 7


def _invoke(name, proc, tmp_path):
    if name == "shared":
        return support._finish(proc, timeout=30)
    if name == "marker":
        return marker._run(tmp_path, timeout=30)
    return outcome._handoff(tmp_path, timeout=30)


def _patch_owned_process(monkeypatch, proc):
    monkeypatch.setattr(support.subprocess, "Popen", lambda *_a, **_kw: proc)

    def publish_stub(install, _cli):
        (install / "hermes_cli").mkdir(parents=True)

    monkeypatch.setattr(outcome, "publish_fixture_launcher", publish_stub)


@pytest.mark.parametrize("name", ["shared", "marker", "outcome"])
@pytest.mark.parametrize("mode", ["recovers", "needs-kill", "never-closes"])
@pytest.mark.parametrize("taskkill_stalled", [False, True])
def test_every_timeout_cleanup_is_bounded(monkeypatch, tmp_path, name, mode, taskkill_stalled):
    proc = OwnedProcess(mode)
    calls = []
    _patch_owned_process(monkeypatch, proc)

    def terminate(command, **kwargs):
        calls.append((command, kwargs))
        if taskkill_stalled:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(support.subprocess, "run", terminate)
    with pytest.raises(pytest.fail.Exception, match="hand-off"):
        _invoke(name, proc, tmp_path)
    assert len(calls) == 1
    assert calls[0][0] == ["taskkill", "/T", "/F", "/PID", str(proc.pid)]
    assert 0 < calls[0][1]["timeout"] <= 15
    assert proc.waits[0] == 30
    assert all(wait is not None and 0 < wait <= 15 for wait in proc.waits[1:])
    assert len(proc.waits) <= 3
    assert bool(proc.kills) is (mode != "recovers" or taskkill_stalled)


@pytest.mark.parametrize("name", ["shared", "marker", "outcome"])
def test_normal_handoff_result_is_not_changed(monkeypatch, tmp_path, name):
    proc = OwnedProcess("normal")
    _patch_owned_process(monkeypatch, proc)
    monkeypatch.setattr(support.subprocess, "run", lambda *_a, **_kw: pytest.fail("normal result ran taskkill"))
    result = _invoke(name, proc, tmp_path)
    if name == "shared":
        assert result == (7, "normal output")
    elif name == "marker":
        assert result == (proc.pid, 7, "normal output")
    else:
        assert result[:4] == (7, "normal output", [], None)
    assert proc.waits == [30]
    assert proc.kills == 0
