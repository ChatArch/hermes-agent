"""Feishu card plugin registration."""

from __future__ import annotations

from gateway.cards import Card, CardHeader, Markdown
from gateway.cards.actions import CardActionContext, CardActionResponse, register_card_action
from plugins.feishu_card.tools import (
    FEISHU_CARD_SCHEMA,
    feishu_card_tool_async,
    resolve_interaction_request,
)


async def _default_authorize_action(ctx: CardActionContext) -> CardActionResponse:
    flow_id = ctx.payload.get("flow_id", "")
    suffix = f"\n\n流程：`{flow_id}`" if flow_id else ""
    return CardActionResponse.replace_card(
        Card(
            header=CardHeader(title="已打开授权链接", color="blue"),
            elements=[Markdown("请在打开的授权页面完成授权。" + suffix)],
        )
    )


async def _default_open_link_action(ctx: CardActionContext) -> CardActionResponse:
    return CardActionResponse.noop()


async def _default_card_respond_action(ctx: CardActionContext) -> CardActionResponse:
    request_id = ctx.payload.get("request_id", "")
    resolved = resolve_interaction_request(
        ctx.session_key,
        request_id,
        ctx.payload,
        chat_id=ctx.chat_id,
        message_id=ctx.message_id,
    )
    if not resolved:
        return CardActionResponse.noop()
    choice = ctx.payload.get("choice") or ctx.payload.get("button_text") or "已选择"
    return CardActionResponse.replace_card(
        Card(
            header=CardHeader(title="已收到反馈", color="green"),
            elements=[Markdown(f"已收到你的选择：`{choice}`")],
        )
    )


async def _default_cancel_action(ctx: CardActionContext) -> CardActionResponse:
    flow_id = ctx.payload.get("flow_id", "")
    suffix = f"\n\n流程：`{flow_id}`" if flow_id else ""
    return CardActionResponse.replace_card(
        Card(
            header=CardHeader(title="授权已取消", color="red"),
            elements=[Markdown("已取消授权流程。" + suffix)],
        )
    )


def _register_default_card_actions() -> None:
    register_card_action("card.respond", _default_card_respond_action)
    register_card_action("auth.open_link", _default_open_link_action)
    register_card_action("auth.authorize", _default_authorize_action)
    register_card_action("auth.cancel", _default_cancel_action)


def register(ctx):
    _register_default_card_actions()
    ctx.register_tool(
        name="feishu_card",
        toolset="messaging",
        schema=FEISHU_CARD_SCHEMA,
        handler=lambda args, **_kw: feishu_card_tool_async(args),
        check_fn=lambda: True,
        is_async=True,
        description="Build, preview, and send flexible Feishu/Lark interactive cards.",
        emoji="🧩",
    )
