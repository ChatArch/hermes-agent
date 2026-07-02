import pytest
from unittest.mock import AsyncMock, MagicMock

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


def _media_adapter_for_image(image, *, name="Feishu", caption="Caption before image", result=None):
    adapter = MagicMock()
    adapter.name = name
    adapter.extract_media = MagicMock(
        side_effect=lambda text: (
            [(str(image), False)],
            text.replace(f"MEDIA:{image}", "").strip(),
        )
    )
    adapter.extract_images = MagicMock(return_value=([], caption))
    adapter.extract_local_files = MagicMock(return_value=([], caption))
    adapter.send_image_file = AsyncMock(
        return_value=result if result is not None else SendResult(success=True, message_id="om_inline")
    )
    adapter.send_multiple_images = AsyncMock()
    adapter.delete_message = AsyncMock(return_value=True)
    return adapter


@pytest.mark.asyncio
async def test_feishu_media_response_uses_inline_caption_path_with_reply_anchor(tmp_path):
    """Natural MEDIA delivery should become one Feishu post with text+inline image.

    The important part for Feishu topics is that the same natural reply anchor
    used by ordinary conversation sends is preserved.  Without it, Feishu falls
    back to a thread_id create payload that can fail validation or land outside
    the current topic.
    """
    image = tmp_path / "chart.png"
    image.write_bytes(b"fake image bytes")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._reply_anchor_for_event = MagicMock(return_value="om_user")
    runner._thread_metadata_for_source = MagicMock(
        return_value={"thread_id": "omt_thread", "reply_to_message_id": "om_user"}
    )

    adapter = _media_adapter_for_image(image)

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

    await runner._deliver_media_from_response(
        f"Caption before image\nMEDIA:{image}",
        event,
        adapter,
        streamed_message_id="om_streamed_text",
    )

    runner._thread_metadata_for_source.assert_called_once_with(event.source, "om_user")
    adapter.send_image_file.assert_awaited_once()
    kwargs = adapter.send_image_file.await_args.kwargs
    assert kwargs["chat_id"] == "oc_chat"
    assert kwargs["image_path"] == str(image)
    assert kwargs["caption"] == "Caption before image"
    assert kwargs["reply_to"] == "om_user"
    assert kwargs["metadata"] == {"thread_id": "omt_thread", "reply_to_message_id": "om_user"}
    adapter.delete_message.assert_awaited_once_with("oc_chat", "om_streamed_text")
    adapter.send_multiple_images.assert_not_awaited()


@pytest.mark.asyncio
async def test_feishu_inline_image_does_not_delete_streamed_text_on_failed_send(tmp_path):
    image = tmp_path / "chart.png"
    image.write_bytes(b"fake image bytes")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._reply_anchor_for_event = MagicMock(return_value="om_user")
    runner._thread_metadata_for_source = MagicMock(
        return_value={"thread_id": "omt_thread", "reply_to_message_id": "om_user"}
    )
    adapter = _media_adapter_for_image(
        image,
        result=SendResult(success=False, error="field validation failed"),
    )
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

    await runner._deliver_media_from_response(
        f"Caption before image\nMEDIA:{image}",
        event,
        adapter,
        streamed_message_id="om_streamed_text",
    )

    adapter.send_image_file.assert_awaited_once()
    adapter.delete_message.assert_not_awaited()
    adapter.send_multiple_images.assert_not_awaited()


def test_feishu_thread_metadata_preserves_reply_anchor_for_adapter_fallback():
    """Metadata-only Feishu sends must still have enough data to reply in-topic."""
    runner = GatewayRunner.__new__(GatewayRunner)

    metadata = runner._thread_metadata_for_target(
        Platform.FEISHU,
        "oc_chat",
        "omt_thread",
        chat_type="group",
        reply_to_message_id="om_user",
    )

    assert metadata == {"thread_id": "omt_thread", "reply_to_message_id": "om_user"}


def test_feishu_thread_metadata_omits_empty_reply_anchor():
    runner = GatewayRunner.__new__(GatewayRunner)

    metadata = runner._thread_metadata_for_target(
        Platform.FEISHU,
        "oc_chat",
        "omt_thread",
        chat_type="group",
        reply_to_message_id=None,
    )

    assert metadata == {"thread_id": "omt_thread"}


@pytest.mark.asyncio
async def test_non_feishu_media_response_keeps_generic_batch_path(tmp_path):
    image = tmp_path / "chart.png"
    image.write_bytes(b"fake image bytes")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._reply_anchor_for_event = MagicMock(return_value=None)
    runner._thread_metadata_for_source = MagicMock(return_value=None)

    adapter = _media_adapter_for_image(image, name="Telegram")

    event = MessageEvent(
        text="user",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            user_id="u",
        ),
        message_id="m1",
    )

    await runner._deliver_media_from_response(
        f"Caption before image\nMEDIA:{image}",
        event,
        adapter,
    )

    adapter.send_multiple_images.assert_awaited_once()
    adapter.send_image_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_feishu_multiple_images_keep_generic_batch_path(tmp_path):
    image1 = tmp_path / "one.png"
    image2 = tmp_path / "two.png"
    image1.write_bytes(b"fake image bytes 1")
    image2.write_bytes(b"fake image bytes 2")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._reply_anchor_for_event = MagicMock(return_value="om_user")
    runner._thread_metadata_for_source = MagicMock(return_value={"thread_id": "omt_thread", "reply_to_message_id": "om_user"})

    adapter = MagicMock()
    adapter.name = "Feishu"
    adapter.extract_media = MagicMock(return_value=([(str(image1), False), (str(image2), False)], "Caption"))
    adapter.extract_images = MagicMock(return_value=([], "Caption"))
    adapter.extract_local_files = MagicMock(return_value=([], "Caption"))
    adapter.send_image_file = AsyncMock(return_value=SendResult(success=True, message_id="unused"))
    adapter.send_multiple_images = AsyncMock()

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

    await runner._deliver_media_from_response(
        f"Caption\nMEDIA:{image1}\nMEDIA:{image2}",
        event,
        adapter,
    )

    adapter.send_multiple_images.assert_awaited_once()
    adapter.send_image_file.assert_not_awaited()


class _FeishuBackgroundInlineAdapter(BasePlatformAdapter):
    """Concrete adapter that exercises BasePlatformAdapter final delivery."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="fake"), Platform.FEISHU)
        self.sent_text = []
        self.inline_images = []
        self.image_batches = []

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent_text.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="om_text")

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs):
        self.inline_images.append(
            {
                "chat_id": chat_id,
                "image_path": image_path,
                "caption": caption,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="om_inline")

    async def send_multiple_images(self, chat_id, images, metadata=None, human_delay=0.0):
        self.image_batches.append(
            {
                "chat_id": chat_id,
                "images": images,
                "metadata": metadata,
                "human_delay": human_delay,
            }
        )

    async def send_typing(self, chat_id, metadata=None):
        pass

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


@pytest.mark.asyncio
async def test_feishu_normal_final_response_uses_single_inline_post(tmp_path):
    """Non-streaming final delivery must not split text and MEDIA images."""
    image = tmp_path / "normal-final.png"
    image.write_bytes(b"fake image bytes")
    adapter = _FeishuBackgroundInlineAdapter()

    async def handler(event):
        return f"Normal final summary\nMEDIA:{image}"

    adapter.set_message_handler(handler)
    event = MessageEvent(
        text="please summarize with screenshot",
        source=SessionSource(
            platform=Platform.FEISHU,
            chat_id="oc_chat",
            user_id="ou_user",
            thread_id="omt_thread",
            chat_type="group",
        ),
        message_id="om_user_msg",
        reply_to_message_id="om_thread_root",
    )

    await adapter._process_message_background(event, build_session_key(event.source))

    assert adapter.inline_images == [
        {
            "chat_id": "oc_chat",
            "image_path": str(image),
            "caption": "Normal final summary",
            "reply_to": "om_thread_root",
            "metadata": {
                "thread_id": "omt_thread",
                "reply_to_message_id": "om_thread_root",
                "notify": True,
            },
        }
    ]
    assert adapter.sent_text == []
    assert adapter.image_batches == []


def test_base_feishu_thread_metadata_preserves_reply_anchor():
    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_chat",
        user_id="ou_user",
        thread_id="omt_thread",
        chat_type="group",
    )

    from gateway.platforms.base import _thread_metadata_for_source

    assert _thread_metadata_for_source(source, "om_thread_root") == {
        "thread_id": "omt_thread",
        "reply_to_message_id": "om_thread_root",
    }
