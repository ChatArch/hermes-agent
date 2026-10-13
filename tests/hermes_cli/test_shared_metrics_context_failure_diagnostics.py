"""Optional shared-metric context failures retain profile and result contracts."""

import logging
from pathlib import Path

import pytest

import hermes_constants
from hermes_cli import plugin_host_diagnostics
from hermes_cli.observability import relay_shared_metrics, shared_metrics_catalog
from hermes_cli.observability import shared_metrics_loop as loop
from hermes_cli.observability import shared_metrics_setup as setup

_PRIVATE_CANARY = "private-metric-context-payload-marker"


def _fail(*_args, **_kwargs):
    try:
        raise ValueError(_PRIVATE_CANARY)
    except ValueError as cause:
        raise RuntimeError(_PRIVATE_CANARY) from cause


def _warning(caplog, name, boundary, diagnostics):
    records = [r for r in caplog.records if r.name == name and r.levelno >= logging.WARNING]
    if not diagnostics:
        assert records == []
        return
    assert len(records) == 1
    info = records[0].exc_info
    text = logging.Formatter("%(message)s").format(records[0])
    assert boundary in text and "Traceback" in text
    assert _PRIVATE_CANARY not in text and "in _fail" not in text
    assert info[1].__cause__ is None and info[1].__context__ is None


@pytest.mark.parametrize("diagnostics", [True, False])
def test_collection_failure_restores_owning_profile(tmp_path, monkeypatch, caplog, diagnostics):
    before = hermes_constants.get_hermes_home()
    home = tmp_path / "target-profile"
    observed = []

    def enabled():
        observed.append(hermes_constants.get_hermes_home())
        _fail()

    monkeypatch.setattr(relay_shared_metrics, "enabled", enabled)
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=setup.__name__):
        assert setup.collection_enabled(home) is False
    assert Path(observed[0]) == home and hermes_constants.get_hermes_home() == before
    _warning(caplog, setup.__name__, "collection_enabled", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_resume_failure_retains_flow_object_and_false(tmp_path, monkeypatch, caplog, diagnostics):
    flow = setup.SetupFlow(str(tmp_path), "cli_model", "custom", tmp_path / "marker")
    before = vars(flow).copy()
    monkeypatch.setattr(shared_metrics_catalog, "provider_metric_name", _fail)
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=setup.__name__):
        assert setup._resumes(flow, "cli_model", _PRIVATE_CANARY) is False
    assert vars(flow) == before and not flow.marker.exists()
    _warning(caplog, setup.__name__, "_resumes", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_missing_home_retains_none(monkeypatch, caplog, diagnostics):
    monkeypatch.setattr(hermes_constants, "get_hermes_home", _fail)
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=loop.__name__):
        assert loop._current_home() is None
    _warning(caplog, loop.__name__, "_current_home", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_memory_origin_failure_keeps_foreground_order_and_batch_limit(monkeypatch, caplog, diagnostics):
    rows = []
    monkeypatch.setattr(loop, "_memory_origin", _fail)
    monkeypatch.setattr(loop, "_emit", lambda mark, build, **raw: rows.append((mark, raw)))
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    ops = [f"op-{i}" for i in range(loop._MAX_BATCH_OPS + 3)]
    with caplog.at_level(logging.WARNING, logger=loop.__name__):
        assert loop._record_memory_ops(ops, provider="builtin", outcome="success", failure_class="none") is None
    assert [row[1]["op"] for row in rows] == ops[:loop._MAX_BATCH_OPS]
    assert all(mark == "MEMORY_OP_MARK" and raw["origin"] == "foreground"
               and raw["provider"] == "builtin" and raw["outcome"] == "success"
               and raw["failure_class"] == "none" for mark, raw in rows)
    _warning(caplog, loop.__name__, "_record_memory_ops", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_browser_name_failure_keeps_primary_result_object(monkeypatch, caplog, diagnostics):
    primary = {"success": True}
    observed = []

    def record(kind, backend, result, *, error_class):
        observed.append((kind, backend(), result, error_class))

    monkeypatch.setattr(loop, "record_execution_backend", record)
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=loop.__name__):
        assert loop._safe_record_browser(_fail, primary, None) is None
    assert observed == [("browser", "other", primary, None)] and observed[0][2] is primary
    _warning(caplog, loop.__name__, "name", diagnostics)


def test_healthy_gate_and_home_values_unchanged(monkeypatch):
    monkeypatch.setattr(relay_shared_metrics, "enabled", lambda: 1)
    assert setup.collection_enabled() is True
    assert loop._current_home() == str(hermes_constants.get_hermes_home())


def test_same_provider_resume_retains_true(tmp_path, monkeypatch):
    flow = setup.SetupFlow(str(tmp_path), "cli_model", "custom", tmp_path / "marker")
    monkeypatch.setattr(shared_metrics_catalog, "provider_metric_name", lambda _provider: "custom")
    assert setup._resumes(flow, "cli_model", "custom:fixture") is True
    assert setup._resumes(flow, "cli_model", "cancel") is False
