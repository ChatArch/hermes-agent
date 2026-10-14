"""Broken plugin discovery cannot hide diagnostics or block bundled languages."""

import logging

import pytest

from hermes_cli import config_language, plugins

_PRIVATE_CANARY = "private-language-plugin-marker"


def test_broken_plugin_discovery_keeps_bundled_language_and_private_diagnostic(monkeypatch, caplog):
    def broken_discovery():
        try:
            raise ValueError(_PRIVATE_CANARY)
        except ValueError as exc:
            raise RuntimeError(_PRIVATE_CANARY) from exc

    monkeypatch.setattr(plugins, "discover_plugins", broken_discovery)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.config_language"):
        assert config_language.resolve_display_language("en") == "en"
        assert config_language.display_language_error("en") is None
    records = [r for r in caplog.records if r.name == "hermes_cli.config_language"
               and r.levelno >= logging.WARNING]
    assert len(records) == 2
    for record in records:
        assert record.exc_info and record.exc_info[2] is not None
        rendered = logging.Formatter("%(message)s").format(record)
        assert _PRIVATE_CANARY not in rendered
        assert "Traceback" in rendered and "resolve_display_language" in rendered
        assert "broken_discovery" not in rendered
        assert record.exc_info[1].__cause__ is None and record.exc_info[1].__context__ is None


def test_diagnostic_unavailable_does_not_block_bundled_language(monkeypatch):
    monkeypatch.setattr(plugins, "discover_plugins", lambda: (_ for _ in ()).throw(RuntimeError(_PRIVATE_CANARY)))
    monkeypatch.setattr(config_language, "private_exc_info", lambda _exc: None, raising=False)
    assert config_language.resolve_display_language("en") == "en"


def test_successful_discovery_is_invoked_before_language_resolution(monkeypatch):
    calls = []
    monkeypatch.setattr(plugins, "discover_plugins", lambda: calls.append("discovered"))
    assert config_language.resolve_display_language("en") == "en"
    assert calls == ["discovered"]
