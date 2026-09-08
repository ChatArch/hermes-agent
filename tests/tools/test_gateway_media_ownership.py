"""Gateway-generated media keeps explicit ownership while tasks run over SSH."""
import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from tools import terminal_tool as tt

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


@pytest.fixture
def ssh_task(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)
    task = "generated-media-ssh"
    tt.register_task_env_overrides(task, {"env_type": "ssh", "ssh_alias": "target", "ssh_host": "example.invalid", "ssh_user": "agent"})
    yield task
    tt.clear_task_env_overrides(task)


def test_tts_tags_keep_gateway_ownership_under_session_ssh(ssh_task, tmp_path):
    from tools.tts_tool import _media_tag
    audio = tmp_path / "audio with spaces.mp3"
    audio.write_bytes(b"test audio")
    assert _media_tag([str(audio)], False, task_id=ssh_task) == "MEDIA:" + audio.as_uri()
    assert _media_tag([str(audio)], True, task_id=ssh_task) == "[[audio_as_voice]]\nMEDIA:" + audio.as_uri()


def test_generated_image_without_sync_never_invents_remote_file(ssh_task, tmp_path, monkeypatch):
    from tools import image_generation_tool as images
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / "cache/images/generated.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG)
    monkeypatch.setattr(images, "_active_terminal_env", lambda _: SimpleNamespace(_remote_home="/home/agent", _sync_manager=None))
    result = json.loads(images._postprocess_image_generate_result(json.dumps({"success": True, "image": str(path)}), task_id=ssh_task))
    assert "agent_visible_image" not in result
    assert result["image_resource"] == path.as_uri()
    assert result["media_tag"] == "MEDIA:" + path.as_uri()


@pytest.mark.parametrize("case", ["cached-spaces", "missing-cache", "outside-cache"])
def test_file_uri_is_gateway_owned_and_never_retried_on_ssh(ssh_task, tmp_path, monkeypatch, case):
    from tools import image_source as images
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / "cache/images/image with spaces.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    if case == "outside-cache":
        path = tmp_path / "outside.png"
    if case != "missing-cache":
        path.write_bytes(PNG)
    monkeypatch.setattr(images, "_ensure_container_env", lambda _: pytest.fail("explicit gateway URI must not read SSH"))
    if case == "cached-spaces":
        result = asyncio.run(images.resolve_image_source(path.as_uri(), images.ResolveContext(task_id=ssh_task)))
        assert result.data == PNG
    else:
        with pytest.raises(images.ImageResolutionError):
            asyncio.run(images.resolve_image_source(path.as_uri(), images.ResolveContext(task_id=ssh_task)))
