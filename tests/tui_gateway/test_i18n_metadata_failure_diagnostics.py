"""Registered i18n failures retain the wire contract with payload-free diagnostics."""

import logging

import pytest

from agent import i18n
from hermes_cli import plugin_host_diagnostics

_PRIVATE = "private-i18n-metadata-marker"


def _failure():
    try:
        raise ValueError(_PRIVATE)
    except ValueError as cause:
        return RuntimeError(_PRIVATE).with_traceback(None), cause


def _records(caplog):
    return [r for r in caplog.records if r.name == "tui_gateway.methods_i18n" and r.levelno >= logging.WARNING]


@pytest.mark.parametrize("method", ["i18n.languages", "i18n.catalog"])
@pytest.mark.parametrize("stage", ["metadata", "ok"])
@pytest.mark.parametrize("diagnostics", [True, False])
def test_registered_failure_preserves_wire_and_private_chain(monkeypatch, caplog, method, stage, diagnostics):
    from tui_gateway import server

    error, cause = _failure()

    def fail(*_args, **_kwargs):
        raise error from cause

    target = "language_options" if method == "i18n.languages" else "surface_catalog"
    healthy = [] if method == "i18n.languages" else {}
    monkeypatch.setattr(i18n, target, lambda *_args: healthy)
    monkeypatch.setattr(i18n, "resolve_language_id", lambda _lang: "en")
    if stage == "metadata":
        monkeypatch.setattr(i18n, target, fail)
    else:
        monkeypatch.setattr(server, "_ok", fail)
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _error: None)
    with caplog.at_level(logging.WARNING, logger="tui_gateway.methods_i18n"):
        result = server.handle_request({"jsonrpc": "2.0", "id": 3, "method": method, "params": {}})
    assert result["error"] == {"code": 5400, "message": _PRIVATE}
    assert result["id"] == 3
    assert error.__cause__ is cause and error.__context__ is None
    records = _records(caplog)
    if not diagnostics:
        assert records == []
        return
    assert len(records) == 1
    rendered = logging.Formatter("%(message)s").format(records[0])
    assert ("Language metadata unavailable" if method.endswith("languages") else "Language catalog unavailable") in rendered
    assert "Traceback" in rendered and _PRIVATE not in rendered and "in fail" not in rendered
    assert records[0].exc_info[1].__cause__ is None and records[0].exc_info[1].__context__ is None


@pytest.mark.parametrize("method", ["i18n.languages", "i18n.catalog"])
def test_registered_success_keeps_payload_identity_and_server_logger(monkeypatch, caplog, method):
    from tui_gateway import server

    rid, sentinel = object(), object()
    payload = [] if method.endswith("languages") else {"fixture": "value"}
    target = "language_options" if method.endswith("languages") else "surface_catalog"
    monkeypatch.setattr(i18n, target, lambda *_args: payload)
    monkeypatch.setattr(i18n, "resolve_language_id", lambda _lang: "en")
    captured = []
    monkeypatch.setattr(server, "_ok", lambda actual_rid, data: captured.append((actual_rid, data)) or sentinel)
    original_logger = server.logger
    assert server._methods[method](rid, {}) is sentinel
    assert captured[0][0] is rid
    assert captured[0][1]["languages" if method.endswith("languages") else "messages"] is payload
    assert server.logger is original_logger and _records(caplog) == []


@pytest.mark.parametrize("method", ["i18n.languages", "i18n.catalog"])
@pytest.mark.parametrize("sink", ["file", "formatter"])
def test_broken_standard_sink_does_not_replace_wire_or_expose_chain(tmp_path, monkeypatch, capsys, method, sink):
    from tui_gateway import server

    error, cause = _failure()

    def fail(*_args):
        raise error from cause

    target = "language_options" if method.endswith("languages") else "surface_catalog"
    monkeypatch.setattr(i18n, target, fail)
    monkeypatch.setattr(i18n, "resolve_language_id", lambda _lang: "en")
    handler = logging.FileHandler(tmp_path, delay=True) if sink == "file" else logging.StreamHandler()
    if sink == "formatter":
        handler.setFormatter(logging.Formatter("%(missing_field)s"))
    logger = logging.getLogger("tui_gateway.methods_i18n")
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.WARNING)
    try:
        result = server.handle_request({"jsonrpc": "2.0", "id": 3, "method": method, "params": {}})
        assert result["error"] == {"code": 5400, "message": _PRIVATE}
        assert _PRIVATE not in capsys.readouterr().err
        assert error.__cause__ is cause and error.__context__ is None
    finally:
        handler.close()
