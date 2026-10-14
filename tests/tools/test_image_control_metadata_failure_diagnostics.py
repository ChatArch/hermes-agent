"""Creative-control metadata failures preserve filtering and both provider callers."""

import json
import logging
from types import SimpleNamespace

import pytest

from hermes_cli import plugin_host_diagnostics
from tools import image_generation_tool as image

_PRIVATE = "private-control-metadata-marker"


@pytest.mark.parametrize("managed", [True, False])
@pytest.mark.parametrize("diagnostics", [True, False])
def test_metadata_failure_does_not_change_provider_kwargs(monkeypatch, caplog, managed, diagnostics):
    events = []
    error = RuntimeError(_PRIVATE)
    cause = ValueError(_PRIVATE)

    def capabilities():
        events.append("capabilities")
        raise error from cause

    def generate(**kwargs):
        events.append(("generate", tuple(kwargs.items())))
        return {"ok": True}

    provider = SimpleNamespace(name="fixture", display_name="Fixture", capabilities=capabilities, generate=generate)
    monkeypatch.setattr(image, "_get_plugin_provider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(image, "_plugin_provider_name", lambda: "fixture")
    monkeypatch.setattr(image, "_read_configured_image_model", lambda: "fixture-model")
    monkeypatch.setattr(image, "_managed_model_plugin", lambda: ("nous", "fixture-model"))
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _error: None)
    call = image._maybe_route_managed_model if managed else image._dispatch_to_plugin_provider
    with caplog.at_level(logging.WARNING, logger=image.__name__):
        result = call("fixture-prompt", "1:1", controls={"quality": object()})
    assert json.loads(result) == {"ok": True}
    assert events == ["capabilities", ("generate", (("prompt", "fixture-prompt"), ("aspect_ratio", "1:1"), ("model", "fixture-model")))]
    assert error.__cause__ is cause and error.__context__ is None
    records = [r for r in caplog.records if r.name == image.__name__ and r.levelno >= logging.WARNING]
    if not diagnostics:
        assert records == []
        return
    assert len(records) == 1
    rendered = logging.Formatter("%(message)s").format(records[0])
    assert "Creative-control metadata unavailable" in rendered and "_declared_controls" in rendered
    assert _PRIVATE not in rendered and "in capabilities" not in rendered


@pytest.mark.parametrize("controls", [None, {}])
def test_absent_controls_do_not_probe_metadata(controls):
    def forbidden():
        pytest.fail("No capabilities call without controls")

    assert image._declared_controls(SimpleNamespace(capabilities=forbidden), controls) is None


def test_successful_filter_keeps_order_value_identity_and_input(caplog):
    first, second = object(), object()
    controls = {"b": first, "ignored": object(), "a": second}
    provider = SimpleNamespace(capabilities=lambda: {"creative_controls": ["a", "b"]})
    result = image._declared_controls(provider, controls)
    assert list(result) == ["b", "a"] and result["b"] is first and result["a"] is second
    assert list(controls) == ["b", "ignored", "a"]
    assert not [r for r in caplog.records if r.name == image.__name__ and r.levelno >= logging.WARNING]


def test_membership_failure_remains_outside_metadata_catch():
    marker = ValueError("membership marker")

    class Declared:
        def __contains__(self, _name):
            raise marker

    provider = SimpleNamespace(capabilities=lambda: {"creative_controls": Declared()})
    with pytest.raises(ValueError) as caught:
        image._declared_controls(provider, {"quality": object()})
    assert caught.value is marker
