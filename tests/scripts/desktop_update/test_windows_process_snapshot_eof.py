"""Snapshot EOF logic and HANDLE lifetime with injected API callbacks, not a host ABI claim."""

import ctypes
from types import SimpleNamespace

import pytest

from hermes_cli import update_lock
from tests.scripts.desktop_update import windows_handoff_support as support


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


def _api(monkeypatch, error, *, handle=88, next_failure=False):
    closed = []

    def first(_handle, pointer):
        pointer._obj.th32ProcessID = 123
        pointer._obj.szExeFile = "fixture.exe"
        return True

    def next_row(*_args):
        if next_failure:
            raise OSError("iterator callback failed")
        return False

    api = SimpleNamespace(
        CreateToolhelp32Snapshot=Function(lambda *_args: handle),
        Process32FirstW=Function(first), Process32NextW=Function(next_row), CloseHandle=closed.append,
    )
    monkeypatch.setattr(update_lock, "_windows_kernel32", lambda: api)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    return closed


def test_normal_no_more_files_returns_rows_and_closes_snapshot(monkeypatch):
    closed = _api(monkeypatch, 18)
    assert support._kernel_process_rows() == [(123, "fixture.exe")]
    assert closed == [88]


@pytest.mark.parametrize("error", [259, 5])
def test_other_errors_reject_partial_rows_and_close_snapshot(monkeypatch, error):
    closed = _api(monkeypatch, error)
    with pytest.raises(pytest.fail.Exception, match="did not complete"):
        support._kernel_process_rows()
    assert closed == [88]


@pytest.mark.parametrize("handle", [0, ctypes.c_void_p(-1).value])
def test_invalid_snapshot_is_not_closed_as_owned_handle(monkeypatch, handle):
    closed = _api(monkeypatch, 18, handle=handle)
    with pytest.raises(pytest.fail.Exception, match="snapshot unavailable"):
        support._kernel_process_rows()
    assert closed == []


def test_callback_failure_still_closes_snapshot(monkeypatch):
    closed = _api(monkeypatch, 18, next_failure=True)
    with pytest.raises(OSError, match="iterator callback failed"):
        support._kernel_process_rows()
    assert closed == [88]
