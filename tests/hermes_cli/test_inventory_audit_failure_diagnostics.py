"""Metadata diagnostics preserve existing featured and manifest fallbacks."""

import builtins
import logging

import pytest

from hermes_cli import inventory, plugin_host_diagnostics, plugin_isolation_audit

_PRIVATE_CANARY = "private-metadata-diagnostic-payload-marker"


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


@pytest.mark.parametrize("failed_import", ["agent.models_dev", "hermes_cli.providers"])
@pytest.mark.parametrize("diagnostics", [True, False])
def test_featured_import_failure_keeps_curated_or_unfeatured_rows(monkeypatch, caplog, failed_import, diagnostics):
    original_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == failed_import:
            _fail()
        return original_import(name, *args, **kwargs)

    models = [f"lab{i % 2}/model-{i}" for i in range(14)]
    row = {"slug": "openrouter", "models": models, "is_user_defined": False}
    explicit = {"slug": "custom:lab", "models": models.copy(), "is_user_defined": True}
    rows = [row, explicit]
    original_rows = rows.copy()
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    monkeypatch.setattr(builtins, "__import__", blocked)
    with caplog.at_level(logging.WARNING, logger=inventory.__name__):
        result = inventory._apply_featured(rows, metadata_config={"provider": "local-fixture"})
    assert result is None and rows == original_rows and rows[0] is row and rows[1] is explicit
    assert row["models"] is models and explicit["models"] == models
    assert explicit["featured_models"] == []
    assert row["featured_models"] == (models[:10] if failed_import == "agent.models_dev" else [])
    _warnings(caplog, inventory.__name__, "_apply_featured", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_manifest_parse_failure_keeps_static_audit_fallback(tmp_path, monkeypatch, caplog, diagnostics):
    import hermes_yaml

    (tmp_path / "plugin.yaml").write_text("kind: platform\n")
    (tmp_path / "__init__.py").write_text("def register(ctx):\n    pass\n")
    monkeypatch.setattr(hermes_yaml, "safe_load", _fail)
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=plugin_isolation_audit.__name__):
        report = plugin_isolation_audit.audit_plugin_dir(tmp_path)
    assert report.verdict == plugin_isolation_audit.VERDICT_HOST
    assert report.reasons == [] and report.notes == [] and report.ctx_methods == set()
    _warnings(caplog, plugin_isolation_audit.__name__, "_read_manifest", diagnostics)


def test_missing_manifest_is_quiet_and_portable(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=plugin_isolation_audit.__name__):
        report = plugin_isolation_audit.audit_plugin_dir(tmp_path)
    assert report.verdict == plugin_isolation_audit.VERDICT_PORTABLE
    assert not caplog.records


def test_valid_platform_manifest_keeps_in_process_requirement(tmp_path):
    (tmp_path / "plugin.yaml").write_text("kind: platform\n")
    (tmp_path / "__init__.py").write_text("def register(ctx):\n    pass\n")
    report = plugin_isolation_audit.audit_plugin_dir(tmp_path)
    assert report.verdict == plugin_isolation_audit.VERDICT_IN_PROCESS
    assert any("platform adapters" in reason for reason in report.reasons)


@pytest.mark.parametrize("failed_import,symbol", [("agent.models_dev", "get_model_info"),
                                                 ("hermes_cli.providers", "is_routing_aggregator")])
def test_public_models_payload_keeps_metadata_failure_fallback(monkeypatch, caplog, failed_import, symbol):
    from hermes_cli import config, model_switch

    models = [f"lab{i % 2}/model-{i}" for i in range(14)]
    row = {"slug": "openrouter", "name": "OpenRouter", "models": models,
           "is_current": True, "is_user_defined": False, "total_models": len(models)}
    monkeypatch.setattr(model_switch, "list_authenticated_providers", lambda **_kwargs: [row])
    monkeypatch.setattr(inventory, "_local_runtime_row", lambda _ctx: None)
    monkeypatch.setattr(inventory, "_moa_provider_row", lambda _provider: None)
    monkeypatch.setattr(config, "load_config_readonly", dict)
    original_import = builtins.__import__

    def blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if name == failed_import and symbol in fromlist:
            _fail()
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked)
    ctx = inventory.ConfigContext("openrouter", models[0], "", {}, [])
    with caplog.at_level(logging.WARNING, logger=inventory.__name__):
        payload = inventory.build_models_payload(ctx, featured=True, probe_custom_providers=False)
    assert payload["provider"] == "openrouter" and payload["model"] == models[0]
    assert payload["providers"] == [row] and payload["providers"][0] is row
    assert row["models"] == models
    assert row["featured_models"] == (models[:10] if failed_import == "agent.models_dev" else [])
    _warnings(caplog, inventory.__name__, "_apply_featured", True)
