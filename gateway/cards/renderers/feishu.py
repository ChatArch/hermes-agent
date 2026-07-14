"""Feishu/Lark renderer for Hermes gateway cards."""

from __future__ import annotations

import json
from typing import Any

from gateway.cards.model import (
    Actions,
    Button,
    Card,
    Divider,
    Image,
    ListItem,
    Markdown,
    MultiSelect,
    Note,
    RawFeishuCard,
    Select,
    SelectOption,
)


def _plain_text(content: str) -> dict[str, str]:
    return {"tag": "plain_text", "content": content}


def _button_value(button: Button, *, session_key: str | None) -> dict[str, str]:
    value = {"action": button.action}
    if session_key:
        value["session_key"] = session_key
    value.update(button.payload)
    return value


def _select_option_value(option: SelectOption, *, session_key: str | None) -> str:
    value = option.value.strip()
    if option.action:
        payload = {"action": option.action}
        if session_key:
            payload["session_key"] = session_key
        payload.update(option.payload)
        if value:
            payload.setdefault("value", value)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return value or option.text


def _render_button(button: Button, *, session_key: str | None) -> dict[str, Any]:
    rendered: dict[str, Any] = {
        "tag": "button",
        "text": _plain_text(button.text),
        "type": button.style or "default",
        "value": _button_value(button, session_key=session_key),
    }
    if button.url:
        rendered["url"] = button.url
    return rendered


def _render_actions(element: Actions, *, session_key: str | None) -> dict[str, Any] | None:
    buttons = [_render_button(button, session_key=session_key) for button in element.buttons]
    if not buttons:
        return None

    if element.layout == "equal":
        columns = [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "horizontal_align": "center",
                "elements": [button],
            }
            for button in buttons
        ]
        column_set: dict[str, Any] = {"tag": "column_set", "columns": columns}
        if len(buttons) == 2:
            column_set["flex_mode"] = "bisect"
        return column_set

    return {"tag": "action", "actions": buttons}


def _render_list_item(element: ListItem, *, session_key: str | None) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {"tag": "lark_md", "content": element.text},
        "extra": _render_button(element.button, session_key=session_key),
    }


def _action_wrapped(control: dict[str, Any]) -> dict[str, Any]:
    # Feishu renders interactive controls from an action block. A bare
    # select_static inside elements[] can be accepted by the API but appear as
    # non-interactive/empty in clients, which caused a false-positive live smoke.
    return {"tag": "action", "actions": [control]}


def _render_select_control(element: Select, *, session_key: str | None) -> dict[str, Any] | None:
    options = []
    rendered_initial = ""
    for option in element.options:
        rendered_value = _select_option_value(option, session_key=session_key)
        if not rendered_value:
            continue
        options.append({"text": _plain_text(option.text), "value": rendered_value})
        if element.initial_value and element.initial_value in {option.value, rendered_value, option.text}:
            rendered_initial = rendered_value
    if not options:
        return None
    rendered: dict[str, Any] = {
        "tag": "select_static",
        "placeholder": _plain_text(element.placeholder),
        "options": options,
    }
    if rendered_initial:
        rendered["initial_option"] = rendered_initial
    return rendered


def _render_select(element: Select, *, session_key: str | None) -> dict[str, Any] | None:
    control = _render_select_control(element, session_key=session_key)
    return _action_wrapped(control) if control else None


def _render_multi_select(element: MultiSelect, *, session_key: str | None) -> dict[str, Any] | None:
    # Feishu's current card API in this deployment rejects multi_select_static
    # (ErrCode 11310). Render as a compact one-at-a-time selector instead.
    options = []
    rendered_initial = ""
    initial_values = set(element.initial_values or [])
    for option in element.options:
        rendered_value = _select_option_value(option, session_key=session_key)
        if not rendered_value:
            continue
        options.append({"text": _plain_text(option.text), "value": rendered_value})
        if not rendered_initial and (
            option.value in initial_values or option.text in initial_values or rendered_value in initial_values
        ):
            rendered_initial = rendered_value
    if not options:
        return None
    control: dict[str, Any] = {
        "tag": "select_static",
        "placeholder": _plain_text(element.placeholder),
        "options": options,
    }
    if rendered_initial:
        control["initial_option"] = rendered_initial
    return _action_wrapped(control)


def render_feishu_card(card: Card | RawFeishuCard, *, session_key: str | None = None) -> dict[str, Any]:
    """Render a generic Hermes card into Feishu interactive-card JSON data."""

    if isinstance(card, RawFeishuCard):
        return card.data

    rendered: dict[str, Any] = {"config": {"wide_screen_mode": True}}
    if card.header and card.header.title:
        rendered["header"] = {
            "title": _plain_text(card.header.title),
            "template": card.header.color or "blue",
        }

    elements: list[dict[str, Any]] = []
    for element in card.elements:
        if isinstance(element, Markdown):
            elements.append({"tag": "markdown", "content": element.content})
        elif isinstance(element, Divider):
            elements.append({"tag": "hr"})
        elif isinstance(element, Image):
            elements.append(
                {
                    "tag": "img",
                    "img_key": element.image_key,
                    "alt": _plain_text(element.alt or "image"),
                }
            )
        elif isinstance(element, Actions):
            rendered_actions = _render_actions(element, session_key=session_key)
            if rendered_actions:
                elements.append(rendered_actions)
        elif isinstance(element, ListItem):
            elements.append(_render_list_item(element, session_key=session_key))
        elif isinstance(element, Select):
            rendered_select = _render_select(element, session_key=session_key)
            if rendered_select:
                elements.append(rendered_select)
        elif isinstance(element, MultiSelect):
            rendered_multi_select = _render_multi_select(element, session_key=session_key)
            if rendered_multi_select:
                elements.append(rendered_multi_select)
        elif isinstance(element, Note):
            elements.append({"tag": "note", "elements": [_plain_text(element.content)]})

    if not elements:
        elements.append({"tag": "markdown", "content": " "})
    rendered["elements"] = elements
    return rendered
