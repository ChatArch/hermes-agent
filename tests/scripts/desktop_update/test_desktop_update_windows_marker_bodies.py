"""Real Windows handoff contract cases, grouped below the unchanged file deadline."""
from __future__ import annotations
from pathlib import Path
import subprocess
import time
import pytest
from tests.scripts.desktop_update.windows_handoff_support import _creation_time
import tests.scripts.desktop_update.windows_marker_support as _handoff_owner
from tests.scripts.desktop_update.windows_marker_support import MARKER, _BODIES, _run, sleeper
__all__ = ["sleeper"]  # Register the imported pytest fixture.
_HANDOFF_TRACE_OWNER = _handoff_owner

@pytest.mark.platforms('windows')
@pytest.mark.parametrize('case', sorted(_BODIES))
def test_marker_bodies_are_parsed_positionally_like_every_other_reader(
    tmp_path: Path, sleeper: subprocess.Popen, case: str,
) -> None:
    template, verdict = _BODIES[case]
    now = int(time.time())
    body = template.format(pid=sleeper.pid, now=now, old=now - 1300, ct=_creation_time(sleeper.pid)).encode()
    (tmp_path / MARKER).write_bytes(body)
    pid, code, out = _run(tmp_path, '-SelfTestMarker', '-NoMarkerCleanup')
    if verdict == 'live':
        assert code == 2, out
        assert (tmp_path / MARKER).read_bytes() == body
    else:
        assert code == 0, out
        assert (tmp_path / MARKER).read_bytes().decode().split('\n')[0] == str(pid)
