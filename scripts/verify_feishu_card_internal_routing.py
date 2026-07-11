#!/usr/bin/env python3
"""Offline verifier for Hermes Feishu card routing.

This script exercises the Python implementation without contacting Feishu:
1. Feishu inbound normalization preserves the triggering om_ message id on
   SessionSource.
2. GatewayRunner session ContextVars expose that id as HERMES_SESSION_MESSAGE_ID.
3. feishu_card sends through the live Hermes adapter target using metadata with
   the same reply anchor.
4. FeishuAdapter builds an SDK-style message.reply request for interactive cards
   with reply_in_thread=True and never falls back to create when the anchor is
   present.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def _build_inbound_feishu_event():
    from gateway.config import PlatformConfig
    from gateway.platforms.feishu import FeishuAdapter

    adapter = FeishuAdapter(PlatformConfig())
    adapter._dispatch_inbound_event = AsyncMock()
    adapter.get_chat_info = AsyncMock(return_value={"chat_id": "oc_chat", "name": "Feishu Group", "type": "group"})
    adapter._resolve_sender_profile = AsyncMock(
        return_value={"user_id": "ou_user", "user_name": "User", "user_id_alt": None}
    )

    message = SimpleNamespace(
        chat_id="oc_chat",
        thread_id="omt_current",
        root_id="omt_current",
        parent_id=None,
        upper_message_id=None,
        message_type="text",
        content=json.dumps({"text": "/verify card routing"}, ensure_ascii=False),
        message_id="om_trigger",
        mentions=[],
    )
    await adapter._process_inbound_message(
        data=SimpleNamespace(event=SimpleNamespace(message=message)),
        message=message,
        sender_id=SimpleNamespace(open_id="ou_user", user_id=None, union_id=None),
        is_bot=False,
        chat_type="group",
        message_id="om_trigger",
    )
    call = adapter._dispatch_inbound_event.await_args
    if call is None:
        raise AssertionError("inbound event was not dispatched")
    event = call.args[0]
    _assert(event.source.message_id == "om_trigger", "SessionSource.message_id was not populated")
    _assert(event.source.thread_id == "omt_current", "SessionSource.thread_id was not preserved")
    return event


def _set_and_verify_session_env(event):
    from gateway.config import Platform
    with contextlib.redirect_stderr(io.StringIO()):
        from gateway.run import GatewayRunner
    from gateway.session import SessionContext
    from gateway.session_context import get_session_env

    context = SessionContext(
        source=event.source,
        connected_platforms=[Platform.FEISHU],
        home_channels={},
        session_key="feishu:oc_chat:omt_current",
        session_id="session-id",
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    tokens = runner._set_session_env(context)
    _assert(get_session_env("HERMES_SESSION_PLATFORM") == "feishu", "session platform mismatch")
    _assert(get_session_env("HERMES_SESSION_CHAT_ID") == "oc_chat", "session chat id mismatch")
    _assert(get_session_env("HERMES_SESSION_THREAD_ID") == "omt_current", "session thread id mismatch")
    _assert(get_session_env("HERMES_SESSION_MESSAGE_ID") == "om_trigger", "session message id mismatch")
    return tokens


async def _verify_feishu_card_tool_uses_session_anchor():
    from gateway.config import Platform
    import gateway.run as gateway_run
    from plugins.feishu_card.tools import feishu_card_tool_async

    sent = []

    class FakeAdapter:
        async def send_card(self, chat_id, card, *, reply_to=None, metadata=None):
            sent.append({"chat_id": chat_id, "card": card, "reply_to": reply_to, "metadata": metadata})
            return SimpleNamespace(success=True, message_id="om_card", thread_id="omt_current", error=None)

    old_ref = gateway_run._gateway_runner_ref
    gateway_run._gateway_runner_ref = lambda: SimpleNamespace(adapters={Platform.FEISHU: FakeAdapter()})
    try:
        raw = await feishu_card_tool_async(
            {
                "action": "send",
                "card": {"elements": [{"type": "markdown", "content": "routing check"}]},
            }
        )
    finally:
        gateway_run._gateway_runner_ref = old_ref
    result = json.loads(raw)
    _assert(result.get("success") is True, f"feishu_card send failed: {raw}")
    _assert(bool(sent), "feishu_card did not call adapter.send_card")
    _assert(sent[0]["chat_id"] == "oc_chat", "feishu_card used wrong chat id")
    _assert(sent[0]["reply_to"] is None, "feishu_card should rely on Hermes metadata, not an explicit plugin reply_to")
    _assert(
        sent[0]["metadata"] == {"thread_id": "omt_current", "reply_to_message_id": "om_trigger"},
        f"unexpected card metadata: {sent[0]['metadata']!r}",
    )
    return sent[0]


async def _verify_adapter_builds_sdk_reply_request():
    from gateway.platforms.feishu import FeishuAdapter

    replies = []
    creates = []

    class MessageApi:
        @staticmethod
        def reply(request):
            replies.append(request)
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_card", thread_id="omt_current"))

        @staticmethod
        def create(request):
            creates.append(request)
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_created"))

    adapter = FeishuAdapter.__new__(FeishuAdapter)
    adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=MessageApi)))

    def reply_body(**kwargs):
        return SimpleNamespace(
            content=kwargs["content"],
            msg_type=kwargs["msg_type"],
            reply_in_thread=kwargs["reply_in_thread"],
            uuid=kwargs["uuid_value"],
        )

    adapter._build_reply_message_body = reply_body
    adapter._build_reply_message_request = lambda message_id, request_body: SimpleNamespace(
        message_id=message_id,
        request_body=request_body,
    )

    payload = json.dumps({"elements": [{"tag": "markdown", "content": "routing check"}]}, ensure_ascii=False)
    response = await adapter._send_raw_message(
        chat_id="oc_chat",
        msg_type="interactive",
        payload=payload,
        reply_to=None,
        metadata={"thread_id": "omt_current", "reply_to_message_id": "om_trigger"},
    )

    _assert(response.success(), "fake Feishu reply response failed")
    _assert(creates == [], "adapter unexpectedly used message.create")
    _assert(len(replies) == 1, "adapter did not call message.reply exactly once")
    request = replies[0]
    _assert(request.message_id == "om_trigger", "reply request used wrong om_ message id")
    _assert(request.request_body.msg_type == "interactive", "reply body msg_type mismatch")
    _assert(request.request_body.content == payload, "reply body content mismatch")
    _assert(request.request_body.reply_in_thread is True, "reply body reply_in_thread must be true")
    json.loads(request.request_body.content)
    return {
        "message_id": request.message_id,
        "msg_type": request.request_body.msg_type,
        "reply_in_thread": request.request_body.reply_in_thread,
        "content_is_json_string": isinstance(request.request_body.content, str),
        "has_uuid": bool(request.request_body.uuid),
    }


async def main() -> None:
    from gateway.session_context import clear_session_vars

    event = await _build_inbound_feishu_event()
    tokens = _set_and_verify_session_env(event)
    try:
        card_send = await _verify_feishu_card_tool_uses_session_anchor()
        sdk_reply = await _verify_adapter_builds_sdk_reply_request()
    finally:
        clear_session_vars(tokens)

    print(
        json.dumps(
            {
                "ok": True,
                "inbound_source": {
                    "chat_id": event.source.chat_id,
                    "thread_id": event.source.thread_id,
                    "message_id": event.source.message_id,
                },
                "feishu_card_metadata": card_send["metadata"],
                "sdk_reply_request": sdk_reply,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
