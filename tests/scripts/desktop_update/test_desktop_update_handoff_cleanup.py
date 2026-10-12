"""A timed-out Windows hand-off test must bound cleanup of its own process."""

import subprocess
import threading
import time
from pathlib import Path

import pytest

from tests.scripts.desktop_update import test_desktop_update_windows_commit_outcome as outcome


class StalledHandoff:
    pid = 424242

    def __init__(self, *, still_stalled=False):
        self.still_stalled = still_stalled
        self.waits = []
        self.killed = False

    def communicate(self, timeout=None):
        self.waits.append(timeout)
        if len(self.waits) == 1 or self.still_stalled and not self.killed:
            raise subprocess.TimeoutExpired(["powershell"], timeout)
        return "bounded diagnostic", None

    def kill(self):
        self.killed = True

    @property
    def returncode(self):
        return 1


@pytest.mark.parametrize("still_stalled", [False, True])
@pytest.mark.parametrize("taskkill_stalled", [False, True])
def test_timed_out_handoff_bounds_cleanup_and_reaps_owned_process(
    monkeypatch, tmp_path, still_stalled, taskkill_stalled
):
    proc = StalledHandoff(still_stalled=still_stalled)
    taskkill = []

    def terminate(command, **kwargs):
        taskkill.append((command, kwargs))
        if taskkill_stalled:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        return subprocess.CompletedProcess(command, 0)

    def publish_stub(install, _cli):
        (install / "hermes_cli").mkdir(parents=True)

    monkeypatch.setattr(outcome, "publish_fixture_launcher", publish_stub)
    monkeypatch.setattr(outcome.subprocess, "Popen", lambda *_a, **_kw: proc)
    monkeypatch.setattr(outcome.subprocess, "run", terminate)

    with pytest.raises(pytest.fail.Exception, match="hand-off did not finish within"):
        outcome._handoff(tmp_path, timeout=30)

    assert taskkill[0][0] == ["taskkill", "/T", "/F", "/PID", str(proc.pid)]
    assert 0 < taskkill[0][1]["timeout"] <= 15
    assert proc.waits[0] == 30
    assert all(wait is not None and 0 < wait <= 15 for wait in proc.waits[1:])
    assert len(proc.waits) == (3 if still_stalled and not taskkill_stalled else 2)
    assert proc.killed is (still_stalled or taskkill_stalled)


@pytest.mark.parametrize("pause_before_publish", [False, True])
def test_r6_release_ownership_survives_observer_preemption(monkeypatch, tmp_path, pause_before_publish):
    release = tmp_path / "release-checkout-lock"
    observed_main_cleanup = threading.Event()
    observer_entered_touch = threading.Event()
    original_touch = Path.touch

    def touch_then_pause_observer(self, *args, **kwargs):
        if self != release:
            return original_touch(self, *args, **kwargs)
        if threading.current_thread() is threading.main_thread():
            try:
                return original_touch(self, *args, **kwargs)
            finally:
                observed_main_cleanup.set()
        if pause_before_publish:
            observer_entered_touch.set()
            assert observed_main_cleanup.wait(timeout=5)
        result = original_touch(self, *args, **kwargs)
        if not pause_before_publish:
            assert observed_main_cleanup.wait(timeout=5)
        return result

    class Holder:
        def __init__(self, _argv):
            original_touch(tmp_path / "checkout-lock-held")

        def poll(self):
            return None

        def wait(self, timeout):
            assert release.exists()
            return 0

        def kill(self):
            pytest.fail("the normal release must not kill the owned holder")

    def fake_handoff(root, *_args, prepare, **_kwargs):
        home = root / "home"
        log = home / "logs" / "desktop-update-handoff.log"
        log.parent.mkdir(parents=True)
        install = root / "checkout"
        install.mkdir()
        prepare(home, install)
        log.write_text("a process still holds the checkout update lock; keeping the update marker until it exits")
        deadline = time.monotonic() + 10
        while not (observer_entered_touch.is_set() if pause_before_publish else release.exists()):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        return 0, "", [], {"ok": True, "finished_at": int(time.time()) + 10}, home

    monkeypatch.setattr(Path, "touch", touch_then_pause_observer)
    monkeypatch.setattr(outcome.subprocess, "Popen", lambda argv, **_kwargs: Holder(argv))
    monkeypatch.setattr(outcome, "_handoff", fake_handoff)
    if pause_before_publish:
        with pytest.raises(AssertionError, match="never entered the checkout-lock release wait"):
            outcome.test_result_finished_at_is_stamped_after_the_r6_release_wait(tmp_path)
    else:
        outcome.test_result_finished_at_is_stamped_after_the_r6_release_wait(tmp_path)
    assert observed_main_cleanup.is_set()
