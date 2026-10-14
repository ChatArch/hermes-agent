"""Malformed locale input must not leak through failure diagnostics."""

import logging

import pytest

from agent import i18n, i18n_layers


@pytest.mark.parametrize("layer", ["bundled", "overlay"])
def test_locale_parse_failure_keeps_fallback_and_safe_traceback(tmp_path, monkeypatch, caplog, layer):
    home = tmp_path / "profile"
    locales = home / "locales"
    locales.mkdir(parents=True)
    canary = "PRIVATE_LOCALE_INPUT_CANARY"
    (locales / "en.yaml").write_text(f'payload: "{canary}\n', encoding="utf-8")
    i18n.reset_language_cache()
    if layer == "bundled":
        monkeypatch.setattr(i18n, "_locales_dir", lambda: locales)
        logger_name = i18n.logger.name
        load = lambda: i18n._load_bundled("en")
    else:
        logger_name = i18n_layers.logger.name
        load = lambda: i18n_layers.overlay_layer(home, "en")
    try:
        with caplog.at_level(logging.WARNING, logger=logger_name):
            assert load() == {}
        records = [record for record in caplog.records if record.name == logger_name]
        assert len(records) == 1
        assert canary not in caplog.text
        assert records[0].exc_info is not None
        assert records[0].exc_info[2] is not None
        assert "ScannerError" in caplog.text
    finally:
        i18n.reset_language_cache()
