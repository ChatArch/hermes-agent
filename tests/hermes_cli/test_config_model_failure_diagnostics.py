"""Config and model discovery keep their original safe fallbacks with private diagnostics."""

import logging

import pytest

from hermes_cli import config, model_setup_flows_local as local, models_pricing as pricing
from hermes_cli import plugin_host_diagnostics

_PRIVATE_CANARY = "private-config-model-boundary-payload-marker"


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
    rendered = logging.Formatter("%(message)s").format(record)
    assert "Traceback" in rendered and boundary in rendered
    assert _PRIVATE_CANARY not in rendered and "in _fail" not in rendered
    assert record.exc_info[1].__cause__ is None and record.exc_info[1].__context__ is None


def _disable_diagnostics(monkeypatch, diagnostics):
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_failed_user_manifest_scan_keeps_bundled_metadata(monkeypatch, caplog, diagnostics):
    core_name = next(name for name in config.CORE_DECLARED_ENV_NAMES if name in config.OPTIONAL_ENV_VARS)
    manifest = {"name": "bundled-demo", "kind": "platform",
                "requires_env": ["BOUNDARY_PLATFORM_TOKEN", core_name]}

    def manifests(_home=None, source="user", **_kwargs):
        if source == "user":
            _fail()
        return iter([("bundled-demo", manifest)])

    original_core = dict(config.OPTIONAL_ENV_VARS[core_name])
    monkeypatch.setattr(config, "OPTIONAL_ENV_VARS", dict(config.OPTIONAL_ENV_VARS))
    monkeypatch.setattr(config, "_platform_plugin_manifests", manifests)
    _disable_diagnostics(monkeypatch, diagnostics)
    with caplog.at_level(logging.WARNING, logger=config.__name__):
        names = config._inject_platform_plugin_env_vars()
    assert names is not None and "BOUNDARY_PLATFORM_TOKEN" in names
    assert core_name not in names
    assert config.OPTIONAL_ENV_VARS["BOUNDARY_PLATFORM_TOKEN"]["password"] is True
    assert config.OPTIONAL_ENV_VARS[core_name] == original_core
    _check_warning(caplog, config.__name__, "_inject_platform_plugin_env_vars", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_local_engine_discovery_failure_keeps_unavailable_result(monkeypatch, caplog, diagnostics):
    from hermes_cli.local_runtime import binaries

    monkeypatch.setattr(binaries, "unavailable_reason", _fail)
    _disable_diagnostics(monkeypatch, diagnostics)
    with caplog.at_level(logging.WARNING, logger=local.__name__):
        assert local.local_models_available() is False
    _check_warning(caplog, local.__name__, "local_models_available", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_provider_identity_failure_cannot_enable_custom_pricing(monkeypatch, caplog, diagnostics):
    from agent import model_metadata

    monkeypatch.setattr(model_metadata, "_infer_provider_from_url", _fail)
    _disable_diagnostics(monkeypatch, diagnostics)
    with caplog.at_level(logging.WARNING, logger=pricing.__name__):
        assert pricing.resolve_pricing_provider(
            "custom:openrouter", base_url="https://openrouter.ai/api/v1",
        ) == ""
    _check_warning(caplog, pricing.__name__, "resolve_pricing_provider", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_cached_auth_failure_keeps_endpoint_unknown(monkeypatch, caplog, diagnostics):
    from hermes_cli import auth

    monkeypatch.setattr(auth, "_load_auth_store", _fail)
    _disable_diagnostics(monkeypatch, diagnostics)
    with caplog.at_level(logging.WARNING, logger=pricing.__name__):
        assert pricing.get_cached_nous_inference_base_url() == ""
    _check_warning(caplog, pricing.__name__, "get_cached_nous_inference_base_url", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_failed_endpoint_override_keeps_default_scope(monkeypatch, caplog, diagnostics):
    from hermes_cli import auth

    monkeypatch.setattr(auth, "_nous_inference_env_override", _fail)
    monkeypatch.setattr(pricing, "get_cached_nous_inference_base_url", lambda: "")
    monkeypatch.setattr(pricing, "_pricing_provider_cache_keys", {})
    _disable_diagnostics(monkeypatch, diagnostics)
    with caplog.at_level(logging.WARNING, logger=pricing.__name__):
        assert pricing.pricing_cache_scope("nous") == pricing._DEFAULT_NOUS_INFERENCE_BASE
    _check_warning(caplog, pricing.__name__, "pricing_cache_scope", diagnostics)


@pytest.mark.parametrize("base_url,expected", [
    ("https://openrouter.ai/api/v1", "openrouter"),
    ("https://openrouter.ai.attacker.invalid/api/v1", ""),
])
def test_custom_pricing_still_requires_official_endpoint(base_url, expected):
    assert pricing.resolve_pricing_provider("custom:openrouter", base_url=base_url) == expected
