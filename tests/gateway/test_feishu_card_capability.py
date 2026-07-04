import asyncio
from types import SimpleNamespace

import pytest

from gateway.cards import (
    Actions,
    Button,
    Card,
    CardHeader,
    Markdown,
    Note,
    RawFeishuCard,
    build_feishu_authorization_card,
)
from gateway.cards.actions import CardActionContext, CardActionResponse, CardActionRegistry
from gateway.cards.renderers.feishu import render_feishu_card
from gateway.platforms.feishu import FeishuAdapter


def test_render_feishu_card_supports_composable_authorization_shape():
    card = Card(
        header=CardHeader(title="飞书授权请求", color="blue"),
        elements=[
            Markdown("需要你完成飞书授权后，我才能继续。"),
            Actions(
                buttons=[
                    Button(
                        text="授权",
                        style="primary",
                        action="auth.authorize",
                        url="https://accounts.feishu.cn/oauth/v1/device/verify?user_code=REDACTED",
                        payload={"flow_id": "flow-1"},
                    ),
                    Button(text="取消", style="danger", action="auth.cancel"),
                ],
                layout="equal",
            ),
            Note("Hermes 将在授权完成后继续。"),
        ],
    )

    rendered = render_feishu_card(card, session_key="session-1")

    assert rendered["config"]["wide_screen_mode"] is True
    assert rendered["header"]["template"] == "blue"
    assert rendered["header"]["title"] == {"tag": "plain_text", "content": "飞书授权请求"}
    assert rendered["elements"][0] == {"tag": "markdown", "content": "需要你完成飞书授权后，我才能继续。"}

    button_columns = rendered["elements"][1]
    assert button_columns["tag"] == "column_set"
    assert button_columns["flex_mode"] == "bisect"
    authorize_button = button_columns["columns"][0]["elements"][0]
    cancel_button = button_columns["columns"][1]["elements"][0]

    assert authorize_button["tag"] == "button"
    assert authorize_button["text"]["content"] == "授权"
    assert authorize_button["type"] == "primary"
    assert authorize_button["url"].startswith("https://accounts.feishu.cn/oauth/v1/device/verify")
    assert authorize_button["value"] == {
        "action": "auth.authorize",
        "session_key": "session-1",
        "flow_id": "flow-1",
    }
    assert cancel_button["text"]["content"] == "取消"
    assert cancel_button["value"] == {"action": "auth.cancel", "session_key": "session-1"}
    assert rendered["elements"][2] == {
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "Hermes 将在授权完成后继续。"}],
    }


def test_feishu_adapter_send_card_sends_interactive_payload(monkeypatch):
    adapter = FeishuAdapter.__new__(FeishuAdapter)
    adapter._client = object()
    calls = []

    async def fake_send_with_retry(**kwargs):
        calls.append(kwargs)

        class Response:
            code = 0
            msg = "ok"
            data = SimpleNamespace(message_id="om_card", thread_id="omt_card")

            def success(self):
                return True

        return Response()

    monkeypatch.setattr(adapter, "_feishu_send_with_retry", fake_send_with_retry)
    card = render_feishu_card(
        Card(header=CardHeader(title="自定义", color="purple"), elements=[Markdown("内容")])
    )

    result = asyncio.run(adapter.send_card("oc_chat", card, metadata={"thread_id": "omt_root"}))

    assert result.success is True
    assert result.message_id == "om_card"
    assert calls[0]["chat_id"] == "oc_chat"
    assert calls[0]["msg_type"] == "interactive"
    assert calls[0]["payload"].startswith('{"config"')
    assert calls[0]["metadata"] == {"thread_id": "omt_root"}


def test_raw_feishu_card_escape_hatch_is_returned_without_rewriting():
    raw = {"config": {"wide_screen_mode": False}, "elements": [{"tag": "hr"}]}

    assert render_feishu_card(RawFeishuCard(raw)) is raw


def test_build_feishu_authorization_card_is_generic_card_composition():
    card = build_feishu_authorization_card(
        verification_url="https://accounts.feishu.cn/oauth/v1/device/verify?user_code=REDACTED",
        flow_id="flow-2",
        title="飞书授权请求",
        body="需要授权。",
    )

    assert isinstance(card, Card)
    rendered = render_feishu_card(card, session_key="session-2")
    buttons = rendered["elements"][1]["columns"]
    assert buttons[0]["elements"][0]["value"]["action"] == "auth.authorize"
    assert buttons[0]["elements"][0]["value"]["flow_id"] == "flow-2"
    assert buttons[1]["elements"][0]["value"]["action"] == "auth.cancel"


def test_card_action_registry_routes_registered_handlers():
    registry = CardActionRegistry()

    async def authorize(ctx: CardActionContext) -> CardActionResponse:
        return CardActionResponse.replace_card(
            Card(header=CardHeader(title="授权已确认", color="green"), elements=[Markdown(ctx.payload["flow_id"])])
        )

    registry.register("auth.authorize", authorize)

    response = asyncio.run(
        registry.dispatch(
            CardActionContext(
                action="auth.authorize",
                payload={"flow_id": "flow-3"},
                user_id="ou_user",
                chat_id="oc_chat",
                message_id="om_msg",
                session_key="session-3",
            )
        )
    )

    assert response.kind == "replace_card"
    assert response.card.header.title == "授权已确认"
    assert response.card.elements[0].content == "flow-3"


def test_card_action_registry_rejects_unknown_action():
    registry = CardActionRegistry()

    with pytest.raises(KeyError, match="No card action handler registered"):
        asyncio.run(
            registry.dispatch(
                CardActionContext(
                    action="missing.action",
                    payload={},
                    user_id="ou_user",
                    chat_id="oc_chat",
                    message_id="om_msg",
                    session_key="session-4",
                )
            )
        )
