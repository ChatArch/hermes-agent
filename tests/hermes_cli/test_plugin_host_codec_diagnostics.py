"""Plugin codec fallbacks stay observable without serializing plugin payloads."""

import builtins
import hashlib
import logging
import subprocess
import sys
import textwrap

import pytest

from hermes_cli import plugin_host_profiles as profiles
from hermes_cli import plugin_host_wire as wire

_PRIVATE_CANARY = "private-plugin-payload-marker"


def _assert_private_diagnostic(caplog, boundary):
    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert records, "a fallback must retain an actionable diagnostic"
    formatter = logging.Formatter("%(message)s")
    for record in records:
        assert record.exc_info and record.exc_info[2] is not None
        rendered = formatter.format(record)
        assert _PRIVATE_CANARY not in rendered
        assert "Traceback" in rendered and boundary in rendered
        assert "model_dump" not in rendered and "__repr__" not in rendered
        assert record.exc_info[1].__cause__ is None
        assert record.exc_info[1].__context__ is None


def test_failed_model_dump_keeps_opaque_fallback_and_private_diagnostic(caplog):
    class BrokenModel:
        def model_dump(self):
            try:
                raise ValueError(_PRIVATE_CANARY)
            except ValueError as exc:
                raise RuntimeError(_PRIVATE_CANARY) from exc

        def __repr__(self):
            return "<stable opaque>"

    with caplog.at_level(logging.WARNING):
        result = wire.encode(BrokenModel())
    assert result == {"__opaque__": "BrokenModel", "repr": "<stable opaque>"}
    _assert_private_diagnostic(caplog, "encode")


def test_failed_repr_keeps_empty_opaque_text_and_private_diagnostic(caplog):
    class BrokenRepr:
        def __repr__(self):
            raise RuntimeError(_PRIVATE_CANARY)

    with caplog.at_level(logging.WARNING):
        result = wire.encode(BrokenRepr())
    assert result == {"__opaque__": "BrokenRepr", "repr": ""}
    _assert_private_diagnostic(caplog, "encode")


def test_version_probe_failure_keeps_fingerprint_and_private_diagnostic(monkeypatch, tmp_path, caplog):
    original_import = builtins.__import__

    def import_failure(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "hermes_cli" and "__version__" in fromlist:
            raise RuntimeError(_PRIVATE_CANARY)
        return original_import(name, globals, locals, fromlist, level)

    plugin_dir = tmp_path / "provider"
    plugin_dir.mkdir()
    monkeypatch.setattr(builtins, "__import__", import_failure)
    with caplog.at_level(logging.WARNING):
        result = profiles._fingerprint(plugin_dir)
    expected = hashlib.sha256(f"{profiles._CACHE_VERSION}|{sys.executable}".encode()).hexdigest()
    assert result == expected
    _assert_private_diagnostic(caplog, "_fingerprint")


def test_plugin_source_literal_is_not_formatted_in_fallback_traceback(tmp_path, caplog):
    literal = "plugin-source-literal-private-marker"
    path = tmp_path / "private_codec_plugin.py"
    source = (
        "class LiteralPlugin:\n"
        "    def model_dump(self):\n"
        f"        raise RuntimeError({literal!r})\n"
        "    def __repr__(self):\n"
        "        return '<literal plugin>'\n"
    )
    path.write_text(source, encoding="utf-8")
    namespace = {}
    exec(compile(source, str(path), "exec"), namespace)
    with caplog.at_level(logging.WARNING):
        result = wire.encode(namespace["LiteralPlugin"]())
    assert result == {"__opaque__": "LiteralPlugin", "repr": "<literal plugin>"}
    _assert_private_diagnostic(caplog, "encode")
    for record in caplog.records:
        assert literal not in logging.Formatter("%(message)s").format(record)


@pytest.mark.parametrize("hook_kind", ["property", "getattribute"])
@pytest.mark.parametrize("boundary", ["dump", "repr", "fingerprint"])
def test_exception_traceback_hooks_cannot_break_existing_fallback(
    monkeypatch, tmp_path, caplog, hook_kind, boundary
):
    attempts = []

    if hook_kind == "property":
        class HookedError(Exception):
            @property
            def __traceback__(self):
                attempts.append("property")
                raise RuntimeError("traceback property denied")
    else:
        class HookedError(Exception):
            def __getattribute__(self, name):
                if name == "__traceback__":
                    attempts.append("getattribute")
                    raise RuntimeError("traceback attribute denied")
                return super().__getattribute__(name)

    class BrokenValue:
        def model_dump(self):
            if boundary == "dump":
                raise HookedError(_PRIVATE_CANARY)
            return {}

        def __repr__(self):
            if boundary == "repr":
                raise HookedError(_PRIVATE_CANARY)
            return "<hooked opaque>"

    if boundary == "fingerprint":
        original_import = builtins.__import__

        def import_failure(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "hermes_cli" and "__version__" in fromlist:
                raise HookedError(_PRIVATE_CANARY)
            return original_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", import_failure)
        plugin_dir = tmp_path / "provider"
        plugin_dir.mkdir()
        operation = lambda: profiles._fingerprint(plugin_dir)
        expected = hashlib.sha256(f"{profiles._CACHE_VERSION}|{sys.executable}".encode()).hexdigest()
        frame_name = "_fingerprint"
    else:
        value = BrokenValue() if boundary == "dump" else type(
            "BrokenRepr", (), {"__repr__": BrokenValue.__repr__}
        )()
        operation = lambda: wire.encode(value)
        expected = {"__opaque__": type(value).__name__, "repr": "<hooked opaque>" if boundary == "dump" else ""}
        frame_name = "encode"

    with caplog.at_level(logging.WARNING):
        try:
            result = operation()
        except RuntimeError:
            result = None
    assert result == expected, "diagnostics must not replace the original fallback with a new error"
    assert attempts == [], "plugin-defined traceback hooks must not be invoked"
    _assert_private_diagnostic(caplog, frame_name)


def test_fallback_does_not_need_audit_permission_to_request_a_caller_frame():
    # Audit hooks cannot be removed; install this one only in an isolated, bounded child.
    script = textwrap.dedent('''
        import sys
        from hermes_cli.plugin_host_wire import encode

        class BrokenModel:
            def model_dump(self):
                raise RuntimeError("private audit probe")
            def __repr__(self):
                return "<audit opaque>"

        def deny_caller_frame(event, args):
            if event == "sys._getframe" and args[0].f_globals.get("__name__") == "hermes_cli.plugin_host_wire":
                raise RuntimeError("caller frame observation denied")

        sys.addaudithook(deny_caller_frame)
        try:
            result = encode(BrokenModel())
        except RuntimeError:
            result = None
        assert result == {"__opaque__": "BrokenModel", "repr": "<audit opaque>"}
    ''')
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=20, check=False)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("boundary", ["dump", "repr", "fingerprint"])
@pytest.mark.parametrize("denied_attr", ["tb_frame", "f_code"])
@pytest.mark.parametrize("error_kind", ["RuntimeError", "PermissionError"])
@pytest.mark.parametrize("warnings_enabled", [True, False])
def test_frame_metadata_audit_denial_preserves_fallback(
    tmp_path, boundary, denied_attr, error_kind, warnings_enabled
):
    # This tests real object.__getattr__ audit events without installing hooks in pytest.
    script = textwrap.dedent('''
        import builtins
        import hashlib
        import logging
        import sys
        from pathlib import Path
        from hermes_cli import plugin_host_profiles as profiles
        from hermes_cli import plugin_host_wire as wire

        boundary, denied_attr, error_kind, warnings_enabled, plugin_dir = sys.argv[1:]
        error_type = getattr(builtins, error_kind)
        logging.disable(logging.NOTSET if warnings_enabled == "1" else logging.CRITICAL)
        hits = []

        class BrokenModel:
            def model_dump(self):
                raise RuntimeError("private metadata probe")
            def __repr__(self):
                return "<audit opaque>"

        class BrokenRepr:
            def __repr__(self):
                raise RuntimeError("private metadata probe")

        if boundary == "fingerprint":
            original_import = builtins.__import__
            def version_failure(name, globals=None, locals=None, fromlist=(), level=0):
                if name == "hermes_cli" and "__version__" in fromlist:
                    raise RuntimeError("private metadata probe")
                return original_import(name, globals, locals, fromlist, level)
            builtins.__import__ = version_failure
            operation = lambda: profiles._fingerprint(Path(plugin_dir))
            expected = hashlib.sha256(f"{profiles._CACHE_VERSION}|{sys.executable}".encode()).hexdigest()
        else:
            value = BrokenModel() if boundary == "dump" else BrokenRepr()
            operation = lambda: wire.encode(value)
            expected = {"__opaque__": type(value).__name__, "repr": "<audit opaque>" if boundary == "dump" else ""}

        def deny_metadata(event, args):
            if event == "object.__getattr__" and args[1] == denied_attr:
                hits.append(denied_attr)
                raise error_type("metadata observation denied")

        sys.addaudithook(deny_metadata)
        try:
            result = operation()
        except (RuntimeError, PermissionError):
            result = None
        if result != expected:
            sys.exit(17)
        if not hits:
            sys.exit(18)
    ''')
    plugin_dir = tmp_path / "provider"
    plugin_dir.mkdir()
    done = subprocess.run(
        [sys.executable, "-c", script, boundary, denied_attr, error_kind,
         "1" if warnings_enabled else "0", str(plugin_dir)],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert done.returncode == 0, (done.returncode, done.stderr)


def test_reference_resolver_failure_still_propagates():
    def refs(_value):
        raise RuntimeError("reference resolution failed")

    with pytest.raises(RuntimeError, match="reference resolution failed"):
        wire.encode(object(), refs=refs)


def test_successful_dump_does_not_emit_a_fallback_warning(caplog):
    class GoodModel:
        def model_dump(self):
            return {"result": 1}

    result = wire.encode(GoodModel())
    assert result == {"__record__": "GoodModel", "fields": {"result": 1}}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
