from typing import Any

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class _FeishuThreadAdapter:
    def __init__(self):
        self.created_threads = []
        self.edits = []
        self.deletes = []
        self.released_guards = []

    async def create_thread(self, chat_id, content, *, reply_to, metadata=None):
        self.created_threads.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="om_seed", thread_id="omt_thread")

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
                "finalize": finalize,
            }
        )
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id):
        self.deletes.append({"chat_id": chat_id, "message_id": message_id})
        return True

    def release_retargeted_session_guard(self, session_key):
        self.released_guards.append(session_key)
        return True


@pytest.mark.asyncio
async def test_feishu_thread_launcher_deletes_seed_and_returns_final_for_bottom_delivery():
    """A /t-created Feishu thread must remove the technical top seed.

    Regression guard for the visible contract: the initial thread seed is a
    temporary placeholder ("⏳") used only to create the native Feishu thread.
    After the agent finishes, that top seed must be deleted/withdrawn rather
    than edited into any visible marker text. The final answer is returned to
    BasePlatformAdapter so the normal delivery path sends a fresh message at
    the bottom of the thread.
    """
    adapter = _FeishuThreadAdapter()
    runner: Any = object.__new__(GatewayRunner)
    runner.adapters = {Platform.FEISHU: adapter}
    runner._session_key_for_source = lambda source: build_session_key(source)

    dispatched = {}

    async def _fake_dispatch(event, source, session_key):
        dispatched["event"] = event
        dispatched["source"] = source
        dispatched["thread_key"] = session_key
        return "final answer"

    runner._dispatch_event_to_agent = _fake_dispatch

    event = MessageEvent(
        text="/t do work",
        message_type=MessageType.COMMAND,
        source=SessionSource(
            platform=Platform.FEISHU,
            chat_id="oc_chat",
            chat_type="dm",
            user_id="ou_user",
        ),
        message_id="om_command",
    )

    result = await runner._dispatch_event_in_feishu_thread(
        event,
        "do work",
        command_name="thread",
        reply_text="do work",
    )

    assert adapter.created_threads == [
        {
            "chat_id": "oc_chat",
            "content": "⏳",
            "reply_to": "om_command",
            "metadata": None,
        }
    ]
    assert adapter.deletes == [{"chat_id": "oc_chat", "message_id": "om_seed"}]
    assert adapter.edits == []
    assert result == "final answer"

    dispatched_event = dispatched["event"]
    assert dispatched_event.source.thread_id == "omt_thread"
    assert dispatched_event.reply_to_message_id == "om_command"
    assert dispatched["thread_key"].endswith(":omt_thread")
