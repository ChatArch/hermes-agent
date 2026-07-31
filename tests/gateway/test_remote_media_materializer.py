import asyncio
from pathlib import Path

from gateway.run import GatewayRunner


class _FakeRemoteEnvironment:
    supports_file_materialization = True

    def __init__(self, payload: bytes = b"\x89PNG\r\n\x1a\nremote"):
        self.payload = payload
        self.calls = []

    def materialize_file(
        self,
        source_path,
        destination_path,
        *,
        max_bytes,
        timeout,
        require_recent_seconds=None,
    ):
        self.calls.append(
            {
                "source_path": source_path,
                "destination_path": str(destination_path),
                "max_bytes": max_bytes,
                "timeout": timeout,
                "require_recent_seconds": require_recent_seconds,
            }
        )
        destination = Path(destination_path)
        destination.write_bytes(self.payload)
        return {
            "source_path": source_path,
            "path": str(destination),
            "size": len(self.payload),
            "mtime": 1.0,
        }


class _FailingRemoteEnvironment(_FakeRemoteEnvironment):
    def materialize_file(self, *args, **kwargs):
        raise RuntimeError("remote transfer failed")


class _PartialFailingRemoteEnvironment(_FakeRemoteEnvironment):
    def materialize_file(self, source_path, destination_path, **kwargs):
        Path(destination_path).write_bytes(self.payload)
        raise RuntimeError("remote transfer failed after staging")


def test_materializes_explicit_remote_media_into_local_cache(tmp_path):
    from gateway.media_materializer import materialize_response_media

    env = _FakeRemoteEnvironment()
    result = materialize_response_media(
        "Screenshot\nMEDIA:ssh://build.example/srv/captures/page.png",
        task_id="session-1",
        ssh_alias="build.example",
        env=env,
        cache_dir=tmp_path,
        max_bytes=1024,
        timeout=9,
    )

    assert len(env.calls) == 1
    assert env.calls[0]["source_path"] == "/srv/captures/page.png"
    assert env.calls[0]["max_bytes"] == 1024
    assert env.calls[0]["timeout"] == 9
    assert "ssh://" not in result.response
    assert result.response.startswith("Screenshot\nMEDIA:")
    assert len(result.materialized_paths) == 1
    local_path = Path(result.materialized_paths[0])
    assert local_path.parent == tmp_path
    assert local_path.suffix == ".png"
    assert local_path.read_bytes() == env.payload
    assert result.failures == ()


def test_legacy_remote_path_uses_current_session_binding(tmp_path):
    from gateway.media_materializer import materialize_response_media

    env = _FakeRemoteEnvironment()
    response = "MEDIA:/srv/captures/page.png\nMEDIA:/srv/captures/page.png"
    result = materialize_response_media(
        response,
        task_id="session-1",
        env=env,
        cache_dir=tmp_path,
    )

    assert len(env.calls) == 1
    assert result.response.count(result.materialized_paths[0]) == 2


def test_preserves_existing_gateway_local_media_without_remote_copy(tmp_path):
    from gateway.media_materializer import materialize_response_media

    local_image = tmp_path / "already-local.png"
    local_image.write_bytes(b"\x89PNG\r\n\x1a\nlocal")
    env = _FakeRemoteEnvironment()

    result = materialize_response_media(
        f"MEDIA:{local_image}",
        task_id="session-1",
        env=env,
        cache_dir=tmp_path / "remote-cache",
    )

    assert result.response == f"MEDIA:{local_image}"
    assert result.materialized_paths == ()
    assert result.failures == ()
    assert env.calls == []


def test_ignores_media_examples_inside_protected_spans(tmp_path):
    from gateway.media_materializer import materialize_response_media

    env = _FakeRemoteEnvironment()
    response = (
        "```text\nMEDIA:/srv/example.png\n"
        "MEDIA:ssh://build.example/srv/example/Caddyfile\n```\n"
        "`MEDIA:/srv/inline.png`\n"
        "> MEDIA:/srv/quote.png\n"
        '{"example":"MEDIA:ssh://build.example/srv/example.png"}'
    )

    result = materialize_response_media(
        response,
        task_id="session-1",
        env=env,
        cache_dir=tmp_path,
    )

    assert result.response == response
    assert result.materialized_paths == ()
    assert result.failures == ()
    assert env.calls == []


def test_supports_quoted_remote_media_paths_with_spaces(tmp_path):
    from gateway.media_materializer import materialize_response_media

    env = _FakeRemoteEnvironment()
    result = materialize_response_media(
        'MEDIA:"/srv/captures/page one.png"',
        task_id="session-1",
        env=env,
        cache_dir=tmp_path,
    )

    assert env.calls[0]["source_path"] == "/srv/captures/page one.png"
    assert result.response == f"MEDIA:{result.materialized_paths[0]}"


def test_failed_remote_materialization_removes_directive_and_reports_failure(tmp_path):
    from gateway.media_materializer import materialize_response_media

    result = materialize_response_media(
        "Screenshot\nMEDIA:/srv/captures/missing.png",
        task_id="session-1",
        env=_FailingRemoteEnvironment(),
        cache_dir=tmp_path,
    )

    assert result.response == "Screenshot"
    assert result.materialized_paths == ()
    assert len(result.failures) == 1
    assert result.failures[0].source_path == "/srv/captures/missing.png"
    assert "transfer failed" in result.failures[0].error


def test_failed_remote_materialization_removes_partial_gateway_file(tmp_path):
    from gateway.media_materializer import materialize_response_media

    result = materialize_response_media(
        "MEDIA:/srv/captures/missing.png",
        task_id="session-1",
        env=_PartialFailingRemoteEnvironment(),
        cache_dir=tmp_path,
    )

    assert result.materialized_paths == ()
    assert len(result.failures) == 1
    assert not list(tmp_path.rglob("remote_*.png"))


def test_remote_materializer_uses_gateway_selected_destination(tmp_path):
    from gateway.media_materializer import materialize_response_media

    unrelated = tmp_path / "unrelated.png"
    unrelated.write_bytes(b"\x89PNG\r\n\x1a\nunrelated")

    class _RedirectingEnvironment(_FakeRemoteEnvironment):
        def materialize_file(self, *args, **kwargs):
            result = super().materialize_file(*args, **kwargs)
            result["path"] = str(unrelated)
            return result

    cache = tmp_path / "cache"
    result = materialize_response_media(
        "MEDIA:/srv/captures/page.png",
        task_id="session-1",
        env=_RedirectingEnvironment(),
        cache_dir=cache,
    )

    assert len(result.materialized_paths) == 1
    assert Path(result.materialized_paths[0]).parent == cache
    assert Path(result.materialized_paths[0]).read_bytes().endswith(b"remote")


def test_remote_materializer_caps_unique_files_per_response(tmp_path):
    from gateway.media_materializer import materialize_response_media

    env = _FakeRemoteEnvironment()
    response = "\n".join(
        f"MEDIA:/srv/captures/page-{index}.png" for index in range(11)
    )
    result = materialize_response_media(
        response,
        task_id="session-1",
        env=env,
        cache_dir=tmp_path,
        max_bytes=1024,
    )

    assert len(env.calls) == 10
    assert len(result.materialized_paths) == 10
    assert len(result.failures) == 1
    assert "10-file limit" in result.failures[0].error


def test_remote_materializer_uses_one_response_deadline(monkeypatch, tmp_path):
    import gateway.media_materializer as media_materializer

    ticks = iter([0.0, 1.0, 11.0])
    monkeypatch.setattr(
        media_materializer,
        "_monotonic",
        lambda: next(ticks),
        raising=False,
    )
    env = _FakeRemoteEnvironment()
    result = media_materializer.materialize_response_media(
        "MEDIA:/srv/first.png\nMEDIA:/srv/second.png",
        task_id="session-1",
        env=env,
        cache_dir=tmp_path,
        timeout=10,
    )

    assert len(env.calls) == 1
    assert len(result.materialized_paths) == 1
    assert len(result.failures) == 1
    assert "deadline" in result.failures[0].error


def test_file_uri_normalizes_to_existing_gateway_path(tmp_path):
    from gateway.media_materializer import materialize_response_media

    local_image = tmp_path / "file uri.png"
    local_image.write_bytes(b"\x89PNG\r\n\x1a\nlocal")
    uri = local_image.as_uri()
    env = _FakeRemoteEnvironment()

    result = materialize_response_media(
        f"MEDIA:{uri}",
        task_id="session-1",
        ssh_alias="build.example",
        env=env,
        cache_dir=tmp_path / "remote-cache",
    )

    assert result.response == f"MEDIA:{local_image}"
    assert result.materialized_paths == ()
    assert result.failures == ()
    assert env.calls == []


def test_ssh_uri_must_match_current_session_binding(tmp_path):
    from gateway.media_materializer import materialize_response_media

    env = _FakeRemoteEnvironment()
    result = materialize_response_media(
        "MEDIA:ssh://other-target/srv/captures/page.png",
        task_id="session-1",
        ssh_alias="build.example",
        env=env,
        cache_dir=tmp_path,
    )

    assert result.response == ""
    assert result.materialized_paths == ()
    assert len(result.failures) == 1
    assert "does not match" in result.failures[0].error
    assert env.calls == []


def test_ssh_uri_percent_decodes_remote_path(tmp_path):
    from gateway.media_materializer import materialize_response_media

    env = _FakeRemoteEnvironment()
    result = materialize_response_media(
        "MEDIA:ssh://build.example/srv/captures/page%20one.png",
        task_id="session-1",
        ssh_alias="build.example",
        env=env,
        cache_dir=tmp_path,
    )

    assert env.calls[0]["source_path"] == "/srv/captures/page one.png"
    assert result.response == f"MEDIA:{result.materialized_paths[0]}"


def test_materializes_extensionless_ssh_uri(tmp_path):
    from gateway.media_materializer import materialize_response_media

    env = _FakeRemoteEnvironment(payload=b"server configuration")
    result = materialize_response_media(
        "MEDIA:ssh://build.example/srv/config/Caddyfile",
        task_id="session-1",
        ssh_alias="build.example",
        env=env,
        cache_dir=tmp_path,
    )

    assert env.calls[0]["source_path"] == "/srv/config/Caddyfile"
    assert len(result.materialized_paths) == 1
    assert Path(result.materialized_paths[0]).suffix == ""
    assert result.response == f"MEDIA:{result.materialized_paths[0]}"


def test_no_active_materializing_environment_keeps_response_unchanged(tmp_path):
    from gateway.media_materializer import materialize_response_media

    response = "MEDIA:/srv/captures/page.png"
    result = materialize_response_media(
        response,
        task_id="session-1",
        env=None,
        cache_dir=tmp_path,
    )

    assert result.response == response
    assert result.materialized_paths == ()
    assert result.failures == ()


def test_gateway_materializes_with_current_session_binding(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    from tools import terminal_tool

    env = _FakeRemoteEnvironment()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(
        gateway_run,
        "get_ssh_binding",
        lambda session_key: type("Binding", (), {"alias": "build.example"})(),
    )
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: env)

    runner = GatewayRunner.__new__(GatewayRunner)
    result = asyncio.run(
        runner._materialize_media_for_delivery(
            "Screenshot\nMEDIA:ssh://build.example/srv/captures/page.png",
            session_id="session-1",
            session_key="agent:main:feishu:dm:chat:thread",
        )
    )

    assert len(env.calls) == 1
    assert env.calls[0]["source_path"] == "/srv/captures/page.png"
    assert result.failures == ()
    assert len(result.materialized_paths) == 1
    assert Path(result.materialized_paths[0]).is_file()
    assert result.response == f"Screenshot\nMEDIA:{result.materialized_paths[0]}"


def test_materialized_remote_image_keeps_feishu_inline_reply_contract(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
    from gateway.session import SessionSource
    from tools import terminal_tool
    from unittest.mock import AsyncMock, MagicMock

    env = _FakeRemoteEnvironment()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(
        gateway_run,
        "get_ssh_binding",
        lambda session_key: type("Binding", (), {"alias": "build.example"})(),
    )
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: env)

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._reply_anchor_for_event = MagicMock(return_value="om_user")
    runner._thread_metadata_for_source = MagicMock(
        return_value={"thread_id": "omt_thread", "reply_to_message_id": "om_user"}
    )
    materialized = asyncio.run(
        runner._materialize_media_for_delivery(
            "Screenshot\nMEDIA:ssh://build.example/srv/captures/page.png",
            session_id="session-1",
            session_key="agent:main:feishu:dm:chat:thread",
        )
    )

    adapter = MagicMock()
    adapter.name = "Feishu"
    adapter.extract_media = MagicMock(side_effect=BasePlatformAdapter.extract_media)
    adapter.extract_images = MagicMock(side_effect=lambda text: ([], text))
    adapter.send_image_file = AsyncMock(
        return_value=SendResult(success=True, message_id="om_inline")
    )
    adapter.send_multiple_images = AsyncMock()
    adapter.delete_message = AsyncMock(return_value=True)
    event = MessageEvent(
        text="user",
        source=SessionSource(
            platform=Platform.FEISHU,
            chat_id="oc_chat",
            user_id="ou_user",
            thread_id="omt_thread",
        ),
        message_id="om_user",
    )

    asyncio.run(
        runner._deliver_media_from_response(
            materialized.response,
            event,
            adapter,
            streamed_message_id="om_streamed",
        )
    )

    adapter.send_image_file.assert_awaited_once()
    kwargs = adapter.send_image_file.await_args.kwargs
    assert kwargs["image_path"] == materialized.materialized_paths[0]
    assert kwargs["caption"] == "Screenshot"
    assert kwargs["reply_to"] == "om_user"
    assert kwargs["metadata"] == {
        "thread_id": "omt_thread",
        "reply_to_message_id": "om_user",
    }
    adapter.delete_message.assert_awaited_once_with("oc_chat", "om_streamed")
    adapter.send_multiple_images.assert_not_awaited()
