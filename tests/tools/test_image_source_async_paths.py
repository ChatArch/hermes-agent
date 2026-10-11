"""Local media probes must not stall the event loop or lose task context."""

import asyncio
import base64
import threading
from contextvars import ContextVar
from pathlib import Path

import pytest

from tools import image_source

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@pytest.mark.parametrize("operation", ["resolve", "is_file", "read_bytes"])
def test_host_file_operations_do_not_run_on_event_loop(monkeypatch, tmp_path, operation):
    image = tmp_path / "picture.png"
    image.write_bytes(PNG)
    monkeypatch.setattr(image_source, "_is_local_terminal_backend", lambda task_id=None: True)
    original = getattr(Path, operation)
    loop_thread = threading.get_ident()
    observed = []

    def checked(path, *args, **kwargs):
        if path == image:
            observed.append(threading.get_ident())
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, operation, checked)
    result = asyncio.run(image_source.resolve_image_source(str(image), image_source.ResolveContext()))
    assert result.data == PNG
    assert result.origin == "file"
    assert observed
    assert loop_thread not in observed, f"{operation} blocked the event loop"


def test_slow_host_probe_allows_event_loop_progress(monkeypatch, tmp_path):
    image = tmp_path / "picture.png"
    image.write_bytes(PNG)
    monkeypatch.setattr(image_source, "_is_local_terminal_backend", lambda task_id=None: True)
    original = image_source._permitted_host_read_target
    entered, release = threading.Event(), threading.Event()

    def slow_probe(path, context):
        entered.set()
        assert release.wait(timeout=2), "event loop could not release the slow probe"
        return original(path, context)

    monkeypatch.setattr(image_source, "_permitted_host_read_target", slow_probe)

    async def scenario():
        async def heartbeat():
            assert await asyncio.to_thread(entered.wait, 2), "host probe never started"
            release.set()

        progress = asyncio.create_task(heartbeat())
        try:
            result = await image_source.resolve_image_source(str(image), image_source.ResolveContext())
            assert result.data == PNG
        finally:
            release.set()
            await progress

    asyncio.run(scenario())


def test_host_credential_guard_keeps_task_context_and_precedes_read(monkeypatch, tmp_path):
    image = tmp_path / "picture.png"
    image.write_bytes(PNG)
    monkeypatch.setattr(image_source, "_is_local_terminal_backend", lambda task_id=None: True)
    scope = ContextVar("image-source-test-scope", default="unbound")
    token = scope.set("served-profile")
    events = []
    original = Path.read_bytes

    def guard(path, source):
        assert scope.get() == "served-profile"
        assert path == image
        events.append("guard")

    def read(path):
        if path == image:
            assert events == ["guard"]
            events.append("read")
        return original(path)

    monkeypatch.setattr(image_source, "_guard_credential_read", guard)
    monkeypatch.setattr(Path, "read_bytes", read)
    try:
        result = asyncio.run(image_source.resolve_image_source(str(image), image_source.ResolveContext()))
        assert result.data == PNG
        assert events == ["guard", "read"]
    finally:
        scope.reset(token)
