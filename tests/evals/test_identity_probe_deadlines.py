"""Eval identity probes must be bounded and stop before any scenario on failure."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from evals.native_compaction import ab_checkpoint_preflight as checkpoint
from evals.token_accounting import replay_gates as replay
from hermes_cli import _subprocess_compat as compat

_PRIVATE = "private-eval-identity-marker"


def _prepare(module, tmp_path, monkeypatch):
    output = tmp_path / "result.json"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "original-home"))
    monkeypatch.setattr(module.sys, "argv", ["eval", "--out", str(output)])
    monkeypatch.setattr(module, "tempfile", SimpleNamespace(mkdtemp=lambda **_kwargs: str(tmp_path / "scratch")))
    return output


@pytest.mark.parametrize("module", [checkpoint, replay], ids=["checkpoint", "replay"])
@pytest.mark.parametrize("failure", ["timeout", "spawn", "nonzero"])
def test_identity_failure_stops_before_wire_or_result(tmp_path, monkeypatch, capsys, module, failure):
    output = _prepare(module, tmp_path, monkeypatch)
    calls = []

    def unavailable(*_args, **_kwargs):
        calls.append("probe")
        return subprocess.CompletedProcess(["git"], 7, _PRIVATE, _PRIVATE) if failure == "nonzero" else None

    def old_run(*_args, **_kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(_PRIVATE, 5, output=_PRIVATE)
        if failure == "spawn":
            raise FileNotFoundError(_PRIVATE)
        return subprocess.CompletedProcess(["git"], 7, _PRIVATE, _PRIVATE)

    def forbidden_wire():
        pytest.fail("failed identity probe started the evaluation wire")

    monkeypatch.setattr(compat, "bounded_probe_run", unavailable)
    monkeypatch.setattr(subprocess, "run", old_run)
    monkeypatch.setattr(module, "_FakeResponses" if module is checkpoint else "_FakeChat", forbidden_wire)
    try:
        code = module.main()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pytest.fail("identity probe failure escaped instead of stopping cleanly")
    assert code == 1 and calls == ["probe"]
    assert not output.exists()
    assert not (tmp_path / "scratch").exists()
    assert module.os.environ["HERMES_HOME"] == str(tmp_path / "original-home")
    captured = capsys.readouterr()
    assert "identity probe" in captured.err.lower()
    assert _PRIVATE not in captured.out + captured.err


@pytest.mark.parametrize("module", [checkpoint, replay], ids=["checkpoint", "replay"])
def test_successful_identity_probe_keeps_head_and_closes_wire(tmp_path, monkeypatch, module):
    output = _prepare(module, tmp_path, monkeypatch)
    probes = []

    def probe(argv, **kwargs):
        probes.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, " fixture-head\n", "")

    closed = []
    wire = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(compat, "bounded_probe_run", probe)
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess(["git"], 0, "fixture-head\n", ""))
    if module is checkpoint:
        monkeypatch.setattr(module, "_FakeResponses", lambda: wire)
        monkeypatch.setattr(module, "scenario_capture", lambda *_args, **_kwargs: {"fixture": True})
        monkeypatch.setattr(module, "scenario_restore", lambda *_args, **_kwargs: {"fixture": True})
    else:
        monkeypatch.setattr(module, "_FakeChat", lambda: wire)
        monkeypatch.setattr(module, "scenario", lambda *_args, **kwargs: {"local_compress_calls_by_turn": [0, 1] if kwargs["chars"] == 400 else [0, 0]})
        monkeypatch.setattr(module, "recovery_scenario", lambda *_args, **_kwargs: {"pass": True})
    assert module.main() == 0
    assert json.loads(output.read_text())["head"] == "fixture-head"
    assert closed == [True] and len(probes) == 1
    assert probes[0][0] == ["git", "rev-parse", "HEAD"]
    assert probes[0][1]["cwd"] == module.ROOT and probes[0][1]["timeout"] == 5.0
