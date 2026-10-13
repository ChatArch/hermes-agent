"""Support-redaction and doctor diagnostics must not expose failed private input."""

import logging

import pytest

from hermes_cli import debug_redaction, doctor_config, plugin_host_diagnostics
from agent import relay_runtime

_PRIVATE_CANARY = "private-support-diagnostic-payload-marker"


def _fail(*_args, **_kwargs):
    try:
        raise ValueError(_PRIVATE_CANARY)
    except ValueError as cause:
        raise RuntimeError(_PRIVATE_CANARY) from cause


def _check_warning(caplog, module_name, boundary, diagnostics):
    records = [r for r in caplog.records if r.name == module_name
               and r.levelno >= logging.WARNING]
    if not diagnostics:
        assert not records
        return
    assert len(records) == 1
    record = records[0]
    assert record.exc_info and record.exc_info[2] is not None
    text = logging.Formatter("%(message)s").format(record)
    assert "Traceback" in text and boundary in text
    assert _PRIVATE_CANARY not in text and "in _fail" not in text
    assert record.exc_info[1].__cause__ is None and record.exc_info[1].__context__ is None


@pytest.mark.parametrize("diagnostics", [True, False])
@pytest.mark.parametrize("max_chars", [None, 4])
def test_redaction_failure_keeps_full_fail_closed_sentinel(monkeypatch, caplog, diagnostics, max_chars):
    monkeypatch.setattr(debug_redaction, "redact_sensitive_text", _fail)
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=debug_redaction.__name__):
        result = debug_redaction.redact_debug_support_text(_PRIVATE_CANARY, max_chars=max_chars)
    assert result == debug_redaction.REDACTION_UNAVAILABLE
    assert _PRIVATE_CANARY not in result
    _check_warning(caplog, debug_redaction.__name__, "redact_debug_support_text", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_relay_check_failure_keeps_manual_issue_and_existing_report(monkeypatch, caplog, capsys, diagnostics):
    monkeypatch.setattr(relay_runtime, "resolve_plugin_sources", _fail)
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=doctor_config.__name__):
        finding = doctor_config._check_relay_plugins(False)
    assert finding.fixed == 0 and finding.issues == []
    assert finding.manual_issues == ["Fix the Relay plugin configuration shown under NeMo Relay Plugins."]
    report = capsys.readouterr().out
    assert "Relay plugin configuration could not be read" in report
    assert "Hermes runs without Relay plugins" in report
    _check_warning(caplog, doctor_config.__name__, "_check_relay_plugins", diagnostics)


def test_missing_relay_keeps_platform_not_available_result(monkeypatch, capsys):
    def missing():
        raise ModuleNotFoundError("optional relay absent", name="nemo_relay")

    monkeypatch.setattr(relay_runtime, "resolve_plugin_sources", missing)
    finding = doctor_config._check_relay_plugins(False)
    assert finding.issues == [] and finding.manual_issues == [] and finding.fixed == 0
    assert "NeMo Relay is not available on this platform" in capsys.readouterr().out


def test_healthy_redaction_keeps_public_message():
    assert debug_redaction.redact_debug_support_text("public message", max_chars=6) == "public"
