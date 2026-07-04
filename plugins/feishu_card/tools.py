"""Tool handler for flexible Feishu/Lark card DSL operations."""

from __future__ import annotations

import json
from typing import Any

from gateway.cards import (
    Actions,
    Button,
    Card,
    CardHeader,
    Divider,
    Markdown,
    Note,
    RawFeishuCard,
    build_feishu_authorization_card,
)
from gateway.cards.renderers.feishu import render_feishu_card


FEISHU_CARD_SCHEMA = {
    "name": "feishu_card",
    "description": (
        "Build and preview flexible Feishu/Lark interactive cards using a JSON DSL. "
        "Use this instead of hardcoding one template per card type."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["schema", "preview", "authorization_preview", "send"],
                "description": "Operation to perform. send uses the live Feishu gateway adapter when available.",
            },
            "card": {"type": "object", "description": "Flexible card DSL for action=preview."},
            "session_key": {"type": "string", "description": "Optional session key to embed in button values."},
            "verification_url": {"type": "string", "description": "Authorization URL for authorization_preview."},
            "flow_id": {"type": "string", "description": "Opaque authorization flow id for authorization_preview."},
            "chat_id": {"type": "string", "description": "Feishu chat id for action=send."},
            "thread_id": {"type": "string", "description": "Optional Feishu thread/topic id for action=send."},
            "reply_to": {"type": "string", "description": "Optional Feishu message id to reply to."},
            "title": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["action"],
    },
}


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _err(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def _dsl_schema() -> dict[str, Any]:
    return {
        "element_types": ["markdown", "divider", "actions", "note"],
        "button_fields": ["text", "style", "action", "url", "payload"],
        "layouts": ["row", "equal"],
        "header": {"fields": ["title", "color"]},
        "escape_hatches": ["raw_feishu"],
    }


def _card_from_spec(spec: dict[str, Any]) -> Card | RawFeishuCard:
    if not isinstance(spec, dict):
        raise ValueError("card must be an object")
    if "raw_feishu" in spec:
        raw = spec["raw_feishu"]
        if not isinstance(raw, dict):
            raise ValueError("raw_feishu must be an object")
        return RawFeishuCard(raw)

    raw_header = spec.get("header") or None
    header = None
    if raw_header is not None:
        if not isinstance(raw_header, dict):
            raise ValueError("header must be an object")
        title = str(raw_header.get("title") or "")
        color = str(raw_header.get("color") or "blue")
        if title:
            header = CardHeader(title=title, color=color)

    raw_elements = spec.get("elements") or []
    if not isinstance(raw_elements, list):
        raise ValueError("elements must be an array")

    elements = []
    for raw_element in raw_elements:
        if not isinstance(raw_element, dict):
            raise ValueError("each element must be an object")
        element_type = str(raw_element.get("type") or "").strip().lower()
        if element_type == "markdown":
            elements.append(Markdown(str(raw_element.get("content") or "")))
        elif element_type == "divider":
            elements.append(Divider())
        elif element_type == "note":
            elements.append(Note(str(raw_element.get("content") or "")))
        elif element_type == "actions":
            raw_buttons = raw_element.get("buttons") or []
            if not isinstance(raw_buttons, list):
                raise ValueError("actions.buttons must be an array")
            buttons = []
            for raw_button in raw_buttons:
                if not isinstance(raw_button, dict):
                    raise ValueError("each button must be an object")
                payload = raw_button.get("payload") or {}
                if not isinstance(payload, dict):
                    raise ValueError("button.payload must be an object")
                buttons.append(
                    Button(
                        text=str(raw_button.get("text") or ""),
                        style=str(raw_button.get("style") or "default"),
                        action=str(raw_button.get("action") or ""),
                        url=(str(raw_button["url"]) if raw_button.get("url") else None),
                        payload={str(k): str(v) for k, v in payload.items()},
                    )
                )
            elements.append(Actions(buttons=buttons, layout=str(raw_element.get("layout") or "row")))
        else:
            raise ValueError(f"unsupported card element type: {element_type!r}")

    return Card(header=header, elements=elements)


def _render_for_args(args: dict[str, Any]) -> dict[str, Any]:
    session_key = str(args.get("session_key") or "") or None
    card = _card_from_spec(args.get("card") or {})
    return render_feishu_card(card, session_key=session_key)


def feishu_card_tool(args: dict[str, Any]) -> str:
    action = str(args.get("action") or "").strip()
    session_key = str(args.get("session_key") or "") or None

    try:
        if action == "schema":
            return _ok(schema=_dsl_schema())
        if action == "preview":
            return _ok(rendered=_render_for_args(args))
        if action == "authorization_preview":
            verification_url = str(args.get("verification_url") or "")
            flow_id = str(args.get("flow_id") or "")
            if not verification_url:
                return _err("verification_url is required")
            if not flow_id:
                return _err("flow_id is required")
            card = build_feishu_authorization_card(
                verification_url=verification_url,
                flow_id=flow_id,
                title=str(args.get("title") or "飞书授权请求"),
                body=str(args.get("body") or "需要你完成飞书授权后，我才能继续。"),
            )
            return _ok(rendered=render_feishu_card(card, session_key=session_key))
        return _err(f"unsupported action: {action}")
    except Exception as exc:
        return _err(str(exc))


async def feishu_card_tool_async(args: dict[str, Any]) -> str:
    action = str(args.get("action") or "").strip()
    if action != "send":
        return feishu_card_tool(args)

    chat_id = str(args.get("chat_id") or "").strip()
    if not chat_id:
        return _err("chat_id is required for send")

    try:
        rendered = _render_for_args(args)
        from gateway.config import Platform
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
        adapter = None
        if runner is not None:
            adapter = getattr(runner, "adapters", {}).get(Platform.FEISHU)
        if adapter is None:
            return _err("No live Feishu adapter is available")
        send_card = getattr(adapter, "send_card", None)
        if send_card is None:
            return _err("Live Feishu adapter does not support send_card")

        thread_id = str(args.get("thread_id") or "").strip()
        reply_to = str(args.get("reply_to") or "").strip() or None
        metadata = {"thread_id": thread_id} if thread_id else None
        result = await send_card(chat_id, rendered, reply_to=reply_to, metadata=metadata)
        if getattr(result, "success", False):
            payload: dict[str, Any] = {"message_id": getattr(result, "message_id", None)}
            thread_result = getattr(result, "thread_id", None)
            if thread_result:
                payload["thread_id"] = thread_result
            return _ok(**payload)
        return _err(getattr(result, "error", "Feishu card send failed") or "Feishu card send failed")
    except Exception as exc:
        return _err(str(exc))
