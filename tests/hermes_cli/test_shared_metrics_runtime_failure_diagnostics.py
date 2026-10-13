"""Runtime metric probes keep their existing optional fallbacks and state."""

import logging
import sys
from types import ModuleType, SimpleNamespace

import psutil
import pytest

from hermes_cli import plugin_host_diagnostics
from hermes_cli.observability import relay_shared_metrics
from hermes_cli.observability import shared_metrics_disabled as disabled
from hermes_cli.observability import shared_metrics_harness as harness
from hermes_cli.observability import shared_metrics_startup as startup
from tools import skill_provenance

_PRIVATE_CANARY = "private-metric-runtime-payload-marker"


def _fail(*_args, **_kwargs):
    try:
        raise ValueError(_PRIVATE_CANARY)
    except ValueError as cause:
        raise RuntimeError(_PRIVATE_CANARY) from cause


def _warnings(caplog, name, boundary, diagnostics):
    records = [r for r in caplog.records if r.name == name and r.levelno >= logging.WARNING]
    if not diagnostics:
        assert records == []
        return
    assert len(records) == 1
    record = records[0]
    text = logging.Formatter("%(message)s").format(record)
    assert boundary in text and "Traceback" in text
    assert _PRIVATE_CANARY not in text and "in _fail" not in text
    assert record.exc_info[1].__cause__ is None and record.exc_info[1].__context__ is None


@pytest.mark.parametrize("diagnostics", [True, False])
@pytest.mark.parametrize("probe", ["enabled", "background", "surface", "startup"])
def test_optional_runtime_probe_failure_preserves_exact_fallback(monkeypatch, caplog, diagnostics, probe):
    if probe == "enabled":
        monkeypatch.setattr(relay_shared_metrics, "enabled", _fail)
        module, call, expected, boundary = harness, harness._enabled, False, "_enabled"
    elif probe == "background":
        monkeypatch.setattr(skill_provenance, "is_background_review", _fail)
        module, call, expected, boundary = harness, harness._background_tool_call, False, "_background_tool_call"
    elif probe == "surface":
        fake = ModuleType("tui_gateway.server")
        fake._resolve_session_platform = _fail
        monkeypatch.setattr(disabled, "_process_surface", "tui_gateway")
        module, call, expected, boundary = disabled, disabled.current_surface, "tui", "current_surface"
    else:
        monkeypatch.setattr(psutil, "Process", _fail)
        module, call, expected, boundary = startup, startup.process_started_at, None, "process_started_at"
    recorded = startup._recorded.copy()
    lock = startup._lock
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with monkeypatch.context() as temporary:
        if probe == "surface":
            temporary.setitem(sys.modules, "tui_gateway.server", fake)
        with caplog.at_level(logging.WARNING, logger=module.__name__):
            result = call()
    assert result is expected if expected in (False, None) else result == expected
    assert startup._recorded == recorded and startup._lock is lock
    _warnings(caplog, module.__name__, boundary, diagnostics)


def test_successful_runtime_probes_keep_original_value_types(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(relay_shared_metrics, "enabled", lambda: sentinel)
    assert harness._enabled() is sentinel
    monkeypatch.setattr(skill_provenance, "is_background_review", lambda: True)
    assert harness._background_tool_call() is True
    monkeypatch.setattr(disabled, "_process_surface", "cli_config")
    assert disabled.current_surface() == "cli_config"
    monkeypatch.setattr(psutil, "Process", lambda _pid: SimpleNamespace(create_time=lambda: 42))
    assert startup.process_started_at() == 42.0
