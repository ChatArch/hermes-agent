"""Handoff phase probes flush before work and never replace its result."""

import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from tests.scripts.desktop_update import windows_handoff_support as support


def _instrument(call, step="handoff"):
    factory = getattr(support, "trace_handoff_phase", None)
    assert callable(factory), "handoff work has no synchronous phase diagnostics"
    return factory(call, family="commit", step=step)


def test_phase_records_bracket_the_real_call_and_keep_result(monkeypatch, capsys):
    marker = object()
    observed = []
    clock = iter([12, 34])
    monkeypatch.setattr(support.time, "monotonic_ns", lambda: next(clock))

    def work():
        observed.append(capsys.readouterr().out)
        return marker

    assert _instrument(work)() is marker
    final = capsys.readouterr().out
    enter, leave = [json.loads(line) for line in [observed[0].strip(), final.strip()]]
    assert enter == {"handoff_phase": "handoff", "family": "commit", "edge": "enter", "monotonic_ns": 12}
    assert leave == {"handoff_phase": "handoff", "family": "commit", "edge": "exit", "monotonic_ns": 34}


def test_phase_keeps_exact_original_exception(capsys):
    error = RuntimeError("original handoff result")

    def work():
        raise error

    with pytest.raises(RuntimeError) as caught:
        _instrument(work)()
    assert caught.value is error
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["edge"] for row in rows] == ["enter", "exit"]
    assert "original handoff result" not in json.dumps(rows)


@pytest.mark.parametrize("failure", [BrokenPipeError, OSError, ValueError])
def test_closed_diagnostic_output_preserves_work(monkeypatch, failure):
    class Closed:
        def write(self, _text):
            raise failure("closed diagnostic stream")

        def flush(self):
            raise failure("closed diagnostic stream")

    marker = object()
    call = _instrument(lambda: marker)
    with monkeypatch.context() as temporary:
        temporary.setattr(sys, "stdout", Closed())
        assert call() is marker


def test_phase_passes_arguments_and_preserves_callable_metadata(capsys):
    def work(first, *, second):
        """Existing callable metadata."""
        return first, second

    call = _instrument(work, "prepare")
    assert call(1, second=2) == (1, 2)
    assert call.__name__ == work.__name__ and call.__doc__ == work.__doc__
    capsys.readouterr()


def test_phase_output_flushes_before_child_exits():
    factory = getattr(support, "trace_handoff_phase", None)
    assert callable(factory), "handoff work has no synchronous phase diagnostics"
    code = """from tests.scripts.desktop_update.windows_handoff_support import trace_handoff_phase
import sys
@trace_handoff_phase(family='commit', step='handoff')
def work():
    sys.stdout.write('PRIMARY\\n')
    raise SystemExit(7)
work()
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 7
    lines = result.stdout.splitlines()
    assert len(lines) == 3 and lines[1] == "PRIMARY"
    assert [json.loads(lines[i])["edge"] for i in (0, 2)] == ["enter", "exit"]


def test_phase_enter_survives_abrupt_child_exit():
    code = """from tests.scripts.desktop_update.windows_handoff_support import trace_handoff_phase
import os
@trace_handoff_phase(family='commit', step='handoff')
def work():
    os._exit(7)
work()
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 7
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(rows) == 1 and rows[0]["edge"] == "enter"


@pytest.mark.parametrize("abrupt", [False, True])
def test_real_runner_retains_phase_rows_after_file_failure(tmp_path, abrupt):
    fixture = tmp_path / "test_desktop_update_windows_commit_outcome.py"
    fixture.write_text("""def _handoff(): return 73
def _finish(): return 0
def publish_fixture_launcher(): return None
def test_phase_pipe():
    assert _handoff() == 73
    TERMINAL
""", encoding="utf-8")
    terminal = "import os; os._exit(7)" if abrupt else "raise AssertionError('primary file failure')"
    fixture.write_text(fixture.read_text().replace("TERMINAL", terminal), encoding="utf-8")
    (tmp_path / "conftest.py").write_text("""import pytest
from tests.scripts.desktop_update.windows_handoff_support import instrument_handoff_phases
@pytest.fixture(autouse=True)
def trace(request, monkeypatch):
    with instrument_handoff_phases(request, monkeypatch):
        yield
""", encoding="utf-8")
    result = subprocess.run(
        ["bash", (support.ROOT / "scripts/run_tests.sh").as_posix(), "-j", "1", "--file-retries", "0", fixture.as_posix()],
        cwd=support.ROOT, env={**os.environ, "HERMES_PYTHON": Path(sys.executable).as_posix()},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "=== Failure output ===" in result.stdout
    output = result.stdout.split("=== Failure output ===", 1)[1]
    rows = []
    for line in output.splitlines():
        start = line.find('{"handoff_phase":')
        if start >= 0:
            rows.append(json.loads(line[start:]))
    assert [row["edge"] for row in rows] == (["enter", "enter", "exit"] if abrupt else ["enter", "enter", "exit", "exit"])
    assert [row["handoff_phase"] for row in rows] == (["case", "handoff", "handoff"] if abrupt else ["case", "handoff", "handoff", "case"])


def test_unknown_family_is_rejected_before_work():
    factory = getattr(support, "trace_handoff_phase", None)
    assert callable(factory), "handoff work has no synchronous phase diagnostics"
    with pytest.raises(ValueError, match="phase"):
        factory(lambda: None, family="private-untrusted-name", step="handoff")


def test_unknown_step_is_rejected_before_work():
    factory = getattr(support, "trace_handoff_phase", None)
    assert callable(factory), "handoff work has no synchronous phase diagnostics"
    with pytest.raises(ValueError, match="phase"):
        factory(lambda: None, family="commit", step="private-untrusted-step")


def test_fixture_instruments_known_module_and_restores_seams(monkeypatch, capsys):
    from types import SimpleNamespace

    instrument = getattr(support, "instrument_handoff_phases", None)
    assert callable(instrument), "native handoff fixtures do not install phase probes"
    original = lambda: 73
    module = SimpleNamespace(
        __name__="tests.scripts.desktop_update.test_desktop_update_windows_commit_outcome",
        _handoff=original, _finish=original, publish_fixture_launcher=original,
    )
    events = []

    class Capture:
        def disabled(self):
            return self

        def __enter__(self):
            events.append("capture-disabled")

        def __exit__(self, *_args):
            events.append("capture-restored")

    request = SimpleNamespace(module=module, getfixturevalue=lambda name: Capture())
    with instrument(request, monkeypatch):
        assert events == ["capture-disabled"]
        assert module._handoff() == 73
        assert module._handoff is not original
    assert events == ["capture-disabled", "capture-disabled", "capture-restored", "capture-restored"]
    assert module._handoff is original and module._finish is original
    assert module.publish_fixture_launcher is original
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["edge"] for r in rows] == ["enter", "enter", "exit", "exit"]
    assert [r["handoff_phase"] for r in rows] == ["case", "handoff", "handoff", "case"]


def test_fixture_does_not_request_capture_for_other_test_modules(monkeypatch):
    from types import SimpleNamespace

    instrument = getattr(support, "instrument_handoff_phases", None)
    assert callable(instrument), "native handoff fixtures do not install phase probes"

    def forbidden(_name):
        raise AssertionError("unrelated capture fixture requested")

    request = SimpleNamespace(module=SimpleNamespace(__name__="tests.other"), getfixturevalue=forbidden)
    with instrument(request, monkeypatch):
        pass
