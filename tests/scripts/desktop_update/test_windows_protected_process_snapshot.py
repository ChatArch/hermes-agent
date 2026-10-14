"""Protected-process witnesses preserve rights and close query handles."""

import os
from types import SimpleNamespace

import pytest

from hermes_cli import update_lock
from tests.scripts.desktop_update import windows_handoff_support as support


def _select(rows, opened, closed):
    fn = getattr(support, "_select_protected_pid", None)
    assert callable(fn), "protected PID selection still depends on a CIM subprocess"
    return fn(rows, opened, closed)


def test_selection_keeps_limited_only_predicate_and_closes_all_handles():
    calls, closed = [], []

    def opened(access, inherit, pid):
        calls.append((access, inherit, pid))
        return {(2, 0x0400): 202, (2, 0x1000): 203, (3, 0x0400): 0, (3, 0x1000): 303}[(pid, access)]

    assert _select([(1, "python.exe"), (2, "services.exe"), (3, "lsass.exe")], opened, closed.append) == 3
    assert calls == [(0x0400, False, 2), (0x1000, False, 2), (0x0400, False, 3), (0x1000, False, 3)]
    assert closed == [202, 203, 303]


@pytest.mark.parametrize("rights", [(0, 0), (101, 102), (101, 0)])
def test_non_protected_process_is_not_accepted(rights):
    closed = []
    with pytest.raises(pytest.fail.Exception, match="limited query rights"):
        _select([(7, "services.exe")], lambda access, *_args: rights[access == 0x1000], closed.append)
    assert closed == [handle for handle in rights if handle]


def test_existing_handle_closes_when_second_query_fails():
    closed = []

    def opened(access, *_args):
        if access == 0x0400:
            return 901
        raise OSError("query fixture failed")

    with pytest.raises(OSError, match="query fixture failed"):
        _select([(7, "services.exe")], opened, closed.append)
    assert closed == [901]


def test_unlisted_process_names_are_not_probed():
    def forbidden(*_args):
        pytest.fail("unlisted process was probed")

    with pytest.raises(pytest.fail.Exception, match="limited query rights"):
        _select([(7, "SERVICES.EXE"), (8, "python.exe")], forbidden, forbidden)


def test_marker_witness_uses_snapshot_without_subprocess(monkeypatch):
    fn = getattr(support, "_protected_pid", None)
    assert callable(fn), "protected PID witness still depends on a CIM subprocess"
    closed = []
    api = SimpleNamespace(OpenProcess=lambda access, _inherit, _pid: 0 if access == 0x0400 else 901, CloseHandle=closed.append)
    monkeypatch.setattr(update_lock, "_windows_kernel32", lambda: api)
    monkeypatch.setattr(support, "_kernel_process_rows", lambda: [(7, "services.exe")])
    monkeypatch.setattr(support.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("CIM subprocess started"))
    assert fn() == 7 and closed == [901]


def test_empty_snapshot_fails_instead_of_fabricating_identity(monkeypatch):
    fn = getattr(support, "_protected_pid", None)
    assert callable(fn), "protected PID witness still depends on a CIM subprocess"
    api = SimpleNamespace(OpenProcess=lambda *_args: pytest.fail("empty snapshot queried"), CloseHandle=lambda _handle: None)
    monkeypatch.setattr(update_lock, "_windows_kernel32", lambda: api)
    monkeypatch.setattr(support, "_kernel_process_rows", list)
    with pytest.raises(pytest.fail.Exception, match="limited query rights"):
        fn()


@pytest.mark.platforms("windows")
def test_native_snapshot_contains_the_running_interpreter():
    rows = support._kernel_process_rows()
    assert any(pid == os.getpid() and name for pid, name in rows)
