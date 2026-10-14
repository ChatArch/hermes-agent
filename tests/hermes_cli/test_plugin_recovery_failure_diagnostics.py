"""Catalog, plugin-list and memory recovery failures keep bounded private diagnostics."""

import json
import logging
from types import SimpleNamespace

import pytest

from hermes_cli import _parser, memory_provider_migration as migration, plugins_cmd
from hermes_cli import plugin_catalog, plugins_cmd_catalog as catalog

_PRIVATE_CANARY = "private-plugin-recovery-payload-marker"


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


@pytest.mark.parametrize("diagnostics", [True, False])
@pytest.mark.parametrize("name,expected", [
    ("catalog_pins", {}), ("catalog_titles", {}), ("catalog_versions", {}),
    ("catalog_rows_maps", ({}, {}, {})),
])
def test_catalog_query_failure_preserves_empty_maps(monkeypatch, caplog, name, expected, diagnostics):
    monkeypatch.setattr(catalog, "load_catalog_live", _fail)
    if not diagnostics:
        monkeypatch.setattr(catalog, "private_exc_info", lambda _exc: None, raising=False)
    with caplog.at_level(logging.WARNING, logger=catalog.__name__):
        assert getattr(catalog, name)() == expected
    _check_warning(caplog, catalog.__name__, name, diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_catalog_hint_failure_keeps_typed_command_error(monkeypatch, caplog, capsys, diagnostics):
    from plugins import memory

    monkeypatch.setattr(memory, "find_provider_dir", lambda _name: None)
    monkeypatch.setattr(plugin_catalog, "get_catalog_entry", _fail)
    monkeypatch.setattr(_parser, "_moved_command_hint", lambda *_args: "")
    if not diagnostics:
        monkeypatch.setattr(migration, "private_exc_info", lambda _exc: None, raising=False)
    with caplog.at_level(logging.WARNING, logger=migration.__name__):
        with pytest.raises(SystemExit) as caught:
            _parser.build_top_level_parser()[0].parse_args(["honcho"])
    assert caught.value.code == 2
    assert "'honcho' is not a `hermes` command" in capsys.readouterr().err
    _check_warning(caplog, migration.__name__, "catalog_install_hint", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_plugin_list_manifest_failure_keeps_plugin_not_enabled(monkeypatch, tmp_path, caplog, capsys, diagnostics):
    plugin = tmp_path / "bundled-plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("kind: platform\n", encoding="utf-8")
    monkeypatch.setattr(plugins_cmd, "_discover_all_plugins", lambda: [
        ("bundled-plugin", "1.0", "test plugin", "bundled", plugin, "bundled-plugin"),
    ])
    monkeypatch.setattr(plugins_cmd, "_get_enabled_set", set)
    monkeypatch.setattr(plugins_cmd, "_get_disabled_set", set)
    monkeypatch.setattr(plugins_cmd, "_category_active_names", set)
    monkeypatch.setattr(plugins_cmd, "_read_install_metadata", dict)
    monkeypatch.setattr(plugins_cmd, "_load_yaml_manifest", _fail)
    monkeypatch.setattr(catalog, "resolved_removed_entries", list)
    monkeypatch.setattr(catalog, "catalog_annotation", lambda _path: "")
    if not diagnostics:
        monkeypatch.setattr(plugins_cmd, "private_exc_info", lambda _exc: None, raising=False)
    with caplog.at_level(logging.WARNING, logger=plugins_cmd.__name__):
        plugins_cmd.cmd_list(SimpleNamespace(json=True))
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["name"] == "bundled-plugin" and rows[0]["status"] == "not enabled"
    _check_warning(caplog, plugins_cmd.__name__, "_default_on", diagnostics)


@pytest.mark.parametrize("diagnostics", [True, False])
def test_memory_install_failure_keeps_config_and_recovery_message(monkeypatch, tmp_path, caplog, diagnostics):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = tmp_path / "config.yaml"
    config.write_text("memory:\n  provider: honcho\n  honcho:\n    workspace: keep-me\n", encoding="utf-8")
    before = config.read_bytes()
    monkeypatch.setattr(migration, "provider_present", lambda _name, _home: False)
    monkeypatch.setattr(migration, "catalog_source", lambda name: name)
    if not diagnostics:
        monkeypatch.setattr(migration, "private_exc_info", lambda _exc: None, raising=False)
    said = []
    with caplog.at_level(logging.WARNING, logger=migration.__name__):
        assert migration.migrate_home(tmp_path, install=_fail, say=said.append) is None
    assert config.read_bytes() == before
    assert len(said) == 1 and "could not be installed automatically" in said[0]
    assert "plugins install honcho" in said[0]
    _check_warning(caplog, migration.__name__, "_install_pending", diagnostics)


def test_combined_catalog_maps_use_one_resolution(monkeypatch, caplog):
    calls = []
    entry = SimpleNamespace(name="sample", sha="a" * 40, version="1.0", title="Sample")
    monkeypatch.setattr(catalog, "load_catalog_live", lambda: calls.append(True) or [entry])
    with caplog.at_level(logging.WARNING, logger=catalog.__name__):
        assert catalog.catalog_rows_maps() == (
            {"sample": "a" * 40}, {"sample": "1.0"}, {"sample": "Sample"},
        )
    assert calls == [True]
    assert not [r for r in caplog.records if r.name == catalog.__name__]
