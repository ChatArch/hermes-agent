"""Feishu card plugin registration."""

from __future__ import annotations

from plugins.feishu_card.tools import FEISHU_CARD_SCHEMA, feishu_card_tool_async


def register(ctx):
    ctx.register_tool(
        name="feishu_card",
        toolset="messaging",
        schema=FEISHU_CARD_SCHEMA,
        handler=lambda args, **_kw: feishu_card_tool_async(args),
        check_fn=lambda: True,
        is_async=True,
        description="Build, preview, and eventually send flexible Feishu/Lark interactive cards.",
        emoji="🧩",
    )
