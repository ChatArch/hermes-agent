"""Shared real Windows protocol harness; not a collected test module."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from tests.scripts.desktop_update.windows_handoff_support import _finish

ROOT = Path(__file__).resolve().parent.parent.parent.parent

SCRIPT = ROOT / 'scripts/desktop-update/windows.ps1'

MARKER = '.hermes-update-in-progress'

CLI = """
import sys
from pathlib import Path
def main():
    if '--version' in sys.argv:
        print('Install directory: ' + str(Path(__file__).resolve().parents[1])); return 0
    if '--help' in sys.argv:
        print('--keep-stash'); return 0
    return 0
if __name__ == '__main__':
    sys.exit(main())
"""

@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
    yield proc
    proc.kill()
    proc.wait()

def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    return proc.pid

def _run(home: Path, *args: str, install: Path | None = None, timeout: int = 120):
    command = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT),
               '-InstallRoot', str(install or home / 'hermes-agent'), '-NoUi', *args]
    env = {**os.environ, 'HERMES_HOME': str(home), 'HERMES_RUNTIME_DIR': str(home / 'empty-store')}
    proc = subprocess.Popen(command, cwd=home, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    _code, out = _finish(proc, timeout=timeout)
    return proc.pid, proc.returncode, out

def _result(home: Path) -> dict:
    return json.loads((home / '.hermes-update-result.json').read_text(encoding='utf-8-sig'))

_BODIES = {   # contract A2: identical verdicts in every reader
    'crlf-bom-v2': ('\ufeff{pid}\r\n{now}\r\nct:{ct}\r\n', 'live'),
    'missing-line-2': ('{pid}\n', 'dead'),
    'garbled-line-2': ('{pid}\nsoon\nct:{ct}\n', 'dead'),
    'fractional-started-at': ('{pid}\n{now}.5\nct:{ct}\n', 'dead'),
    'garbled-ct-is-v1-fresh': ('{pid}\n{now}\nct:garbage\n', 'live'),
    'garbled-ct-is-v1-past-20min': ('{pid}\n{old}\nct:garbage\n', 'dead'),
    'delegate-without-ct-is-ignored': ('999999\n{now}\nct:1.000\ndelegate:{pid}\n', 'dead'),
}

HOLD_CLI = """
import os, sys, time
from pathlib import Path
def main():
    if '--version' in sys.argv:
        print('Install directory: ' + str(Path(__file__).resolve().parents[1])); return 0
    if '--help' in sys.argv:
        print('--keep-stash'); return 0
    if sys.argv[1:2] == ['update']:   # still starting up: report the pid, wait to be released
        hold = Path(os.environ['HANDOFF_HOLD'])
        Path(str(hold) + '.pid').write_text(str(os.getpid()), encoding='utf-8')
        while not hold.exists():
            time.sleep(0.05)
    return 0
if __name__ == '__main__':
    sys.exit(main())
"""

def _marker_lines(path: Path) -> list[str]:
    try:   # the script may be mid-replace (sharing violation): read again next poll
        return path.read_bytes().decode().splitlines()
    except OSError:
        return []

def _parent_pid(pid: int) -> int:
    out = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').ParentProcessId"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip()
    return int(out or 0)
