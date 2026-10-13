"""Windows fixture identity sampling uses kernel FILETIME, not a new CIM process."""

import os
import subprocess

import pytest

from hermes_cli import update_lock
from tests.scripts.desktop_update import (
    test_desktop_update_windows_marker as marker,
    windows_handoff_support as support,
)


@pytest.mark.parametrize("entry", [support._creation_time, marker._creation_time])
@pytest.mark.parametrize("fraction", [0, 1, 4999, 9999, 10000, 1234567, 9999999])
def test_creation_sample_is_exact_millisecond_floor_without_process_launch(monkeypatch, entry, fraction):
    pid = 434343
    ticks = update_lock._FILETIME_UNIX_EPOCH + 1700000000 * 10000000 + fraction
    seen = []

    def kernel(target):
        seen.append(target)
        return ticks

    def forbidden(*_args, **_kwargs):
        pytest.fail("creation-time sampling must not launch PowerShell/CIM")

    monkeypatch.setattr(update_lock, "_windows_create_filetime", kernel)
    monkeypatch.setattr(subprocess, "run", forbidden)
    milliseconds = (ticks - update_lock._FILETIME_UNIX_EPOCH) // 10000
    seconds, remainder = divmod(milliseconds, 1000)
    assert entry(pid) == f"{seconds}.{remainder:03d}"
    assert seen == [pid]


@pytest.mark.parametrize("entry", [support._creation_time, marker._creation_time])
@pytest.mark.parametrize("before_epoch_ticks", [1, 10000, 12345678])
def test_negative_epoch_sample_preserves_original_cim_sign(monkeypatch, entry, before_epoch_ticks):
    ticks = update_lock._FILETIME_UNIX_EPOCH - before_epoch_ticks
    monkeypatch.setattr(update_lock, "_windows_create_filetime", lambda _pid: ticks)
    milliseconds = (ticks - update_lock._FILETIME_UNIX_EPOCH) // 10000
    # The previous CIM witness was int(ToUnixTimeMilliseconds()) / 1000,
    # formatted at three decimals. Preserve even a pre-1970 system clock.
    assert entry(434343) == f"{milliseconds / 1000:.3f}"


@pytest.mark.parametrize("entry", [support._creation_time, marker._creation_time])
def test_unreadable_creation_time_fails_instead_of_fabricating_identity(monkeypatch, entry):
    monkeypatch.setattr(update_lock, "_windows_create_filetime", lambda _pid: None)

    def forbidden(*_args, **_kwargs):
        pytest.fail("creation-time sampling must not launch PowerShell/CIM")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(pytest.fail.Exception, match="Kernel creation time unavailable"):
        entry(434343)


@pytest.mark.platforms("windows")
def test_native_creation_sample_matches_cim_millisecond_witness():
    pid = os.getpid()
    command = (f"$c = (Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CreationDate; "
               "[DateTimeOffset]::new($c.ToUniversalTime()).ToUnixTimeMilliseconds().ToString()")
    result = subprocess.run([support.POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=60, check=True)
    milliseconds = int(result.stdout.strip())
    seconds, remainder = divmod(milliseconds, 1000)
    expected = f"{seconds}.{remainder:03d}"
    assert support._creation_time(pid) == marker._creation_time(pid) == expected
