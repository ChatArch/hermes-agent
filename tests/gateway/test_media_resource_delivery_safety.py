"""Regression tests for typed MEDIA resource URI delivery safety.

The user-facing incident shape is a normal text line (often a clickable URL)
followed by an SSH-owned media artifact directive:

    https://example.invalid/account/scan/login/[REDACTED]?/api/login/qrcode
    MEDIA:ssh://zhihong.oray/path/to/qr.png

The URL is content.  The MEDIA:ssh line is an internal Hermes delivery control.
Delivery surfaces must either materialize it into a gateway-local MEDIA:/path
before extraction, or fail closed by removing the directive from visible text.
They must never send the raw resource directive to the user.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class _FakeRemoteEnvironment:
    supports_file_materialization = True

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def materialize_file(self, source_path, destination, *, max_bytes, timeout, require_recent_seconds=None):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\x89PNG\r\n\x1a\nremote")
        self.calls.append(
            {
                "source_path": source_path,
                "destination": str(destination),
                "max_bytes": max_bytes,
                "timeout": timeout,
                "require_recent_seconds": require_recent_seconds,
            }
        )
        return {"path": str(destination), "size": destination.stat().st_size}


def _qr_content() -> str:
    return (
        "https://www.zhihu.com/account/scan/login/[REDACTED]?/api/login/qrcode\n"
        "MEDIA:ssh://zhihong.oray/home/zhihong/Playground/projects/chatarch/"
        "08-05-chatpost-login-cli-practice/playground/qr-live-20260805-now/"
        "zhihu-qr-login-live.png"
    )


def _media_only_content() -> str:
    return (
        "MEDIA:ssh://zhihong.oray/home/zhihong/Playground/projects/chatarch/"
        "08-05-chatpost-login-cli-practice/playground/qr-live-20260805-now/"
        "zhihu-qr-login-live.png"
    )


def test_materializer_handles_url_plus_ssh_media_shape(tmp_path):
    from gateway.media_materializer import materialize_response_media

    env = _FakeRemoteEnvironment()
    result = materialize_response_media(
        _qr_content(),
        task_id="session-1",
        ssh_alias="zhihong.oray",
        env=env,
        cache_dir=tmp_path,
    )

    assert "https://www.zhihu.com/account/scan/login/[REDACTED]" in result.response
    assert "MEDIA:ssh://" not in result.response
    assert result.response.count("MEDIA:") == 1
    assert len(result.materialized_paths) == 1
    assert Path(result.materialized_paths[0]).is_file()
    assert result.response.endswith(f"MEDIA:{result.materialized_paths[0]}")
    assert env.calls[0]["source_path"].endswith("/zhihu-qr-login-live.png")


def test_materializer_handles_media_only_ssh_resource_shape(tmp_path):
    from gateway.media_materializer import materialize_response_media

    env = _FakeRemoteEnvironment()
    result = materialize_response_media(
        _media_only_content(),
        task_id="session-1",
        ssh_alias="zhihong.oray",
        env=env,
        cache_dir=tmp_path,
    )

    assert "MEDIA:ssh://" not in result.response
    assert result.response == f"MEDIA:{result.materialized_paths[0]}"
    assert len(result.materialized_paths) == 1
    assert Path(result.materialized_paths[0]).is_file()
    assert result.failures == ()
    assert env.calls[0]["source_path"].endswith("/zhihu-qr-login-live.png")


def test_extract_media_fails_closed_for_unmaterialized_resource_uri():
    media, cleaned = BasePlatformAdapter.extract_media(_qr_content())

    assert media == []
    assert "https://www.zhihu.com/account/scan/login/[REDACTED]" in cleaned
    assert "MEDIA:ssh://" not in cleaned
    assert "zhihu-qr-login-live.png" not in cleaned


def test_extract_media_fails_closed_for_unmaterialized_media_only_resource_uri():
    media, cleaned = BasePlatformAdapter.extract_media(_media_only_content())

    assert media == []
    assert cleaned == ""


def test_display_strip_preserves_resource_uri_examples():
    content = (
        "Example:\n"
        "```\nMEDIA:ssh://zhihong.oray/path/example.png\n```\n"
        "> MEDIA:ssh://zhihong.oray/path/quoted.png\n"
        '{"example":"MEDIA:ssh://zhihong.oray/path/json.png"}\n'
        "Real one:\nMEDIA:ssh://zhihong.oray/path/real.png\n"
    )

    cleaned = BasePlatformAdapter.strip_media_directives_for_display(content)

    assert "MEDIA:ssh://zhihong.oray/path/example.png" in cleaned
    assert "MEDIA:ssh://zhihong.oray/path/quoted.png" in cleaned
    assert '"MEDIA:ssh://zhihong.oray/path/json.png"' in cleaned
    assert "MEDIA:ssh://zhihong.oray/path/real.png" not in cleaned


@pytest.mark.asyncio
async def test_streamed_delivery_materializes_resource_uri_before_extracting(tmp_path, monkeypatch):
    from gateway.media_materializer import materialize_response_media

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
    adapter = SimpleNamespace(
        name="Feishu",
        extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images,
        send_image_file=AsyncMock(return_value=SendResult(success=True, message_id="om_img")),
        send_multiple_images=AsyncMock(),
        send_voice=AsyncMock(return_value=SendResult(success=True, message_id="om_voice")),
        send_video=AsyncMock(return_value=SendResult(success=True, message_id="om_video")),
        send_document=AsyncMock(return_value=SendResult(success=True, message_id="om_doc")),
        delete_message=AsyncMock(return_value=True),
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._reply_anchor_for_event = MagicMock(return_value="om_user")
    runner._thread_metadata_for_source = MagicMock(
        return_value={"thread_id": "omt_thread", "reply_to_message_id": "om_user"}
    )
    runner._materialize_media_for_delivery = AsyncMock(
        return_value=materialize_response_media(
            _qr_content(),
            task_id="session-1",
            ssh_alias="zhihong.oray",
            env=_FakeRemoteEnvironment(),
            cache_dir=tmp_path,
        )
    )

    await runner._deliver_media_from_response(
        _qr_content(),
        event,
        adapter,
        streamed_message_id="om_streamed_text",
        session_id="session-1",
        session_key="feishu:oc_chat:ou_user",
    )

    runner._materialize_media_for_delivery.assert_awaited_once()
    adapter.send_image_file.assert_awaited_once()
    kwargs = adapter.send_image_file.await_args.kwargs
    assert kwargs["caption"].startswith("https://www.zhihu.com/account/scan/login/[REDACTED]")
    assert "MEDIA:ssh://" not in kwargs["caption"]
    assert kwargs["image_path"].endswith(".png")
    adapter.delete_message.assert_awaited_once_with("oc_chat", "om_streamed_text")


@pytest.mark.asyncio
async def test_streamed_delivery_materializes_media_only_resource_uri_before_extracting(tmp_path):
    from gateway.media_materializer import materialize_response_media

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
    adapter = SimpleNamespace(
        name="Feishu",
        extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images,
        send_image_file=AsyncMock(return_value=SendResult(success=True, message_id="om_img")),
        send_multiple_images=AsyncMock(),
        send_voice=AsyncMock(return_value=SendResult(success=True, message_id="om_voice")),
        send_video=AsyncMock(return_value=SendResult(success=True, message_id="om_video")),
        send_document=AsyncMock(return_value=SendResult(success=True, message_id="om_doc")),
        delete_message=AsyncMock(return_value=True),
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._reply_anchor_for_event = MagicMock(return_value="om_user")
    runner._thread_metadata_for_source = MagicMock(
        return_value={"thread_id": "omt_thread", "reply_to_message_id": "om_user"}
    )
    runner._materialize_media_for_delivery = AsyncMock(
        return_value=materialize_response_media(
            _media_only_content(),
            task_id="session-1",
            ssh_alias="zhihong.oray",
            env=_FakeRemoteEnvironment(),
            cache_dir=tmp_path,
        )
    )

    await runner._deliver_media_from_response(
        _media_only_content(),
        event,
        adapter,
        streamed_message_id="om_streamed_text",
        session_id="session-1",
        session_key="feishu:oc_chat:ou_user",
    )

    runner._materialize_media_for_delivery.assert_awaited_once()
    adapter.send_image_file.assert_awaited_once()
    kwargs = adapter.send_image_file.await_args.kwargs
    assert kwargs["caption"] is None
    assert "MEDIA:ssh://" not in kwargs["image_path"]
    assert kwargs["image_path"].endswith(".png")
    adapter.send_multiple_images.assert_not_awaited()
    adapter.delete_message.assert_awaited_once_with("oc_chat", "om_streamed_text")
