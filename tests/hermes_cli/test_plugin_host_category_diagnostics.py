"""Category-plugin failure diagnostics preserve real load-instance dispatch semantics."""

import itertools
import logging
import threading
import types

import pytest

from hermes_cli import plugin_host_child as child

_CANARY = "category-private-payload-marker"


class CategoryBase:
    pass


@pytest.fixture
def category_runtime(monkeypatch):
    # The sync typed handler needs reference tables, not a daemon loop or protocol threads.
    runtime = object.__new__(child.HostRuntime)
    runtime.refs = {}
    runtime.owners = {}
    runtime._ids = itertools.count(1)
    runtime._lock = threading.Lock()
    runtime.instance_modules = {}
    runtime.modules = {}
    monkeypatch.setattr(child, "_import_base", lambda _ref: CategoryBase)
    return runtime


def _load(runtime, module):
    runtime.instance_modules["category-path"] = module
    return runtime.handle("load_instance", {
        "plugin_key": "category-key", "path": "category-path", "base": "category:Base",
        "capture": "register_category", "name": "test category", "forward": False,
    }, None)


def _private_failure_is_visible(caplog):
    records = [r for r in caplog.records if r.name == "hermes_cli.plugin_host_child"
               and r.levelno >= logging.WARNING]
    assert records, "category failure must remain diagnosable"
    for record in records:
        assert record.exc_info and record.exc_info[2] is not None
        text = logging.Formatter("%(message)s").format(record)
        assert _CANARY not in text
        assert "Traceback" in text and "op_load_instance" in text
        assert record.exc_info[1].__cause__ is None and record.exc_info[1].__context__ is None


def test_register_failure_keeps_subclass_fallback_and_private_diagnostic(category_runtime, caplog):
    calls = []
    module = types.ModuleType("test_category_fallback")

    def register(_ctx):
        calls.append("register")
        raise RuntimeError(_CANARY)

    class Fallback(CategoryBase):
        def __init__(self):
            calls.append("fallback")

    module.register = register
    module.Fallback = Fallback
    with caplog.at_level(logging.WARNING):
        result = _load(category_runtime, module)
    assert calls == ["register", "fallback"]
    assert isinstance(category_runtime.refs[result["__object__"]], Fallback)
    assert category_runtime.owners[result["__object__"]] == "category-key"
    _private_failure_is_visible(caplog)


def test_captured_instance_wins_after_register_failure(category_runtime, caplog):
    module = types.ModuleType("test_category_capture")
    captured = CategoryBase()

    def register(ctx):
        ctx.register_category(captured)
        raise RuntimeError(_CANARY)

    class Fallback(CategoryBase):
        def __init__(self):
            pytest.fail("already captured instance must not be replaced")

    module.register = register
    module.Fallback = Fallback
    with caplog.at_level(logging.WARNING):
        result = _load(category_runtime, module)
    assert category_runtime.refs[result["__object__"]] is captured
    _private_failure_is_visible(caplog)


def test_failed_constructor_tries_next_subclass_and_private_diagnostic(category_runtime, caplog):
    calls = []
    module = types.ModuleType("test_category_constructor")

    class AFailure(CategoryBase):
        def __init__(self):
            calls.append("broken")
            raise RuntimeError(_CANARY)

    class BSuccess(CategoryBase):
        def __init__(self):
            calls.append("success")

    module.AFailure = AFailure
    module.BSuccess = BSuccess
    with caplog.at_level(logging.WARNING):
        result = _load(category_runtime, module)
    assert calls == ["broken", "success"]
    assert isinstance(category_runtime.refs[result["__object__"]], BSuccess)
    _private_failure_is_visible(caplog)


@pytest.mark.parametrize("phase", ["register", "constructor"])
def test_unavailable_diagnostic_keeps_category_fallback(category_runtime, monkeypatch, phase):
    module = types.ModuleType("category_diagnostic_unavailable")
    monkeypatch.setattr(child, "private_exc_info", lambda _exc: None)

    def register(_ctx):
        raise RuntimeError(_CANARY)

    class AFailure(CategoryBase):
        def __init__(self):
            raise RuntimeError(_CANARY)

    class BSuccess(CategoryBase):
        pass

    if phase == "register":
        module.register = register
    else:
        module.AFailure = AFailure
    module.BSuccess = BSuccess
    result = _load(category_runtime, module)
    assert isinstance(category_runtime.refs[result["__object__"]], BSuccess)


def test_no_capture_or_usable_subclass_still_returns_none(category_runtime):
    assert _load(category_runtime, types.ModuleType("empty_category")) is None


def test_successful_capture_keeps_reference_contract_without_warning(category_runtime, caplog):
    module = types.ModuleType("good_category")
    captured = CategoryBase()
    module.register = lambda ctx: ctx.register_category(captured)
    result = _load(category_runtime, module)
    assert category_runtime.refs[result["__object__"]] is captured
    assert category_runtime.modules["category-key"] == "good_category"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
