"""Session SSH image reads must follow the same backend as terminal/file tools."""

import asyncio
import base64
from pathlib import Path

import pytest

from tools import image_source
from tools import terminal_tool as tt

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@pytest.fixture
def ssh_session(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)
    task_id = "image-ssh-session"
    tt.register_task_env_overrides(task_id, {
        "env_type": "ssh", "ssh_alias": "image-target", "ssh_host": "example.invalid",
        "ssh_user": "agent", "cwd": "/home/agent/project",
    })

    class RemoteEnv:
        supports_file_materialization = True
        cwd = "/home/agent/project"
        _remote_home = "/home/agent"

        def __init__(self):
            self.calls = []
            self.failure = None
            self.payload = PNG

        def materialize_file(self, source_path, destination_path, *, max_bytes, timeout,
                             require_recent_seconds=None):
            self.calls.append((source_path, max_bytes, timeout))
            if self.failure:
                raise self.failure
            Path(destination_path).write_bytes(self.payload)
            return {"source_path": source_path, "size": len(self.payload)}

    env = RemoteEnv()
    monkeypatch.setattr(image_source, "_get_active_env", lambda _: env)
    monkeypatch.setattr(tt, "ensure_task_env", lambda _: env)
    yield task_id, env
    tt.clear_task_env_overrides(task_id)


@pytest.mark.parametrize("source", ["absolute", "relative", "tilde", "uri"])
def test_ssh_image_source_uses_session_backend_not_host(ssh_session, tmp_path, source):
    task_id, env = ssh_session
    collision = tmp_path / "shared.png"
    collision.write_bytes(b"HOST FILE MUST NOT BE READ")
    sources = {
        "absolute": str(collision),
        "relative": "reports/screenshot.png",
        "tilde": "~/reports/screenshot.png",
        "uri": "ssh://image-target/home/agent/reports/screenshot.png",
    }
    expected = {
        "absolute": str(collision),
        "relative": "/home/agent/project/reports/screenshot.png",
        "tilde": "~/reports/screenshot.png",
        "uri": "/home/agent/reports/screenshot.png",
    }
    result = asyncio.run(image_source.resolve_image_source(
        sources[source], image_source.ResolveContext(task_id=task_id)))
    assert result.data == PNG
    assert result.mime == "image/png"
    assert env.calls == [(expected[source], image_source._MAX_INGEST_BYTES, 30)]
    assert collision.read_bytes() == b"HOST FILE MUST NOT BE READ"
    assert env.cwd == "/home/agent/project"


@pytest.mark.parametrize("failure", ["missing", "protected", "other-target"])
def test_ssh_image_source_fails_closed_without_local_fallback(ssh_session, tmp_path, failure):
    task_id, env = ssh_session
    collision = tmp_path / "readable-local.png"
    collision.write_bytes(PNG)
    source = str(collision)
    if failure == "other-target":
        source = "ssh://other-target/home/agent/image.png"
    elif failure == "protected":
        env.failure = PermissionError("Remote materialization path is protected")
    else:
        env.failure = RuntimeError("remote file is missing")
    with pytest.raises(image_source.ImageResolutionError):
        asyncio.run(image_source.resolve_image_source(
            source, image_source.ResolveContext(task_id=task_id)))
    assert len(env.calls) == (0 if failure == "other-target" else 1)


def test_video_materialization_uses_the_same_session_backend(ssh_session, tmp_path):
    from tools import vision_tools
    task_id, env = ssh_session
    env.payload = b"\x00\x00\x00\x18ftypisom" + b"REMOTE VIDEO BYTES"
    collision = tmp_path / "shared.mp4"
    collision.write_bytes(b"HOST VIDEO MUST NOT BE READ")
    temporary = []
    try:
        path = asyncio.run(vision_tools._materialize_video(str(collision), task_id, temporary))
        assert path.read_bytes() == env.payload
        assert path != collision
        assert env.calls
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)


def test_registered_vision_handler_embeds_backend_pixels_and_cleans_cache(ssh_session, monkeypatch):
    from tools import vision_tools
    from tools.registry import registry
    from hermes_constants import get_hermes_dir

    task_id, env = ssh_session
    monkeypatch.setattr(vision_tools, "_should_use_native_vision_fast_path", lambda: True)
    cache = get_hermes_dir("cache/vision", "temp_vision_images")
    before = set(cache.rglob("*")) if cache.exists() else set()
    result = asyncio.run(registry.get_entry("vision_analyze").handler(
        {"image_url": "reports/screenshot.png", "question": "What is shown?"}, task_id=task_id))
    assert result["_multimodal"] is True
    assert base64.b64decode(result["content"][1]["image_url"]["url"].split(",", 1)[1]) == PNG
    assert set(cache.rglob("*")) == before
    assert env.cwd == "/home/agent/project"
