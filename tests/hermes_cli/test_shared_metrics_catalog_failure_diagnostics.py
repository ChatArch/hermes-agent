"""Shared-metric catalog fallback diagnostics must not expose private names."""

import logging

import pytest

from agent import models_dev, skill_commands
from hermes_cli import commands, plugin_host_diagnostics, plugins
from hermes_cli.observability import shared_metrics_catalog as catalog

_PRIVATE_CANARY = "private-metric-catalog-payload-marker"


def _fail(*_args, **_kwargs):
    try:
        raise ValueError(_PRIVATE_CANARY)
    except ValueError as cause:
        raise RuntimeError(_PRIVATE_CANARY) from cause


@pytest.fixture(autouse=True)
def clear_provider_lookup_cache():
    catalog._models_dev_provider.cache_clear()
    yield
    catalog._models_dev_provider.cache_clear()


def _warnings(caplog, boundary, diagnostics):
    records = [r for r in caplog.records if r.name == catalog.__name__ and r.levelno >= logging.WARNING]
    if not diagnostics:
        assert not records
        return
    assert len(records) == 1
    record = records[0]
    text = logging.Formatter("%(message)s").format(record)
    assert boundary in text and "Traceback" in text
    assert _PRIVATE_CANARY not in text and "in _fail" not in text
    assert record.exc_info[1].__cause__ is None and record.exc_info[1].__context__ is None


@pytest.mark.parametrize("diagnostics", [True, False])
def test_core_command_failure_keeps_skill_fallback(monkeypatch, caplog, diagnostics):
    monkeypatch.setattr(commands, "resolve_command", _fail)
    monkeypatch.setattr(skill_commands, "resolve_skill_command_key", lambda _name: "fixture-skill")
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=catalog.__name__):
        result = catalog.slash_command_metric_name("/" + _PRIVATE_CANARY)
    assert result == "skill"
    _warnings(caplog, "slash_command_metric_name", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
@pytest.mark.parametrize("stage", ["skill", "plugin"])
def test_extension_lookup_failure_keeps_anonymous_unknown(monkeypatch, caplog, diagnostics, stage):
    monkeypatch.setattr(commands, "resolve_command", lambda _name: None)
    monkeypatch.setattr(skill_commands, "resolve_skill_command_key", _fail if stage == "skill" else lambda _name: None)
    monkeypatch.setattr(plugins, "get_plugin_command_handler", _fail)
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=catalog.__name__):
        result = catalog.slash_command_metric_name(_PRIVATE_CANARY)
    assert result == "unknown"
    _warnings(caplog, "slash_command_metric_name", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_provider_lookup_failure_stays_cached_custom_without_network(monkeypatch, caplog, diagnostics):
    calls = []

    def lookup(name, *, allow_network):
        calls.append((name, allow_network))
        _fail()

    monkeypatch.setattr(models_dev, "get_provider_info", lookup)
    monkeypatch.setattr(catalog, "provider_names", frozenset)
    monkeypatch.setattr(catalog, "custom_provider_aliases", frozenset)
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=catalog.__name__):
        first = catalog.provider_metric_name(_PRIVATE_CANARY)
        second = catalog.provider_metric_name(_PRIVATE_CANARY)
    assert first == second == "custom"
    assert calls == [(_PRIVATE_CANARY, False)]
    _warnings(caplog, "_models_dev_provider", diagnostics)


def test_known_command_and_plugin_names_keep_original_classification(monkeypatch):
    assert catalog.slash_command_metric_name("/help") == "help"
    monkeypatch.setattr(commands, "resolve_command", lambda _name: None)
    monkeypatch.setattr(skill_commands, "resolve_skill_command_key", lambda _name: None)
    monkeypatch.setattr(plugins, "get_plugin_command_handler", lambda _name: object())
    assert catalog.slash_command_metric_name(_PRIVATE_CANARY) == "plugin"


def test_blank_command_keeps_unknown_without_lookup(monkeypatch):
    monkeypatch.setattr(commands, "resolve_command", _fail)
    assert catalog.slash_command_metric_name("") == "unknown"
