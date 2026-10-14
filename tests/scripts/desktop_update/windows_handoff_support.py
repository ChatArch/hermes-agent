"""Shared helpers for the real-process Windows hand-off protocol tests (not collected)."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime
from functools import wraps
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = ROOT / 'scripts/desktop-update/windows.ps1'
MARKER_PS1 = ROOT / 'scripts/desktop-update/marker.ps1'
MARKER = '.hermes-update-in-progress'
POWERSHELL = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                          'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')

_TRACE_MODULES = {
    'test_desktop_update_windows_commit_outcome': ('commit', (
        ('_handoff', 'handoff'), ('publish_fixture_launcher', 'fixture'), ('_finish', 'finish'),
    )),
    'test_desktop_update_windows_marker': ('marker', (
        ('_run', 'handoff'), ('publish_fixture_launcher', 'fixture'), ('_finish', 'finish'),
        ('_protected_pid', 'claim-clock'), ('_creation_time', 'claim-clock'),
        ('legacy_read', 'legacy-reader'), ('_result', 'result'),
    )),
    'test_desktop_update_windows_handoff_lineage': ('lineage', (
        ('_old_desktop', 'old-desktop'), ('_script', 'spawn'), ('_finish', 'finish'),
        ('_creation_time', 'claim-clock'), ('_wait_for_log', 'wait-log'),
    )),
}
_TRACE_STEPS = frozenset({
    'case', 'handoff', 'fixture', 'finish', 'claim-clock', 'legacy-reader', 'result',
    'old-desktop', 'spawn', 'wait-log', 'prepare',
})

_STAGE_LABELS = (
    (re.compile(r'^(?:claimed|adopted|reclaiming stale) update marker\b'), 'claim'),
    (re.compile(r'^hand-off start:'), 'start'),
    (re.compile(r'^update marker names its custodian pid\b'), 'custodian'),
    (re.compile(r'^process cwd set to install root:'), 'cwd'),
    (re.compile(r'^running: python\b'), 'update'),
    (re.compile(r'^hermes update exit code:'), 'exit'),
    (re.compile(r'^removed update marker \(owned\)'), 'release'),
)
_STAGE_ORDER = {name: index for index, (_, name) in enumerate(_STAGE_LABELS)}
_STAGE_TIMESTAMP = re.compile(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|[+-]\d\d:\d\d)) (.*)$')


def _handoff_stage_spans(family: str, output: str) -> list[dict]:
    """Measure adjacent, known PowerShell milestones; never retain their text."""
    if family not in {'commit', 'marker'}:
        return []
    previous = None
    spans = []
    for line in output.splitlines():
        match = _STAGE_TIMESTAMP.fullmatch(line)
        if match is None:
            continue
        label = next((name for pattern, name in _STAGE_LABELS if pattern.search(match[2])), None)
        if label is None:
            continue
        try:
            stamp = datetime.fromisoformat(match[1])
        except ValueError:
            continue
        if previous is not None:
            old_label, old_stamp = previous
            if _STAGE_ORDER[label] <= _STAGE_ORDER[old_label] or stamp < old_stamp:
                continue
            spans.append({'handoff_stage': f'{old_label}-to-{label}', 'family': family,
                          'elapsed_s': int((stamp - old_stamp).total_seconds())})
        previous = (label, stamp)
    return spans


def _phase_record(family: str, step: str, edge: str) -> None:
    # Flush straight to the per-file runner pipe; do not rely on pytest's
    # per-case captured buffer, which disappears when the file cap kills it.
    record = {'handoff_phase': step, 'family': family, 'edge': edge,
              'monotonic_ns': time.monotonic_ns()}
    try:
        print(json.dumps(record), flush=True)
    except (OSError, ValueError):
        return  # A closed diagnostic pipe must not replace the test result.


def trace_handoff_phase(function=None, *, family: str, step: str, _capture=None):
    """Bracket one harness call, without recording arguments, paths or errors."""
    if family not in {'commit', 'marker', 'lineage'} or step not in _TRACE_STEPS:
        raise ValueError('Unknown handoff phase')

    def decorate(call):
        @wraps(call)
        def traced(*args, **kwargs):
            # pytest restarts capture between fixture setup and call. Keep
            # this disable scope inside the actual call, not only over yield.
            with _capture.disabled() if _capture is not None else nullcontext():
                _phase_record(family, step, 'enter')
                try:
                    result = call(*args, **kwargs)
                    if step == 'handoff' and isinstance(result, tuple):
                        output_index = {'commit': 1, 'marker': 2}.get(family)
                        if output_index is not None and len(result) > output_index and isinstance(result[output_index], str):
                            for record in _handoff_stage_spans(family, result[output_index]):
                                try:
                                    print(json.dumps(record), flush=True)
                                except (OSError, ValueError):
                                    break
                    return result
                finally:
                    _phase_record(family, step, 'exit')
        return traced

    return decorate if function is None else decorate(function)


@contextmanager
def instrument_handoff_phases(request, monkeypatch):
    """Only the three native timeout families bypass pytest's output capture."""
    module = request.module
    spec = _TRACE_MODULES.get(module.__name__.rsplit('.', 1)[-1])
    if spec is None:
        yield
        return
    family, helpers = spec
    capture = request.getfixturevalue('capfd')
    with monkeypatch.context() as scoped, capture.disabled():
        for name, step in helpers:
            scoped.setattr(module, name, trace_handoff_phase(getattr(module, name), family=family, step=step, _capture=capture))
        _phase_record(family, 'case', 'enter')
        try:
            yield
        finally:
            _phase_record(family, 'case', 'exit')


HOLD_CLI = """
import os, sys, time
from pathlib import Path
def main():
    if '--version' in sys.argv:
        print('Install directory: ' + str(Path(__file__).resolve().parents[1])); return 0
    if '--help' in sys.argv:
        print('--keep-stash'); return 0
    if sys.argv[1:2] == ['update']:   # the update's first act: say so, then wait to be released
        hold = Path(os.environ['HANDOFF_HOLD'])
        Path(str(hold) + '.pid').write_text(str(os.getpid()), encoding='utf-8')
        while not hold.exists():
            time.sleep(0.05)
    return 0
if __name__ == '__main__':
    sys.exit(main())
"""


def _ps(command: str, timeout: int = 60) -> str:
    return subprocess.run([POWERSHELL, '-NoProfile', '-Command', command],
                          capture_output=True, text=True, timeout=timeout, check=True).stdout.strip()


def _creation_time(pid: int) -> str:
    # The same limited-rights kernel clock, without starting a PowerShell/CIM
    # observer for each marker. Keep CIM's millisecond truncation exactly.
    from hermes_cli.update_lock import _FILETIME_UNIX_EPOCH, _windows_create_filetime

    ticks = _windows_create_filetime(pid)
    if ticks is None:
        pytest.fail(f'Kernel creation time unavailable for pid {pid}')
    milliseconds = (ticks - _FILETIME_UNIX_EPOCH) // 10000
    seconds, remainder = divmod(abs(milliseconds), 1000)
    sign = '-' if milliseconds < 0 else ''
    return f'{sign}{seconds}.{remainder:03d}'


def _kernel_process_rows() -> list[tuple[int, str]]:
    """Read process ids/names without starting PowerShell or consulting WMI."""
    import ctypes
    from ctypes import wintypes
    from hermes_cli.update_lock import _windows_kernel32

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
            ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
            ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
            ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', ctypes.c_long),
            ('dwFlags', wintypes.DWORD), ('szExeFile', ctypes.c_wchar * 260),
        ]

    kernel32 = _windows_kernel32()
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for walk in (kernel32.Process32FirstW, kernel32.Process32NextW):
        walk.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        walk.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        pytest.fail('Kernel process snapshot unavailable')
    rows = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            rows.append((int(entry.th32ProcessID), entry.szExeFile))
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES is the sole normal end.
            pytest.fail('Kernel process enumeration did not complete')
    finally:
        kernel32.CloseHandle(snapshot)
    return rows


def _select_protected_pid(rows, open_process, close_handle) -> int:
    names = ('csrss.exe', 'smss.exe', 'wininit.exe', 'services.exe', 'lsass.exe', 'MsMpEng.exe')
    for pid, name in rows:
        if name not in names:
            continue
        full = limited = None
        try:
            full = open_process(0x0400, False, pid)
            limited = open_process(0x1000, False, pid)
        finally:
            for handle in (full, limited):
                if handle:
                    close_handle(handle)
        if limited and not full:
            return pid
    pytest.fail('No process grants only limited query rights')


def _protected_pid() -> int:
    from hermes_cli.update_lock import _windows_kernel32

    kernel32 = _windows_kernel32()
    return _select_protected_pid(_kernel_process_rows(), kernel32.OpenProcess, kernel32.CloseHandle)


def _alive(pid: int) -> bool:
    return str(pid) in subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                                      capture_output=True, text=True, check=False).stdout


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    return proc.pid


def _env(home: Path, **extra: str) -> dict[str, str]:
    return {**os.environ, 'HERMES_HOME': str(home), 'HERMES_RUNTIME_DIR': str(home / 'empty-store'), **extra}


def _script(home: Path, *args: str, install: Path | None = None, **env: str) -> subprocess.Popen:
    return subprocess.Popen(
        [POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT),
         '-InstallRoot', str(install or home / 'hermes-agent'), '-NoUi', *args],
        cwd=home, env=_env(home, **env), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _finish(proc: subprocess.Popen, timeout: int = 120) -> tuple[int, str]:
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                           capture_output=True, check=False, timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()  # Only the Popen owned by this test; no unrelated PID discovery.
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                out, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pytest.fail('hand-off process output did not close after bounded cleanup')
        pytest.fail(f'hand-off did not finish within {timeout}s: {out}')
    return proc.returncode, out


def _op(home: Path, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT),
         '-InstallRoot', str(home / 'hermes-agent'), *args],
        cwd=home, env=_env(home), capture_output=True, text=True, timeout=120, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _log(home: Path) -> str:
    """The hand-off log so far. windows.ps1 appends with Add-Content, which holds the file without
    read sharing for the length of each write: a read landing in that window is a sharing
    violation (PermissionError), not a missing line, so it is retried."""
    path = home / 'logs/desktop-update-handoff.log'
    for _ in range(50):
        try:
            return path.read_text(encoding='utf-8-sig') if path.exists() else ''
        except PermissionError:
            time.sleep(0.1)
    return path.read_text(encoding='utf-8-sig')


def _custodian(home: Path, handoff: int) -> str:
    """The pid windows.ps1 names on line 1 before any update work starts: its custodian, which
    outlives the hand-off so an old Desktop never reads a dead owner ('' until it is named)."""
    found = re.search(rf'names its custodian pid (\d+) \(hand-off pid {handoff}\)', _log(home))
    return found.group(1) if found else ''


class _HeldLock:
    """Hold the A7 sidecar lock from another process (this one): an open handle
    conflicts with the script's FileShare.None open."""

    def __init__(self, home: Path) -> None:
        deadline = time.monotonic() + 30
        while True:
            try:
                self._f = open(home / (MARKER + '.lock'), 'a+b')   # windows-footgun: ok — binary mode
                return
            except PermissionError:   # the script holds it right now
                assert time.monotonic() < deadline, 'never got the marker lock'
                time.sleep(0.02)

    def release(self) -> None:
        self._f.close()


def _wait_for_log(home: Path, needle: str, timeout: int = 90) -> str:
    deadline = time.monotonic() + timeout
    while needle not in _log(home):
        assert time.monotonic() < deadline, f'{needle!r} never logged: {_log(home)}'
        time.sleep(0.1)
    return _log(home)
