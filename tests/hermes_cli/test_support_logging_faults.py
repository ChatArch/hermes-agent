"""Secondary diagnostic failures must neither leak private chains nor replace recovery."""

import builtins
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from agent import relay_runtime
from hermes_cli import debug_redaction, diagnostic_logging, doctor_config, plugin_host_diagnostics

_PRIVATE_CANARY = "private-support-diagnostic-payload-marker"


def _fail(*_args, **_kwargs):
    try:
        raise ValueError(_PRIVATE_CANARY)
    except ValueError as cause:
        raise RuntimeError(_PRIVATE_CANARY) from cause


def _call(boundary, monkeypatch):
    if boundary == "redactor":
        monkeypatch.setattr(debug_redaction, "redact_sensitive_text", _fail)
        assert debug_redaction.redact_debug_support_text(_PRIVATE_CANARY, max_chars=4) == debug_redaction.REDACTION_UNAVAILABLE
    else:
        monkeypatch.setattr(relay_runtime, "resolve_plugin_sources", _fail)
        finding = doctor_config._check_relay_plugins(False)
        assert finding.fixed == 0 and finding.issues == []
        assert finding.manual_issues == ["Fix the Relay plugin configuration shown under NeMo Relay Plugins."]


@pytest.mark.parametrize("boundary", ["redactor", "relay"])
@pytest.mark.parametrize("fault", ["file-open", "format", "filter"])
def test_broken_standard_logging_keeps_private_recovery(boundary, fault, monkeypatch, capsys, tmp_path):
    module = debug_redaction if boundary == "redactor" else doctor_config
    logger = logging.getLogger(module.__name__)
    previous = logger.handlers, logger.level, logger.propagate, logger.disabled
    if fault == "file-open":
        handler = logging.FileHandler(tmp_path, delay=True)
    else:
        handler = logging.StreamHandler()
        if fault == "format":
            handler.setFormatter(logging.Formatter("%(missing)s"))
        else:
            class BrokenFilter(logging.Filter):
                def filter(self, _record):
                    raise Exception(_PRIVATE_CANARY)
            handler.addFilter(BrokenFilter())
    logger.handlers, logger.propagate, logger.disabled = [handler], False, False
    logger.setLevel(logging.WARNING)
    monkeypatch.setattr(logging, "raiseExceptions", True)
    try:
        _call(boundary, monkeypatch)
        captured = capsys.readouterr()
        assert _PRIVATE_CANARY not in captured.err
        if boundary == "relay":
            assert "Relay plugin configuration could not be read" in captured.out
    finally:
        logger.handlers, logger.level, logger.propagate, logger.disabled = previous
        handler.close()


@pytest.mark.parametrize("boundary", ["redactor", "relay"])
def test_private_traceback_helper_failure_keeps_recovery(boundary, monkeypatch, capsys):
    monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", _fail)
    _call(boundary, monkeypatch)
    assert _PRIVATE_CANARY not in capsys.readouterr().err


@pytest.mark.parametrize("boundary", ["redactor", "relay"])
@pytest.mark.parametrize("exception_type", [ModuleNotFoundError, RuntimeError])
def test_diagnostic_import_failure_keeps_recovery(boundary, exception_type, monkeypatch, capsys):
    original_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "hermes_cli.plugin_host_diagnostics":
            raise exception_type(_PRIVATE_CANARY)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    _call(boundary, monkeypatch)
    assert _PRIVATE_CANARY not in capsys.readouterr().err


@pytest.mark.parametrize("boundary", ["redactor", "relay"])
def test_diagnostic_shield_leaves_original_cause_untouched(boundary, monkeypatch, caplog):
    original = RuntimeError(_PRIVATE_CANARY)
    cause = ValueError(_PRIVATE_CANARY)

    def fail(*_args, **_kwargs):
        raise original from cause

    module = debug_redaction if boundary == "redactor" else doctor_config
    with monkeypatch.context() as operation:
        if boundary == "redactor":
            operation.setattr(debug_redaction, "redact_sensitive_text", fail)
            call = lambda: debug_redaction.redact_debug_support_text(_PRIVATE_CANARY)
        else:
            operation.setattr(relay_runtime, "resolve_plugin_sources", fail)
            call = lambda: doctor_config._check_relay_plugins(False)
        with caplog.at_level(logging.WARNING, logger=module.__name__):
            call()
    assert original.__cause__ is cause and original.__context__ is None


def test_diagnostic_sink_imports_without_site_or_optional_helper(tmp_path):
    copy = tmp_path / "diagnostic_logging.py"
    copy.write_text(Path(diagnostic_logging.__file__).read_text())
    code = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('diagnostic_logging', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    raise RuntimeError('private-support-diagnostic-payload-marker')
except RuntimeError as exc:
    module.PrivateDiagnosticLogger('isolated-private-diagnostic').warning('Fixed diagnostic', exc_info=exc)
print('closed')
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(copy)],
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0 and result.stdout.strip() == "closed"
    assert _PRIVATE_CANARY not in result.stderr
