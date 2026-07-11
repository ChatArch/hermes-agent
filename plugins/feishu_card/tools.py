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
    Image,
    Markdown,
    Note,
    RawFeishuCard,
    build_feishu_authorization_card,
)
from gateway.cards.renderers.feishu import render_feishu_card

_INTERACTION_REQUEST_TIMEOUT_SECONDS = 300

_ALLOWED_BUTTON_STYLES = ["default", "primary", "danger"]
_ALLOWED_LAYOUTS = ["row", "equal"]
_ALLOWED_HEADER_COLORS = [
    "blue",
    "green",
    "red",
    "orange",
    "purple",
    "grey",
    "turquoise",
    "violet",
    "indigo",
    "wathet",
    "yellow",
    "carmine",
]

_BUTTON_DSL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["text"],
    "properties": {
        "text": {"type": "string", "description": "Visible button label."},
        "style": {"type": "string", "enum": _ALLOWED_BUTTON_STYLES, "default": "default"},
        "action": {"type": "string", "description": "Stable machine-readable choice id."},
        "url": {"type": "string", "description": "Optional http(s) URL for navigation buttons."},
        "payload": {
            "type": "object",
            "description": "Small non-secret key/value payload merged into card callbacks.",
        },
    },
    "additionalProperties": False,
}

_CARD_DSL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Hermes high-level Feishu interactive-card DSL. It renders to official Feishu card JSON, then is sent as msg_type=interactive content string.",
    "properties": {
        "header": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "color": {"type": "string", "enum": _ALLOWED_HEADER_COLORS, "default": "blue"},
            },
            "additionalProperties": False,
        },
        "elements": {
            "type": "array",
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "required": ["type", "content"],
                        "properties": {
                            "type": {"const": "markdown"},
                            "content": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "required": ["type"],
                        "properties": {"type": {"const": "divider"}},
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "required": ["type"],
                        "anyOf": [{"required": ["image_key"]}, {"required": ["img_key"]}],
                        "properties": {
                            "type": {"const": "image"},
                            "image_key": {"type": "string", "pattern": "^img_"},
                            "img_key": {"type": "string", "pattern": "^img_"},
                            "alt": {"type": "string", "default": "image"},
                        },
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "required": ["type", "buttons"],
                        "properties": {
                            "type": {"const": "actions"},
                            "layout": {"type": "string", "enum": _ALLOWED_LAYOUTS, "default": "row"},
                            "buttons": {"type": "array", "minItems": 1, "items": _BUTTON_DSL_SCHEMA},
                        },
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "required": ["type", "content"],
                        "properties": {
                            "type": {"const": "note"},
                            "content": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                ]
            },
        },
        "raw_feishu": {
            "type": "object",
            "description": "Official Feishu card JSON escape hatch. Prefer high-level elements unless exact raw card control is required.",
        },
    },
    "additionalProperties": False,
}

_OFFICIAL_MESSAGE_CONTRACT: dict[str, Any] = {
    "create_chat_message": {
        "method": "POST",
        "path": "/open-apis/im/v1/messages",
        "query": {"receive_id_type": "chat_id|open_id|user_id"},
        "body": {"receive_id": "oc_xxx or ou_xxx", "msg_type": "interactive", "content": "<card JSON string>", "uuid": "optional idempotency key"},
        "source": "larksuite-cli shortcuts/im/im_messages_send.go and lark_oapi CreateMessageRequestBody",
    },
    "reply_thread_message": {
        "method": "POST",
        "path": "/open-apis/im/v1/messages/{om_message_id}/reply",
        "body": {"msg_type": "interactive", "content": "<card JSON string>", "reply_in_thread": True, "uuid": "optional idempotency key"},
        "source": "larksuite-cli shortcuts/im/im_messages_reply.go and lark_oapi ReplyMessageRequestBody",
    },
    "id_rule": "Reply/topic sends require an om_ message id. omt_ is a thread id and must never be used as the reply API message_id.",
}


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
                    "validate",
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
            "card": _CARD_DSL_SCHEMA,
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
        "card_schema": _CARD_DSL_SCHEMA,
        "official_message_contract": _OFFICIAL_MESSAGE_CONTRACT,
        "element_types": ["markdown", "divider", "image", "actions", "note"],
        "button_fields": ["text", "style", "action", "url", "payload"],
        "layouts": _ALLOWED_LAYOUTS,
        "button_styles": _ALLOWED_BUTTON_STYLES,
        "header": {"fields": ["title", "color"], "colors": _ALLOWED_HEADER_COLORS},
        "escape_hatches": ["raw_feishu"],
        "validation": {
            "behavior": "Use action=validate or action=preview before live sends when generating new card shapes.",
            "routing": "Feishu topic sends require an om_ reply anchor. omt_ is a thread id, not a message id.",
            "source_alignment": "Request shape mirrors larksuite-cli im +messages-send/+messages-reply and lark_oapi CreateMessageRequestBody/ReplyMessageRequestBody.",
        },
        "semantic_actions": ["request_interaction", "request_authorization"],
        "request_interaction": {
            "behavior": (
                "Hermes rewrites each terminal button to a managed card.respond callback, waits for the user click, "
                "and returns {request_id, choice, payload, message_id, thread_id} to the model."
            ),
            "button_payload_contract": (
                "Each terminal button's action becomes payload.choice. button.payload key-values are returned in payload. "
                "URL-only/non-terminal buttons should set payload.terminal=false and provide a separate completion/cancel button."
            ),
        },
    }


def _is_feishu_message_id(value: str | None) -> bool:
    return str(value or "").strip().startswith("om_")


def _is_feishu_thread_id(value: str | None) -> bool:
    return str(value or "").strip().startswith("omt_")


def _normalize_card_text(value: Any) -> str:
    """Convert common model-escaped newlines before Feishu card rendering."""

    text = str(value or "")
    return text.replace("\\r\\n", "\n").replace("\\n", "\n")


def _unknown_keys(value: dict[str, Any], allowed: set[str]) -> list[str]:
    return sorted(str(key) for key in value if str(key) not in allowed)


def _card_spec_diagnostics(spec: dict[str, Any]) -> dict[str, list[str]]:
    """Return local DSL diagnostics before Feishu sees the card."""

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(spec, dict):
        return {"errors": ["card must be an object"], "warnings": []}
    top_unknown = _unknown_keys(spec, {"header", "elements", "raw_feishu"})
    if top_unknown:
        errors.append(f"unsupported card fields: {', '.join(top_unknown)}")
    if "raw_feishu" in spec:
        if len(spec) > 1:
            errors.append("raw_feishu cannot be combined with high-level card fields")
        raw = spec.get("raw_feishu")
        if not isinstance(raw, dict):
            errors.append("raw_feishu must be an object")
        elif not isinstance(raw.get("elements"), list):
            warnings.append("raw_feishu card has no elements array; Feishu may reject an empty card")
        return {"errors": errors, "warnings": warnings}

    header = spec.get("header") or None
    if header is not None:
        if not isinstance(header, dict):
            errors.append("header must be an object")
        else:
            header_unknown = _unknown_keys(header, {"title", "color"})
            if header_unknown:
                errors.append(f"unsupported header fields: {', '.join(header_unknown)}")
            title = str(header.get("title") or "")
            color = str(header.get("color") or "blue")
            if title and len(title) > 80:
                warnings.append("header.title is long; Feishu card headers are easier to scan under 80 chars")
            if color and color not in _ALLOWED_HEADER_COLORS:
                errors.append(f"unsupported header.color: {color!r}")

    elements = spec.get("elements") or []
    if not isinstance(elements, list):
        errors.append("elements must be an array")
        return {"errors": errors, "warnings": warnings}
    if not elements:
        warnings.append("card.elements is empty; Hermes will render a blank markdown fallback")
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            errors.append(f"elements[{index}] must be an object")
            continue
        element_type = str(element.get("type") or "").strip().lower()
        if element_type not in {"markdown", "divider", "image", "actions", "note"}:
            errors.append(f"unsupported elements[{index}].type: {element_type!r}")
            continue
        allowed_element_fields = {
            "markdown": {"type", "content"},
            "divider": {"type"},
            "image": {"type", "image_key", "img_key", "alt"},
            "actions": {"type", "layout", "buttons"},
            "note": {"type", "content"},
        }[element_type]
        element_unknown = _unknown_keys(element, allowed_element_fields)
        if element_unknown:
            errors.append(f"unsupported fields on elements[{index}]: {', '.join(element_unknown)}")
        if element_type in {"markdown", "note"} and not str(element.get("content") or ""):
            warnings.append(f"elements[{index}] {element_type} content is empty")
        if element_type == "image":
            image_key = str(element.get("image_key") or element.get("img_key") or "")
            if not image_key:
                errors.append(f"elements[{index}].image_key is required for image elements")
            elif not image_key.startswith("img_"):
                errors.append(f"elements[{index}].image_key must be an uploaded Feishu image key starting with img_")
        if element_type == "actions":
            layout = str(element.get("layout") or "row")
            if layout not in _ALLOWED_LAYOUTS:
                errors.append(f"unsupported actions layout at elements[{index}]: {layout!r}")
            buttons = element.get("buttons") or []
            if not isinstance(buttons, list):
                errors.append(f"elements[{index}].buttons must be an array")
                continue
            if not buttons:
                errors.append(f"elements[{index}].buttons must contain at least one button")
            for button_index, button in enumerate(buttons):
                loc = f"elements[{index}].buttons[{button_index}]"
                if not isinstance(button, dict):
                    errors.append(f"{loc} must be an object")
                    continue
                button_unknown = _unknown_keys(button, {"text", "style", "action", "url", "payload"})
                if button_unknown:
                    errors.append(f"unsupported fields on {loc}: {', '.join(button_unknown)}")
                if not str(button.get("text") or ""):
                    errors.append(f"{loc}.text is required")
                style = str(button.get("style") or "default")
                if style not in _ALLOWED_BUTTON_STYLES:
                    errors.append(f"unsupported {loc}.style: {style!r}")
                payload = button.get("payload") if "payload" in button else {}
                if not isinstance(payload, dict):
                    errors.append(f"{loc}.payload must be an object")
                url = str(button.get("url") or "")
                if url and not url.startswith(("http://", "https://")):
                    errors.append(f"{loc}.url must start with http:// or https://")
                if url and not button.get("action"):
                    warnings.append(f"{loc} has url but no action; add action for deterministic callback payloads")
                if not url and not button.get("action"):
                    warnings.append(f"{loc} has no action; Hermes will fall back to button text as the choice")
    return {"errors": errors, "warnings": warnings}


def _validate_card_spec(spec: dict[str, Any]) -> dict[str, list[str]]:
    diagnostics = _card_spec_diagnostics(spec)
    if diagnostics["errors"]:
        raise ValueError("; ".join(diagnostics["errors"]))
    return diagnostics


def _card_from_spec(spec: dict[str, Any]) -> Card | RawFeishuCard:
    _validate_card_spec(spec)
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
            elements.append(Markdown(_normalize_card_text(raw_element.get("content"))))
        elif element_type == "divider":
            elements.append(Divider())
        elif element_type == "image":
            elements.append(
                Image(
                    image_key=str(raw_element.get("image_key") or raw_element.get("img_key") or ""),
                    alt=str(raw_element.get("alt") or "image"),
                )
            )
        elif element_type == "note":
            elements.append(Note(_normalize_card_text(raw_element.get("content"))))
        elif element_type == "actions":
            raw_buttons = raw_element.get("buttons") or []
            if not isinstance(raw_buttons, list):
                raise ValueError("actions.buttons must be an array")
            buttons = []
            for raw_button in raw_buttons:
                if not isinstance(raw_button, dict):
                    raise ValueError("each button must be an object")
                payload = raw_button.get("payload") if "payload" in raw_button else {}
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
                layout="row",
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
        if entry.chat_id and not chat_id:
            return False
        if message_id and entry.response_message_id and message_id != entry.response_message_id:
            return False
        if entry.response_message_id and not message_id:
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
    if thread_id:
        anchor = str(reply_to or reply_to_message_id or "").strip()
        if not anchor:
            raise ValueError(
                "Feishu card sends inside a topic require a triggering om_ message id; "
                "current Hermes session context has thread_id but no reply anchor. "
                "This should be supplied by the Feishu inbound message source, the same way normal Hermes replies and approval cards are routed."
            )
        if not _is_feishu_message_id(anchor):
            hint = "omt_ is a thread id, not a message id" if _is_feishu_thread_id(anchor) else "reply anchor must start with om_"
            raise ValueError(
                f"Invalid Feishu card reply anchor {anchor!r}: {hint}. "
                "Use an om_ triggering message id for threaded card sends."
            )
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
            payload = button.get("payload") if "payload" in button else {}
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
                    "layout": "row",
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
        if action == "validate":
            diagnostics = _card_spec_diagnostics(args.get("card") or {})
            return _ok(valid=not diagnostics["errors"], diagnostics=diagnostics)
        if action == "preview":
            diagnostics = _card_spec_diagnostics(args.get("card") or {})
            if diagnostics["errors"]:
                return _err("; ".join(diagnostics["errors"]))
            return _ok(rendered=_render_for_args(args), diagnostics=diagnostics)
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
