"""Private rollup diagnostics retain root-profile consent and count fallbacks."""

import logging
from pathlib import Path

import pytest

import hermes_constants
from hermes_cli import plugin_host_diagnostics
from hermes_cli import config
from hermes_cli.observability import shared_metrics_engagement as engagement
from hermes_cli.observability import shared_metrics_snapshot as snapshot

_PRIVATE = "private-rollup-probe-marker"


def _fail(*_args, **_kwargs):
    try:
        raise ValueError(_PRIVATE)
    except ValueError as cause:
        raise RuntimeError(_PRIVATE) from cause


def _warning(caplog, name, boundary, enabled):
    records = [record for record in caplog.records if record.name == name and record.levelno >= logging.WARNING]
    if not enabled:
        assert records == []
        return
    assert len(records) == 1
    rendered = logging.Formatter("%(message)s").format(records[0])
    assert boundary in rendered and "Traceback" in rendered
    assert _PRIVATE not in rendered and "in _fail" not in rendered
    assert records[0].exc_info[1].__cause__ is None
    assert records[0].exc_info[1].__context__ is None


@pytest.mark.parametrize("diagnostics", [True, False])
def test_unreadable_root_keeps_profile_home(tmp_path, monkeypatch, caplog, diagnostics):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", _fail)
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=engagement.__name__):
        assert engagement._root_home(profile) == profile.resolve()
    _warning(caplog, engagement.__name__, "_root_home", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_unreadable_root_consent_restores_original_profile(tmp_path, monkeypatch, caplog, diagnostics):
    original = hermes_constants.get_hermes_home()
    home = tmp_path / "root"
    home.mkdir()
    observed = []

    def unreadable_config():
        observed.append(hermes_constants.get_hermes_home())
        _fail()

    monkeypatch.setattr(config, "read_raw_config_readonly", unreadable_config)
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=engagement.__name__):
        assert engagement._collects(home) is False
    assert observed == [home] and hermes_constants.get_hermes_home() == original
    _warning(caplog, engagement.__name__, "_collects", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_unreadable_snapshot_count_keeps_zero(monkeypatch, caplog, diagnostics):
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    with caplog.at_level(logging.WARNING, logger=snapshot.__name__):
        assert snapshot._safe_count(_fail) == 0
    _warning(caplog, snapshot.__name__, "_safe_count", diagnostics)


def test_healthy_rollup_probes_keep_original_result_types(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *, home: root)
    assert engagement._root_home(tmp_path / "profile") == root.resolve()
    monkeypatch.setattr(config, "read_raw_config_readonly", lambda: {"telemetry": {"shared_metrics": {"enabled": True}}})
    before = hermes_constants.get_hermes_home()
    assert engagement._collects(root) is True
    assert hermes_constants.get_hermes_home() == before
    marker = object()
    assert snapshot._safe_count(lambda: marker) is marker
