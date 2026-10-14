"""Status failures stay local to their row without exposing private payloads."""

import logging
from types import SimpleNamespace

import pytest

from hermes_cli import plugin_host_diagnostics
from hermes_cli import status

_PRIVATE = "private-status-diagnostic-marker"


def _fail(*_args, **_kwargs):
    try:
        raise ValueError(_PRIVATE)
    except ValueError as cause:
        raise RuntimeError(_PRIVATE) from cause


def _check(caplog, boundary, diagnostics):
    rows = [r for r in caplog.records if r.name == status.__name__ and r.levelno >= logging.WARNING]
    if not diagnostics:
        assert rows == []
        return
    assert len(rows) == 1
    rendered = logging.Formatter("%(message)s").format(rows[0])
    assert boundary in rendered and "Traceback" in rendered
    assert _PRIVATE not in rendered and "in _fail" not in rendered
    assert rows[0].exc_info[1].__cause__ is None and rows[0].exc_info[1].__context__ is None


@pytest.mark.parametrize("diagnostics", [True, False])
@pytest.mark.parametrize("boundary", ["config", "cron", "row", "summary"])
def test_status_failure_keeps_original_fallback(tmp_path, monkeypatch, caplog, diagnostics, boundary):
    captured = []
    monkeypatch.setattr(status, "_kv", lambda label, value, *_args: captured.append((label, value)))
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=status.__name__):
        if boundary == "config":
            marker = object()
            ctx = SimpleNamespace(config=marker, unrelated=marker)
            monkeypatch.setattr(status, "load_config", _fail)
            assert status._load_ctx_config(ctx) is None
            assert ctx.config == {} and ctx.unrelated is marker
            catching = "_load_ctx_config"
        elif boundary == "cron":
            (tmp_path / "cron").mkdir()
            (tmp_path / "cron/jobs.json").write_text("{}", encoding="utf-8")
            monkeypatch.setattr(status, "get_hermes_home", lambda: tmp_path)
            monkeypatch.setattr(status, "_load_json", _fail)
            assert status._cron_summary() == "(error reading jobs file)"
            catching = "_cron_summary"
        elif boundary == "row":
            assert status._summary_row("Fixture:", _fail, "empty") is None
            assert captured == [("Fixture:", "unknown")]
            catching = "_summary_row"
        else:
            monkeypatch.setattr(status, "_render_header", lambda _ctx: None)
            monkeypatch.setattr(status, "_banner", lambda *_args: None)
            monkeypatch.setattr(status, "load_config", dict)
            monkeypatch.setattr(status, "_effective_provider_label", lambda: "fixture")
            monkeypatch.setattr(status, "_summary_row", lambda *_args: None)
            monkeypatch.setattr(status, "_cron_summary", lambda: "0")
            monkeypatch.setattr(status, "_gateway_state", _fail)
            assert status._render_summary(SimpleNamespace(config={})) is None
            assert ("Gateway:", "unknown") in captured
            assert ("Jobs:", "0") in captured
            catching = "_render_summary"
    _check(caplog, catching, diagnostics)


def test_status_successes_keep_types_and_order(tmp_path, monkeypatch):
    config = {"model": "fixture"}
    monkeypatch.setattr(status, "load_config", lambda: config)
    ctx = SimpleNamespace(config=None)
    status._load_ctx_config(ctx)
    assert ctx.config is config
    captured = []
    monkeypatch.setattr(status, "_kv", lambda *args: captured.append(args))
    status._summary_row("Fixture:", lambda: ["b", "a", "b"], "empty")
    assert captured == [("Fixture:", "b, a")]
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron/jobs.json").write_text('{"jobs": [{"enabled": true}, {"enabled": false}, {}]}', encoding="utf-8")
    monkeypatch.setattr(status, "get_hermes_home", lambda: tmp_path)
    assert status._cron_summary() == "2 active, 3 total"
