"""Plugin metadata fallbacks keep their public contracts and private diagnostics."""

import logging

import pytest

from hermes_cli import _parser, plugin_validate_locales
from hermes_cli.plugin_validate import validate_plugin_dir

_PRIVATE_CANARY = "private-plugin-metadata-payload-marker"


def _fail(*_args, **_kwargs):
    try:
        raise ValueError(_PRIVATE_CANARY)
    except ValueError as cause:
        raise RuntimeError(_PRIVATE_CANARY) from cause


def _assert_warning(caplog, module_name, boundary):
    records = [r for r in caplog.records if r.name == module_name
               and r.levelno >= logging.WARNING]
    assert len(records) == 1
    record = records[0]
    assert record.exc_info and record.exc_info[2] is not None
    rendered = logging.Formatter("%(message)s").format(record)
    assert _PRIVATE_CANARY not in rendered
    assert "Traceback" in rendered and boundary in rendered
    assert "in _fail" not in rendered
    assert record.exc_info[1].__cause__ is None and record.exc_info[1].__context__ is None


def _language_pack(tmp_path):
    plugin = tmp_path / "language-pack"
    (plugin / "locales").mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(
        "name: language-pack\nversion: 1.0.0\ndescription: test pack\nprovides_locales: [pl]\n",
        encoding="utf-8",
    )
    (plugin / "locales" / "pl.yaml").write_text(
        "approval:\n  denied: Denied\n", encoding="utf-8",
    )
    return plugin


def test_unknown_command_hint_failure_retains_parser_exit(monkeypatch, caplog, capsys):
    from plugins import memory

    monkeypatch.setattr(memory, "find_provider_dir", _fail)
    monkeypatch.setattr(_parser, "_moved_command_hint", lambda *_args: "")
    parser = _parser.build_top_level_parser()[0]
    with caplog.at_level(logging.WARNING, logger="hermes_cli._parser"):
        with pytest.raises(SystemExit) as caught:
            parser.parse_args(["honcho"])
    assert caught.value.code == 2
    stderr = capsys.readouterr().err
    assert "'honcho' is not a `hermes` command" in stderr
    assert _PRIVATE_CANARY not in stderr
    _assert_warning(caplog, "hermes_cli._parser", "_plugin_command_install_hint")


def test_broken_reference_keeps_pack_admissible_with_diagnostic(monkeypatch, tmp_path, caplog):
    from agent import i18n_layers

    plugin = _language_pack(tmp_path)
    monkeypatch.setattr(i18n_layers, "parse_locale_file", _fail)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugin_validate_locales"):
        report = validate_plugin_dir(plugin)
    assert report.ok, report.failures
    assert any(ok and "key check skipped" in detail for label, ok, detail in report.checks
               if label == "locale pl")
    _assert_warning(caplog, "hermes_cli.plugin_validate_locales", "reference_keys")


def test_broken_locale_remains_rejected_with_diagnostic(monkeypatch, tmp_path, caplog):
    plugin = _language_pack(tmp_path)
    monkeypatch.setattr(plugin_validate_locales, "_load_document", _fail)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugin_validate_locales"):
        report = validate_plugin_dir(plugin)
    assert not report.ok and report.exit_code == 1
    assert any("pl.yaml failed to parse" in error for error in report.failures)
    _assert_warning(caplog, "hermes_cli.plugin_validate_locales", "_check_locale_file")


@pytest.mark.parametrize("failure", ["reference", "locale"])
def test_unavailable_locale_diagnostics_do_not_change_admission(monkeypatch, tmp_path, failure):
    from agent import i18n_layers

    plugin = _language_pack(tmp_path)
    monkeypatch.setattr(plugin_validate_locales, "private_exc_info", lambda _exc: None, raising=False)
    if failure == "reference":
        monkeypatch.setattr(i18n_layers, "parse_locale_file", _fail)
    else:
        monkeypatch.setattr(plugin_validate_locales, "_load_document", _fail)
    report = validate_plugin_dir(plugin)
    assert report.ok is (failure == "reference")


def test_unavailable_hint_diagnostics_keep_original_fallback(monkeypatch):
    from plugins import memory

    monkeypatch.setattr(memory, "find_provider_dir", _fail)
    monkeypatch.setattr(_parser, "private_exc_info", lambda _exc: None, raising=False)
    assert _parser._plugin_command_install_hint("hermes", "honcho") is None
