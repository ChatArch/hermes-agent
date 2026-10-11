"""Card failure boundaries retain private tracebacks, not payload text."""
import asyncio
import builtins
import json
import logging
import traceback

import pytest

from plugins.feishu_card import tools


_CANARY = "card-exception-private-canary"
_CARD = {"elements": [{"type": "markdown", "content": "visible"}]}
_REQUEST = {"action": "request_interaction", "request_id": "test-request", "card": {
    "elements": [{"type": "actions", "buttons": [{"text": "Continue", "action": "continue"}]}],
}}


def _boom(*_args, **_kwargs):
    raise RuntimeError(_CANARY)


async def _send_boom(**_kwargs):
    _boom()


def _assert_private_traceback(caplog):
    records = [r for r in caplog.records if r.name == "plugins.feishu_card.tools"]
    assert records, "card failure silently lost its traceback"
    assert any(r.exc_info and r.exc_info[2] is not None for r in records)
    for record in records:
        assert _CANARY not in record.getMessage()
        if record.exc_info:
            assert _CANARY not in "".join(traceback.format_exception(*record.exc_info))


@pytest.mark.parametrize("boundary", ["preview", "send", "prepare_request", "send_request"])
def test_card_error_boundaries_log_private_traceback_without_pending_leak(monkeypatch, caplog, boundary):
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(tools, "_interaction_requests", {})
    monkeypatch.setattr(tools, "_current_feishu_session_target", lambda: (
        "oc_test", "", "session-test", "om_test",
    ))
    if boundary in {"preview", "prepare_request"}:
        monkeypatch.setattr(tools, "_render_for_args", _boom)
    else:
        monkeypatch.setattr(tools, "_send_rendered_card", _send_boom)
    args = _REQUEST if boundary.endswith("request") else {"action": boundary, "card": _CARD}
    raw = tools.feishu_card_tool(args) if boundary == "preview" else asyncio.run(tools.feishu_card_tool_async(args))
    assert json.loads(raw)["success"] is False
    assert tools._interaction_requests == {}
    _assert_private_traceback(caplog)


def test_session_context_import_failure_logs_private_traceback_and_remains_unavailable(monkeypatch, caplog):
    original = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "gateway.session_context":
            raise ImportError(_CANARY)
        return original(name, *args, **kwargs)

    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    assert tools._current_feishu_session_target() == ("", "", "", "")
    _assert_private_traceback(caplog)


def test_header_diagnostic_error_and_warning_order_is_preserved():
    actual = tools._card_spec_diagnostics({
        "header": {"title": "x" * 81, "color": "invalid", "extra": True},
        "elements": [{"type": "markdown", "content": ""}],
    })
    assert actual == {
        "errors": ["unsupported header fields: extra", "unsupported header.color: 'invalid'"],
        "warnings": [
            "header.title is long; Feishu card headers are easier to scan under 80 chars",
            "elements[0] markdown content is empty",
        ],
    }


def test_action_diagnostic_order_and_invalid_button_continue_are_preserved():
    actual = tools._card_spec_diagnostics({"elements": [{
        "type": "actions", "layout": "column", "buttons": [None, {}],
    }]})
    assert actual == {
        "errors": [
            "unsupported actions layout at elements[0]: 'column'",
            "elements[0].buttons[0] must be an object",
            "elements[0].buttons[1].text is required",
        ],
        "warnings": ["elements[0].buttons[1] has no action; Hermes will fall back to button text as the choice"],
    }


def test_element_dispatch_preserves_all_five_element_types_and_defaults():
    from gateway.cards import Actions, Button, Card, Divider, Image, Markdown, Note
    actual = tools._card_from_spec({"elements": [
        {"type": "markdown", "content": "a\\nb"},
        {"type": "divider"},
        {"type": "image", "img_key": "img_test"},
        {"type": "note", "content": "note"},
        {"type": "actions", "buttons": [{"text": "Continue", "payload": {"count": 3}}]},
    ]})
    assert actual == Card(header=None, elements=[
        Markdown("a\nb"), Divider(), Image(image_key="img_test", alt="image"), Note("note"),
        Actions(buttons=[Button(text="Continue", style="default", action="", url=None,
                               payload={"count": "3"})], layout="row"),
    ])
