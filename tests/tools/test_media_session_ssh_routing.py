"""Media paths honor session routing without moving provider credentials."""
import base64
import json
from pathlib import Path

import pytest

from gateway import media_fetch, session_context
from tools import image_generation_tool as image_gen
from tools import terminal_tool as tt

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@pytest.fixture
def session(monkeypatch):
    task = "media-routing-session"
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)
    monkeypatch.setattr(tt, "_task_env_overrides", {})
    monkeypatch.setattr(tt, "_active_environments", {})
    tt.register_task_env_overrides(task, {"env_type": "ssh", "ssh_alias": "media-target"})
    key_token = session_context._SESSION_KEY.set(task)
    id_token = session_context._SESSION_ID.set(task)

    class Remote:
        supports_file_materialization = True
        _remote_home = "/home/agent"
        cwd = "/home/agent/project"

        def __init__(self):
            self.paths = []
            self.failure = None

        def materialize_file(self, source, destination, **kwargs):
            self.paths.append(source)
            if self.failure:
                raise self.failure
            Path(destination).write_bytes(PNG)

        def fetch_realpath(self, source):
            return source

        def fetch_file(self, source, destination, *, max_bytes):
            self.paths.append(source)
            Path(destination).write_bytes(PNG)

    env = Remote()
    tt._active_environments[task] = env
    monkeypatch.setattr(tt, "ensure_task_env", lambda _: env)
    try:
        yield task, env
    finally:
        session_context._SESSION_KEY.reset(key_token)
        session_context._SESSION_ID.reset(id_token)


@pytest.mark.parametrize("field", ["image_url", "reference_image_urls"])
def test_image_handler_converts_remote_reference_before_provider(session, monkeypatch, tmp_path, field):
    task, env = session
    collision = tmp_path / "shared.png"
    collision.write_bytes(b"HOST INPUT MUST NOT BE READ")
    seen = {}

    def provider(prompt, aspect_ratio, **kwargs):
        seen.update(kwargs)
        return json.dumps({"success": True, "image": "https://example.invalid/result.png"})

    monkeypatch.setattr(image_gen, "_dispatch_to_plugin_provider", provider)
    value = str(collision) if field == "image_url" else [str(collision)]
    result = json.loads(image_gen._handle_image_generate({"prompt": "edit", field: value}, task_id=task))
    assert result["success"] is True
    actual = seen[field] if field == "image_url" else seen[field][0]
    assert actual == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert env.paths == [str(collision)]
    assert collision.read_bytes() == b"HOST INPUT MUST NOT BE READ"


def test_image_remote_failure_does_not_dispatch_host_collision(session, monkeypatch, tmp_path):
    task, env = session
    collision = tmp_path / "shared.png"
    collision.write_bytes(PNG)
    env.failure = RuntimeError("remote missing")
    calls = []
    monkeypatch.setattr(image_gen, "_dispatch_to_plugin_provider", lambda *a, **kw: calls.append(kw) or '{}')
    result = json.loads(image_gen._handle_image_generate(
        {"prompt": "edit", "image_url": str(collision)}, task_id=task))
    assert calls == []
    assert result.get("error")


def test_media_fetch_honors_session_ssh_override(session, monkeypatch, tmp_path):
    _, env = session
    cache = tmp_path / "documents"
    monkeypatch.setattr("gateway.platforms.base.DOCUMENT_CACHE_DIR", cache)
    monkeypatch.setattr("gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS", (cache,))
    result = media_fetch.fetch_remote_media("/home/agent/report.png")
    assert result is not None
    assert Path(result).read_bytes() == PNG
    assert env.paths == ["/home/agent/report.png"]


def test_media_fetch_respects_session_local_override(session, monkeypatch):
    task, _ = session
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    tt.register_task_env_overrides(task, {"env_type": "local"})
    assert media_fetch._active_remote_env() is None


def test_image_cache_fallback_respects_local_session_override(session, monkeypatch, tmp_path):
    task, _ = session
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    tt.register_task_env_overrides(task, {"env_type": "local"})
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(image_gen, "_active_terminal_env", lambda _: None)
    raw = json.dumps({"success": True, "image": str(home / "cache/images/result.png")})
    assert json.loads(image_gen._postprocess_image_generate_result(raw, task_id=task)) == json.loads(raw)
