"""Optional metadata failures retain UI and skill identity fallbacks."""

import builtins
import logging
import json
from types import SimpleNamespace

import pytest

from agent import error_surface, learning_graph, skill_commands
from hermes_cli import models, plugin_host_diagnostics, provider_catalog
from tools import skills_tool

_PRIVATE = "private-metadata-diagnostic-marker"


def _fail(*_args, **_kwargs):
    try:
        raise ValueError(_PRIVATE)
    except ValueError as cause:
        raise RuntimeError(_PRIVATE) from cause


def _check(caplog, name, boundary, diagnostics):
    rows = [r for r in caplog.records if r.name == name and r.levelno >= logging.WARNING]
    if not diagnostics:
        assert rows == []
        return
    assert len(rows) == 1
    text = logging.Formatter("%(message)s").format(rows[0])
    assert boundary in text and "Traceback" in text
    assert _PRIVATE not in text and "in _fail" not in text
    assert rows[0].exc_info[1].__cause__ is None and rows[0].exc_info[1].__context__ is None


@pytest.mark.parametrize("diagnostics", [True, False])
@pytest.mark.parametrize("boundary", ["api_env", "provider_label", "auth_kind", "missing_skill", "external_path"])
def test_metadata_failure_retains_exact_result(tmp_path, monkeypatch, caplog, diagnostics, boundary):
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _exc: None)
    if boundary in ("api_env", "auth_kind"):
        monkeypatch.setattr(provider_catalog, "provider_catalog_by_slug", _fail)
        call = error_surface._api_key_env if boundary == "api_env" else error_surface.auth_kind
        module, catching = error_surface, call.__name__
    elif boundary == "provider_label":
        monkeypatch.setattr(models, "provider_label", _fail)
        call, module, catching = error_surface._provider_label, error_surface, "_provider_label"
    elif boundary == "missing_skill":
        monkeypatch.setattr(skills_tool, "skill_view", _fail)
        call, module, catching = skill_commands._missing_skill_label, skill_commands, "_missing_skill_label"
    else:
        root = tmp_path / "skills"
        for name in ("alpha", "beta"):
            folder = root / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody", encoding="utf-8")
        monkeypatch.setattr(learning_graph, "_load_usage", dict)
        original = builtins.__import__

        def unavailable(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "agent.skill_utils" and "is_external_skill_path" in fromlist:
                _fail()
            return original(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", unavailable)
        call, module, catching = None, learning_graph, "build_skill_nodes"
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        if boundary == "external_path":
            nodes = learning_graph.build_skill_nodes([("profile", root)])
            assert set(nodes) == {"alpha", "beta"}
            assert all(node.source == "profile" for node in nodes.values())
        else:
            result = call(_PRIVATE)
            expected = "" if boundary == "api_env" else "api_key" if boundary == "auth_kind" else _PRIVATE
            assert result == expected
    _check(caplog, module.__name__, catching, diagnostics)


def test_metadata_success_paths_keep_identity(monkeypatch):
    descriptor = SimpleNamespace(api_key_env_vars=["FIXTURE_API_KEY"], tab="accounts")
    monkeypatch.setattr(provider_catalog, "provider_catalog_by_slug", lambda: {"fixture": descriptor})
    assert error_surface._api_key_env("Fixture") == "FIXTURE_API_KEY"
    assert error_surface.auth_kind("fixture") == "oauth"
    assert error_surface._api_key_env("unknown") == ""
    monkeypatch.setattr(models, "provider_label", lambda provider: provider + " label")
    assert error_surface._provider_label("fixture") == "fixture label"
    monkeypatch.setattr(skills_tool, "skill_view", lambda *_args, **_kwargs: json.dumps({"load_names": ["one/a", "two/a"]}))
    assert skill_commands._missing_skill_label("a") == "Ambiguous skill name a: use one of one/a, two/a"
