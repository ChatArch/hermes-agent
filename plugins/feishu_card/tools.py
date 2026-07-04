"""Tool handler for flexible Feishu/Lark card DSL operations."""

from __future__ import annotations

import asyncio
import json
import threading
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

_AUTHORIZATION_REQUEST_TIMEOUT_SECONDS = 300


@dataclass(slots=True)
class _AuthorizationRequestEntry:
    event: threading.Event
    choice: str | None = None


_authorization_lock = threading.RLock()
_authorization_requests: dict[tuple[str, str], _AuthorizationRequestEntry] = {}


FEISHU_CARD_SCHEMA = {
    "name": "feishu_card",
    "description": (
        "Build, request, preview, and send flexible Feishu/Lark interactive cards using a JSON DSL. "
        "Use request_authorization for natural current-session authorization flows."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["schema", "preview", "authorization_preview", "send", "request_authorization"],
                "description": (
                    "Operation to perform. request_authorization sends an authorization card to the current "
                    "Feishu gateway conversation and waits for the user's card choice. send uses the live "
                    "Feishu gateway adapter when available."
                ),
            },
            "card": {"type": "object", "description": "Flexible card DSL for action=preview."},
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
        "semantic_actions": ["request_authorization"],
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


def resolve_authorization_request(session_key: str | None, flow_id: str, choice: str) -> bool:
    """Resolve a pending semantic Feishu authorization request.

    Called by the default authorization card-action handlers.  This mirrors the
    SSH Mode grant resolver: a card callback turns a user choice into a pending
    model-tool result.
    """
    clean_session_key = str(session_key or "").strip()
    clean_flow_id = str(flow_id or "").strip()
    if not clean_session_key or not clean_flow_id:
        return False
    clean_choice = str(choice or "").strip().lower()
    if clean_choice not in {"authorize", "cancel"}:
        clean_choice = "cancel"
    with _authorization_lock:
        entry = _authorization_requests.pop((clean_session_key, clean_flow_id), None)
    if entry is None:
        return False
    entry.choice = clean_choice
    entry.event.set()
    return True


def _current_feishu_session_target() -> tuple[str, str, str, str]:
    """Return the current Feishu gateway target from session ContextVars.

    This mirrors how built-in approval cards (tool approval, SSH Mode, update
    prompts) naturally route back to the active conversation: the gateway seeds
    ``HERMES_SESSION_CHAT_ID`` and ``HERMES_SESSION_THREAD_ID`` for the current
    task, then platform send methods receive the thread via ``metadata``.
    """
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


async def _request_authorization(args: dict[str, Any]) -> str:
    verification_url = str(args.get("verification_url") or "").strip()
    flow_id = str(args.get("flow_id") or "").strip()
    if not verification_url:
        return _err("verification_url is required")
    if not flow_id:
        return _err("flow_id is required")

    chat_id, thread_id, session_key, message_id = _current_feishu_session_target()
    if not chat_id or not session_key:
        return _err("request_authorization requires a live Feishu gateway session")

    card = build_feishu_authorization_card(
        verification_url=verification_url,
        flow_id=flow_id,
        title=str(args.get("title") or "飞书授权请求"),
        body=str(args.get("body") or "需要你完成飞书授权后，我才能继续。"),
    )
    rendered = render_feishu_card(card, session_key=session_key)
    request_key = (session_key, flow_id)
    entry = _AuthorizationRequestEntry(event=threading.Event())
    with _authorization_lock:
        _authorization_requests[request_key] = entry

    try:
        result = await _send_rendered_card(
            chat_id=chat_id,
            rendered=rendered,
            thread_id=thread_id,
            reply_to_message_id=message_id,
        )
        if not getattr(result, "success", False):
            with _authorization_lock:
                _authorization_requests.pop(request_key, None)
            return _err(
                getattr(result, "error", "Feishu authorization card send failed")
                or "Feishu authorization card send failed"
            )

        resolved = await asyncio.to_thread(entry.event.wait, _AUTHORIZATION_REQUEST_TIMEOUT_SECONDS)
        if not resolved:
            with _authorization_lock:
                _authorization_requests.pop(request_key, None)
            return _err("Timed out waiting for Feishu authorization response")
        return _ok(
            choice=entry.choice or "cancel",
            flow_id=flow_id,
            message_id=getattr(result, "message_id", None),
            thread_id=getattr(result, "thread_id", None),
        )
    except Exception as exc:
        with _authorization_lock:
            _authorization_requests.pop(request_key, None)
        return _err(str(exc))


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
