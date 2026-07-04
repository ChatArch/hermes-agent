"""Tool handler for flexible Feishu/Lark card DSL operations."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from copy import deepcopy
from dataclasses import dataclass
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

_INTERACTION_REQUEST_TIMEOUT_SECONDS = 300


@dataclass(slots=True)
class _InteractionRequestEntry:
    event: threading.Event
    chat_id: str
    trigger_message_id: str
    response_message_id: str | None = None
    payload: dict[str, str] | None = None


_interaction_lock = threading.RLock()
_interaction_requests: dict[tuple[str, str], _InteractionRequestEntry] = {}


FEISHU_CARD_SCHEMA = {
    "name": "feishu_card",
    "description": (
        "Build, preview, send, or request feedback with flexible Feishu/Lark interactive cards. "
        "Use request_interaction when the model needs to ask the user for structured feedback and then continue."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "schema",
                    "preview",
                    "authorization_preview",
                    "send",
                    "request_interaction",
                    "request_authorization",
                ],
                "description": (
                    "Operation to perform. request_interaction sends a model-designed card to the current "
                    "Feishu conversation, waits for a button response, and returns the user's structured payload. "
                    "request_authorization is a convenience specialization for authorization-link cards."
                ),
            },
            "card": {"type": "object", "description": "Flexible card DSL for preview/send/request_interaction."},
            "request_id": {"type": "string", "description": "Optional stable id for request_interaction feedback."},
            "session_key": {"type": "string", "description": "Optional session key to embed in button values."},
            "verification_url": {"type": "string", "description": "Authorization URL for authorization_preview/request_authorization."},
            "flow_id": {"type": "string", "description": "Opaque authorization flow id for authorization card actions."},
            "chat_id": {
                "type": "string",
                "description": "Feishu chat id for action=send. Omit inside a Feishu gateway session to send to the current conversation.",
            },
            "thread_id": {
                "type": "string",
                "description": "Optional Feishu thread/topic id for action=send. Omit inside a Feishu gateway session to keep the card in the current thread.",
            },
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
        "semantic_actions": ["request_interaction", "request_authorization"],
        "request_interaction": {
            "behavior": (
                "Hermes rewrites each button to a managed card.respond callback, waits for the user click, "
                "and returns {request_id, choice, payload, message_id, thread_id} to the model."
            ),
            "button_payload_contract": (
                "Each button's action becomes payload.choice. button.payload key-values are returned in payload."
            ),
        },
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


def _build_authorization_request_card(
    *,
    verification_url: str,
    flow_id: str,
    title: str,
    body: str,
) -> Card:
    """Build an authorization card as one instance of the generic card feedback pattern."""
    return Card(
        header=CardHeader(title=title, color="blue"),
        elements=[
            Markdown(body),
            Actions(
                layout="equal",
                buttons=[
                    Button(
                        text="打开授权链接",
                        style="primary",
                        action="auth.open_link",
                        url=verification_url,
                        payload={"flow_id": flow_id},
                    ),
                    Button(
                        text="我已完成授权",
                        style="primary",
                        action="auth.authorize",
                        payload={"flow_id": flow_id},
                    ),
                    Button(
                        text="取消",
                        style="danger",
                        action="auth.cancel",
                        payload={"flow_id": flow_id},
                    ),
                ],
            ),
            Note("打开授权链接后，请回到这里点击“我已完成授权”，Hermes 才会继续任务。"),
        ],
    )


def resolve_interaction_request(
    session_key: str | None,
    request_id: str,
    payload: dict[str, Any],
    *,
    chat_id: str = "",
    message_id: str = "",
) -> bool:
    """Resolve a pending generic card interaction request."""
    clean_session_key = str(session_key or "").strip()
    clean_request_id = str(request_id or "").strip()
    if not clean_session_key or not clean_request_id:
        return False
    clean_payload = {str(k): str(v) for k, v in (payload or {}).items()}
    with _interaction_lock:
        request_key = (clean_session_key, clean_request_id)
        entry = _interaction_requests.get(request_key)
        if entry is None:
            return False
        if chat_id and entry.chat_id and chat_id != entry.chat_id:
            return False
        if message_id and entry.response_message_id and message_id != entry.response_message_id:
            return False
        _interaction_requests.pop(request_key, None)
    entry.payload = clean_payload
    entry.event.set()
    return True


def _current_feishu_session_target() -> tuple[str, str, str, str]:
    """Return the current Feishu gateway target from session ContextVars."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return "", "", "", ""

    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    if platform != "feishu":
        return "", "", "", ""
    return (
        get_session_env("HERMES_SESSION_CHAT_ID", "").strip(),
        get_session_env("HERMES_SESSION_THREAD_ID", "").strip(),
        get_session_env("HERMES_SESSION_KEY", "").strip(),
        get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip(),
    )


def _metadata_for_current_session(thread_id: str, message_id: str = "") -> dict[str, str] | None:
    metadata: dict[str, str] = {}
    if thread_id:
        metadata["thread_id"] = thread_id
    if message_id:
        metadata["reply_to_message_id"] = message_id
    return metadata or None


async def _send_rendered_card(
    *,
    chat_id: str,
    rendered: dict[str, Any],
    thread_id: str = "",
    reply_to: str | None = None,
    reply_to_message_id: str = "",
) -> Any:
    from gateway.config import Platform
    from gateway.run import _gateway_runner_ref

    runner = _gateway_runner_ref()
    adapter = None
    if runner is not None:
        adapter = getattr(runner, "adapters", {}).get(Platform.FEISHU)
    if adapter is None:
        raise RuntimeError("No live Feishu adapter is available")
    send_card = getattr(adapter, "send_card", None)
    if send_card is None:
        raise RuntimeError("Live Feishu adapter does not support send_card")

    metadata = _metadata_for_current_session(thread_id, reply_to_message_id)
    return await send_card(chat_id, rendered, reply_to=reply_to, metadata=metadata)


def _prepare_interaction_card_args(args: dict[str, Any], *, request_id: str) -> dict[str, Any]:
    effective_args = dict(args)
    card_spec = deepcopy(args.get("card") or {})
    if not isinstance(card_spec, dict):
        raise ValueError("card must be an object for request_interaction")
    elements = card_spec.get("elements") or []
    if not isinstance(elements, list):
        raise ValueError("card.elements must be an array for request_interaction")
    button_count = 0
    for element in elements:
        if not isinstance(element, dict) or str(element.get("type") or "").strip().lower() != "actions":
            continue
        buttons = element.get("buttons") or []
        if not isinstance(buttons, list):
            raise ValueError("actions.buttons must be an array for request_interaction")
        for button in buttons:
            if not isinstance(button, dict):
                raise ValueError("each button must be an object for request_interaction")
            original_action = str(button.get("action") or button.get("text") or "").strip()
            if not original_action:
                raise ValueError("request_interaction buttons require action or text")
            payload = button.get("payload") or {}
            if not isinstance(payload, dict):
                raise ValueError("button.payload must be an object for request_interaction")
            if button.get("url") and str(payload.get("terminal") or "").strip().lower() in {"false", "0", "no"}:
                button_count += 1
                continue
            prepared_payload = {str(k): str(v) for k, v in payload.items()}
            prepared_payload.setdefault("choice", original_action)
            prepared_payload.setdefault("request_id", request_id)
            prepared_payload.setdefault("button_text", str(button.get("text") or original_action))
            button["action"] = "card.respond"
            button["payload"] = prepared_payload
            button_count += 1
    if button_count == 0:
        raise ValueError("request_interaction requires at least one action button")
    effective_args["card"] = card_spec
    return effective_args


async def _request_interaction(args: dict[str, Any]) -> str:
    chat_id, thread_id, session_key, message_id = _current_feishu_session_target()
    if not chat_id or not session_key:
        return _err("request_interaction requires a live Feishu gateway session")
    request_id = str(args.get("request_id") or "").strip() or f"card-{uuid.uuid4().hex}"

    try:
        effective_args = _prepare_interaction_card_args(args, request_id=request_id)
        effective_args["session_key"] = session_key
        rendered = _render_for_args(effective_args)
    except Exception as exc:
        return _err(str(exc))

    request_key = (session_key, request_id)
    entry = _InteractionRequestEntry(event=threading.Event(), chat_id=chat_id, trigger_message_id=message_id)
    with _interaction_lock:
        if request_key in _interaction_requests:
            return _err(f"request_interaction request_id already pending: {request_id}")
        _interaction_requests[request_key] = entry

    try:
        result = await _send_rendered_card(
            chat_id=chat_id,
            rendered=rendered,
            thread_id=thread_id,
            reply_to_message_id=message_id,
        )
        if not getattr(result, "success", False):
            with _interaction_lock:
                _interaction_requests.pop(request_key, None)
            return _err(getattr(result, "error", "Feishu interaction card send failed") or "Feishu interaction card send failed")
        with _interaction_lock:
            current = _interaction_requests.get(request_key)
            if current is not None and current is entry:
                current.response_message_id = str(getattr(result, "message_id", "") or "") or None

        resolved = await asyncio.to_thread(entry.event.wait, _INTERACTION_REQUEST_TIMEOUT_SECONDS)
        if not resolved:
            with _interaction_lock:
                _interaction_requests.pop(request_key, None)
            return _err("Timed out waiting for Feishu card interaction response")
        payload = entry.payload or {}
        return _ok(
            request_id=request_id,
            choice=payload.get("choice"),
            payload=payload,
            message_id=getattr(result, "message_id", None),
            thread_id=getattr(result, "thread_id", None),
        )
    except Exception as exc:
        with _interaction_lock:
            _interaction_requests.pop(request_key, None)
        return _err(str(exc))


async def _request_authorization(args: dict[str, Any]) -> str:
    verification_url = str(args.get("verification_url") or "").strip()
    flow_id = str(args.get("flow_id") or "").strip()
    if not verification_url:
        return _err("verification_url is required")
    if not flow_id:
        return _err("flow_id is required")

    title = str(args.get("title") or "飞书授权请求")
    body = str(args.get("body") or "需要你完成飞书授权后，我才能继续。")
    interaction_args = {
        "action": "request_interaction",
        "request_id": flow_id,
        "card": {
            "header": {"title": title, "color": "blue"},
            "elements": [
                {"type": "markdown", "content": body},
                {
                    "type": "actions",
                    "layout": "equal",
                    "buttons": [
                        {
                            "text": "打开授权链接",
                            "style": "primary",
                            "action": "open_link",
                            "url": verification_url,
                            "payload": {"flow_id": flow_id, "kind": "open_link", "terminal": "false"},
                        },
                        {
                            "text": "我已完成授权",
                            "style": "primary",
                            "action": "authorize",
                            "payload": {"flow_id": flow_id, "kind": "authorize"},
                        },
                        {
                            "text": "取消",
                            "style": "danger",
                            "action": "cancel",
                            "payload": {"flow_id": flow_id, "kind": "cancel"},
                        },
                    ],
                },
                {"type": "note", "content": "打开授权链接后，请回到这里点击“我已完成授权”，Hermes 才会继续任务。"},
            ],
        },
    }
    raw = await _request_interaction(interaction_args)
    result = json.loads(raw)
    if not result.get("success"):
        return raw
    choice = str(result.get("choice") or "")
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    kind = str(payload.get("kind") or choice)
    if kind == "open_link":
        # Opening the URL is not terminal feedback; the user still needs to
        # return and click completed/cancel.  If a platform sends a URL-button
        # callback, surface it without pretending authorization completed.
        return _ok(
            choice="open_link",
            flow_id=flow_id,
            payload=payload,
            message_id=result.get("message_id"),
            thread_id=result.get("thread_id"),
        )
    return _ok(
        choice="authorize" if kind == "authorize" else "cancel",
        flow_id=flow_id,
        payload=payload,
        message_id=result.get("message_id"),
        thread_id=result.get("thread_id"),
    )


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
    if action == "request_interaction":
        return await _request_interaction(args)
    if action == "request_authorization":
        return await _request_authorization(args)
    if action != "send":
        return feishu_card_tool(args)

    session_chat_id, session_thread_id, session_key, session_message_id = _current_feishu_session_target()
    chat_id = str(args.get("chat_id") or "").strip()
    target_from_session = False
    if not chat_id:
        chat_id = session_chat_id
        target_from_session = bool(chat_id)
    if not chat_id:
        return _err("chat_id is required for send outside a Feishu gateway session")

    try:
        effective_args = dict(args)
        if not effective_args.get("session_key") and session_key:
            effective_args["session_key"] = session_key
        rendered = _render_for_args(effective_args)
        thread_id = str(args.get("thread_id") or "").strip()
        if not thread_id and target_from_session:
            thread_id = session_thread_id
        reply_to = str(args.get("reply_to") or "").strip() or None
        result = await _send_rendered_card(
            chat_id=chat_id,
            rendered=rendered,
            thread_id=thread_id,
            reply_to=reply_to,
            reply_to_message_id=session_message_id if target_from_session else "",
        )
        if getattr(result, "success", False):
            payload: dict[str, Any] = {"message_id": getattr(result, "message_id", None)}
            thread_result = getattr(result, "thread_id", None)
            if thread_result:
                payload["thread_id"] = thread_result
            return _ok(**payload)
        return _err(getattr(result, "error", "Feishu card send failed") or "Feishu card send failed")
    except Exception as exc:
        return _err(str(exc))
