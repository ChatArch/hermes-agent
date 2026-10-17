"""Real Windows handoff contract cases, grouped below the unchanged file deadline."""
from __future__ import annotations
from pathlib import Path
import subprocess
import sys
import threading
import time
import pytest
import tests.scripts.desktop_update.windows_commit_support as _handoff_owner
from tests.scripts.desktop_update.windows_commit_support import _CHECKOUT_HOLDER, _handoff
_HANDOFF_TRACE_OWNER = _handoff_owner

@pytest.mark.platforms('windows')
def test_result_finished_at_is_stamped_after_the_r6_release_wait(tmp_path: Path) -> None:
    # The relaunched Desktop drops a non-manual result whose finished_at is 30 minutes old; the
    # R6 wait lasts up to 2 h. The result must carry the time the hand-off actually finished.
    holders = []
    release = tmp_path / 'release-checkout-lock'
    ready = tmp_path / 'checkout-lock-held'
    release_at = []
    observers = []
    stop_observer = threading.Event()

    def publish_release_once() -> bool:
        try:
            release.touch(exist_ok=False)
        except FileExistsError:
            return False
        return True

    def hold_checkout(home: Path, install: Path) -> None:
        holder = subprocess.Popen([sys.executable, '-c', _CHECKOUT_HOLDER,
                                   str(install / '.hermes-update.lock'), str(ready), str(release)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        holders.append(holder)
        deadline = time.monotonic() + 15
        while not ready.exists():
            assert holder.poll() is None and time.monotonic() < deadline, 'checkout holder never acquired its lock'
            time.sleep(0.05)

        def release_after_wait():
            log = home / 'logs/desktop-update-handoff.log'
            try:
                # _handoff owns the 150s deadline; do not expire before it enters R6.
                while not stop_observer.is_set():
                    try:
                        if log.exists() and 'keeping the update marker' in log.read_text(encoding='utf-8-sig'):
                            # Distinguish the initial result stamp from the stamp after R6 releases.
                            if stop_observer.wait(2):
                                return  # cleanup won the race; this was not an R6 release
                            if publish_release_once():
                                release_at.append(release.stat().st_mtime_ns)
                            return
                    except PermissionError:  # Add-Content briefly owns the file without read sharing.
                        pass
                    time.sleep(0.1)
            finally:
                publish_release_once()

        observer = threading.Thread(target=release_after_wait, daemon=True)
        observers.append(observer)
        observer.start()
    try:
        code, out, _, result, home = _handoff(tmp_path, '-NoGateway', prepare=hold_checkout)
        handoff_done_at = time.time_ns()
    finally:
        stop_observer.set()
        publish_release_once()
        for observer in observers:
            observer.join(timeout=5)
        for proc in holders:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        assert all(not observer.is_alive() for observer in observers), 'checkout-lock observer did not stop'
    assert len(release_at) == 1, 'hand-off never entered the checkout-lock release wait'
    assert handoff_done_at >= release_at[0], 'hand-off returned before the observer released the lock'
    assert code == 0, out
    log = (home / 'logs/desktop-update-handoff.log').read_text(encoding='utf-8-sig')
    assert 'keeping the update marker' in log, log
    assert result['ok'] is True and result['finished_at'] >= release_at[0] // 1_000_000_000 - 1, result


@pytest.mark.platforms('windows')
def test_marker_with_a_live_delegate_is_handed_to_it_at_finish(tmp_path: Path) -> None:
    # A7 release rule: the owner deletes its claim unless a delegate still runs; then that
    # delegate becomes the owner (canonical body, started_at kept, no delegate line).
    pid_file = tmp_path / 'delegate.pid'
    try:
        code, out, _, _result, home = _handoff(tmp_path, HANDOFF_DELEGATE=str(pid_file))
        assert code == 0, out
        marker = (home / '.hermes-update-in-progress').read_bytes().decode().split('\n')
        assert marker[0] == pid_file.read_text(encoding='utf-8-sig'), marker
        assert marker[2].startswith('ct:') and marker[3:] == [''], marker
    finally:
        if pid_file.exists():
            subprocess.run(['taskkill', '/F', '/PID', pid_file.read_text(encoding='utf-8-sig')], capture_output=True, check=False)
