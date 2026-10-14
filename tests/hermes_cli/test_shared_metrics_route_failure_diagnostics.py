"""Optional metric route diagnostics preserve adapter identity and cache causes."""

import logging
from types import SimpleNamespace

import pytest

from agent import surface_switch
from hermes_cli import plugin_host_diagnostics
from hermes_cli.observability import shared_metrics_efficiency as efficiency
from hermes_cli.observability import shared_metrics_gateway as gateway

_PRIVATE = "private-route-metric-marker"


def _fail(*_args, **_kwargs):
    try:
        raise ValueError(_PRIVATE)
    except ValueError as cause:
        raise RuntimeError(_PRIVATE) from cause


def _interrupt(*_args, **_kwargs):
    raise KeyboardInterrupt("original interrupt")


def _warning(caplog, module, boundary, diagnostics):
    records = [r for r in caplog.records if r.name == module.__name__ and r.levelno >= logging.WARNING]
    if not diagnostics:
        assert records == []
        return
    assert len(records) == 1
    text = logging.Formatter("%(message)s").format(records[0])
    assert boundary in text and "Traceback" in text
    assert _PRIVATE not in text and "in _fail" not in text
    assert records[0].exc_info[1].__cause__ is None
    assert records[0].exc_info[1].__context__ is None


@pytest.mark.parametrize("diagnostics", [True, False])
def test_fronted_route_failure_keeps_original_adapter(monkeypatch, caplog, diagnostics):
    adapter = SimpleNamespace(platform=SimpleNamespace(value="relay"), _metrics_platform=_fail)
    before = vars(adapter).copy()
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=gateway.__name__):
        assert gateway._source_platform(adapter, _PRIVATE) is adapter
    assert vars(adapter) == before
    _warning(caplog, gateway, "_source_platform", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_identity_lookup_failure_keeps_prompt_rebuild_cause(monkeypatch, caplog, diagnostics):
    agent = SimpleNamespace(model="model", provider="provider")
    before = vars(agent).copy()
    recorded = []
    monkeypatch.setattr(surface_switch, "identity_line_value", _fail)
    monkeypatch.setattr(efficiency, "record_cache_break", lambda original, cause: recorded.append((original, cause)))
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=efficiency.__name__):
        assert efficiency.record_prompt_rebuild(agent, _PRIVATE, "stale_runtime", "new") is None
    assert recorded == [(agent, "system_prompt_rebuild")] and recorded[0][0] is agent
    assert vars(agent) == before
    _warning(caplog, efficiency, "record_prompt_rebuild", diagnostics)


def test_healthy_fronted_route_keeps_resolver_identity():
    routed = object()
    adapter = SimpleNamespace(platform="relay", _metrics_platform=lambda _chat: routed)
    assert gateway._source_platform(adapter, "fixture") is routed
    assert gateway._source_platform(adapter, None) is adapter
    ordinary = SimpleNamespace(platform="feishu", _metrics_platform=_fail)
    assert gateway._source_platform(ordinary, "fixture") is ordinary


def test_normal_identity_switch_retains_model_switch_cause(monkeypatch):
    agent = SimpleNamespace(model="new", provider="provider")
    recorded = []
    monkeypatch.setattr(surface_switch, "identity_line_value", lambda _prompt, label: {"Model": "old", "Provider": "provider"}[label])
    monkeypatch.setattr(efficiency, "record_cache_break", lambda original, cause: recorded.append((original, cause)))
    efficiency.record_prompt_rebuild(agent, "old", "stale_runtime", "new")
    assert recorded == [(agent, "model_switch")]
    efficiency.record_prompt_rebuild(agent, "same", "stale_runtime", "same")
    assert recorded == [(agent, "model_switch")]


@pytest.mark.parametrize("boundary", ["fronted", "identity"])
def test_optional_route_boundaries_do_not_swallow_interrupt(monkeypatch, boundary):
    if boundary == "fronted":
        adapter = SimpleNamespace(platform="relay", _metrics_platform=_interrupt)
        with pytest.raises(KeyboardInterrupt, match="original interrupt"):
            gateway._source_platform(adapter, "fixture")
    else:
        monkeypatch.setattr(surface_switch, "identity_line_value", _interrupt)
        with pytest.raises(KeyboardInterrupt, match="original interrupt"):
            efficiency.record_prompt_rebuild(SimpleNamespace(), "old", "stale_runtime", "new")
