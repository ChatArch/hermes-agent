import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from gateway.cards import Markdown
from gateway.config import Platform
from plugins.feishu_card.tools import feishu_card_tool, feishu_card_tool_async


def _json_result(payload):
    result = feishu_card_tool(payload)
    return json.loads(result)


def test_feishu_card_bundled_tool_plugin_auto_loads_without_config(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_cli import plugins as pmod
    from tools.registry import registry

    registry.deregister("feishu_card")
    mgr = pmod.PluginManager()
    mgr.discover_and_load()

    loaded = mgr._plugins["feishu_card"]
    assert loaded.enabled is True
    assert loaded.manifest.source == "bundled"
    assert loaded.manifest.kind == "tool"
    tool = registry.get_entry("feishu_card")
    assert tool is not None
    assert tool.toolset == "messaging"
    assert tool.is_async is True


def test_feishu_card_plugin_registers_default_authorization_actions():
    from gateway.cards.actions import CardActionContext, get_card_action_registry
    from plugins.feishu_card import register

    class Ctx:
        def register_tool(self, **_kwargs):
            pass

    register(Ctx())
    registry = get_card_action_registry()

    cancel_response = asyncio.run(
        registry.dispatch(
            CardActionContext(
                action="auth.cancel",
                payload={"flow_id": "flow-default"},
                user_id="ou_user",
                chat_id="oc_chat",
                message_id="om_msg",
                session_key="session-default",
            )
        )
    )
    assert cancel_response.kind == "replace_card"
    assert cancel_response.card is not None
    assert isinstance(cancel_response.card.elements[0], Markdown)
    assert cancel_response.card.header.title == "授权已取消"
    assert "flow-default" in cancel_response.card.elements[0].content

    authorize_response = asyncio.run(
        registry.dispatch(
            CardActionContext(
                action="auth.authorize",
                payload={"flow_id": "flow-default"},
                user_id="ou_user",
                chat_id="oc_chat",
                message_id="om_msg",
                session_key="session-default",
            )
        )
    )
    assert authorize_response.kind == "replace_card"
    assert authorize_response.card is not None
    assert authorize_response.card.header.title == "已打开授权链接"


def test_feishu_card_tool_preview_renders_custom_card_spec():
    result = _json_result(
        {
            "action": "preview",
            "session_key": "session-tool",
            "card": {
                "header": {"title": "自定义卡片", "color": "purple"},
                "elements": [
                    {"type": "markdown", "content": "这是一张自定义卡片。"},
                    {
                        "type": "actions",
                        "layout": "equal",
                        "buttons": [
                            {"text": "打开", "style": "primary", "action": "custom.open", "url": "https://example.com"},
                            {"text": "关闭", "style": "danger", "action": "custom.close"},
                        ],
                    },
                ],
            },
        }
    )

    assert result["success"] is True
    assert result["rendered"]["header"]["template"] == "purple"
    assert result["rendered"]["elements"][1]["columns"][0]["elements"][0]["value"] == {
        "action": "custom.open",
        "session_key": "session-tool",
    }


def test_feishu_card_tool_authorization_preview_uses_generic_renderer():
    result = _json_result(
        {
            "action": "authorization_preview",
            "session_key": "session-auth",
            "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify?user_code=REDACTED",
            "flow_id": "flow-tool",
            "title": "飞书授权请求",
            "body": "请授权。",
        }
    )

    assert result["success"] is True
    rendered = result["rendered"]
    assert rendered["header"]["title"]["content"] == "飞书授权请求"
    authorize = rendered["elements"][1]["columns"][0]["elements"][0]
    cancel = rendered["elements"][1]["columns"][1]["elements"][0]
    assert authorize["url"].startswith("https://accounts.feishu.cn/oauth/v1/device/verify")
    assert authorize["value"]["action"] == "auth.authorize"
    assert authorize["value"]["flow_id"] == "flow-tool"
    assert cancel["value"]["action"] == "auth.cancel"


def test_feishu_card_tool_schema_describes_flexible_card_dsl():
    result = _json_result({"action": "schema"})

    assert result["success"] is True
    assert "markdown" in result["schema"]["element_types"]
    assert "actions" in result["schema"]["element_types"]
    assert "raw_feishu" in result["schema"]["escape_hatches"]


def test_feishu_card_tool_request_authorization_waits_for_current_session_choice(monkeypatch):
    sent = []

    class Adapter:
        async def send_card(self, chat_id, card, *, reply_to=None, metadata=None):
            sent.append({"chat_id": chat_id, "card": card, "reply_to": reply_to, "metadata": metadata})
            from plugins.feishu_card import tools

            tools.resolve_authorization_request("session-current", "flow-natural", "authorize")
            return SimpleNamespace(success=True, message_id="om_auth", thread_id="omt_current", error=None)

    fake_gateway_run = ModuleType("gateway.run")
    fake_gateway_run._gateway_runner_ref = lambda: SimpleNamespace(adapters={Platform.FEISHU: Adapter()})
    monkeypatch.setitem(sys.modules, "gateway.run", fake_gateway_run)

    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(
        platform="feishu",
        chat_id="oc_current",
        thread_id="omt_current",
        session_key="session-current",
        message_id="om_trigger",
    )
    try:
        raw = asyncio.run(
            feishu_card_tool_async(
                {
                    "action": "request_authorization",
                    "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify?user_code=REDACTED",
                    "flow_id": "flow-natural",
                    "title": "飞书授权请求",
                    "body": "请授权。",
                }
            )
        )
    finally:
        clear_session_vars(tokens)
    result = json.loads(raw)

    assert result["success"] is True
    assert result["choice"] == "authorize"
    assert result["message_id"] == "om_auth"
    assert sent[0]["chat_id"] == "oc_current"
    assert sent[0]["metadata"] == {"thread_id": "omt_current", "reply_to_message_id": "om_trigger"}
    authorize = sent[0]["card"]["elements"][1]["columns"][0]["elements"][0]
    assert authorize["value"]["action"] == "auth.authorize"
    assert authorize["value"]["session_key"] == "session-current"
    assert authorize["value"]["flow_id"] == "flow-natural"


def test_feishu_card_authorization_card_action_resolves_pending_request(monkeypatch):
    sent = []

    class Adapter:
        async def send_card(self, chat_id, card, *, reply_to=None, metadata=None):
            sent.append({"chat_id": chat_id, "card": card, "reply_to": reply_to, "metadata": metadata})
            return SimpleNamespace(success=True, message_id="om_auth", thread_id="omt_current", error=None)

    fake_gateway_run = ModuleType("gateway.run")
    fake_gateway_run._gateway_runner_ref = lambda: SimpleNamespace(adapters={Platform.FEISHU: Adapter()})
    monkeypatch.setitem(sys.modules, "gateway.run", fake_gateway_run)

    from gateway.cards.actions import CardActionContext, get_card_action_registry
    from gateway.session_context import clear_session_vars, set_session_vars
    from plugins.feishu_card import register
    from plugins.feishu_card import tools as card_tools

    monkeypatch.setattr(card_tools, "_AUTHORIZATION_REQUEST_TIMEOUT_SECONDS", 0.2)

    class Ctx:
        def register_tool(self, **_kwargs):
            pass

    register(Ctx())
    tokens = set_session_vars(
        platform="feishu",
        chat_id="oc_current",
        thread_id="omt_current",
        session_key="session-current",
        message_id="om_trigger",
    )

    async def scenario():
        task = asyncio.create_task(
            feishu_card_tool_async(
                {
                    "action": "request_authorization",
                    "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify?user_code=REDACTED",
                    "flow_id": "flow-cancel",
                }
            )
        )
        await asyncio.sleep(0)
        response = await get_card_action_registry().dispatch(
            CardActionContext(
                action="auth.cancel",
                payload={"flow_id": "flow-cancel"},
                user_id="ou_user",
                chat_id="oc_current",
                message_id="om_auth",
                session_key="session-current",
            )
        )
        raw = await task
        return response, json.loads(raw)

    try:
        response, result = asyncio.run(scenario())
    finally:
        clear_session_vars(tokens)

    assert response.kind == "replace_card"
    assert response.card is not None
    assert response.card.header.title == "授权已取消"
    assert result["success"] is True
    assert result["choice"] == "cancel"
    assert result["flow_id"] == "flow-cancel"


def test_feishu_card_tool_send_defaults_to_current_feishu_session(monkeypatch):
    sent = []

    class Adapter:
        async def send_card(self, chat_id, card, *, reply_to=None, metadata=None):
            sent.append({"chat_id": chat_id, "card": card, "reply_to": reply_to, "metadata": metadata})
            return SimpleNamespace(success=True, message_id="om_sent", thread_id="omt_sent", error=None)

    fake_gateway_run = ModuleType("gateway.run")
    fake_gateway_run._gateway_runner_ref = lambda: SimpleNamespace(adapters={Platform.FEISHU: Adapter()})
    monkeypatch.setitem(sys.modules, "gateway.run", fake_gateway_run)

    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(
        platform="feishu",
        chat_id="oc_current",
        thread_id="omt_current",
        session_key="session-current",
        message_id="om_current",
    )
    try:
        raw = asyncio.run(
            feishu_card_tool_async(
                {
                    "action": "send",
                    "card": {
                        "header": {"title": "当前会话卡片", "color": "green"},
                        "elements": [
                            {
                                "type": "actions",
                                "layout": "equal",
                                "buttons": [{"text": "确认", "style": "primary", "action": "auth.authorize"}],
                            }
                        ],
                    },
                },
            )
        )
    finally:
        clear_session_vars(tokens)
    result = json.loads(raw)

    assert result == {"success": True, "message_id": "om_sent", "thread_id": "omt_sent"}
    assert sent[0]["chat_id"] == "oc_current"
    assert sent[0]["metadata"] == {"thread_id": "omt_current", "reply_to_message_id": "om_current"}
    assert sent[0]["reply_to"] is None
    button_value = sent[0]["card"]["elements"][0]["columns"][0]["elements"][0]["value"]
    assert button_value["session_key"] == "session-current"


def test_feishu_card_tool_send_uses_live_feishu_adapter(monkeypatch):
    sent = []

    class Adapter:
        async def send_card(self, chat_id, card, *, reply_to=None, metadata=None):
            sent.append({"chat_id": chat_id, "card": card, "reply_to": reply_to, "metadata": metadata})
            return SimpleNamespace(success=True, message_id="om_sent", thread_id="omt_sent", error=None)

    fake_gateway_run = ModuleType("gateway.run")
    fake_gateway_run._gateway_runner_ref = lambda: SimpleNamespace(adapters={Platform.FEISHU: Adapter()})
    monkeypatch.setitem(sys.modules, "gateway.run", fake_gateway_run)

    raw = asyncio.run(
        feishu_card_tool_async(
            {
                "action": "send",
                "chat_id": "oc_chat",
                "thread_id": "omt_root",
                "reply_to": "om_root",
                "session_key": "session-send",
                "card": {
                    "header": {"title": "发送卡片", "color": "green"},
                    "elements": [{"type": "markdown", "content": "live send"}],
                },
            },
        )
    )
    result = json.loads(raw)

    assert result == {"success": True, "message_id": "om_sent", "thread_id": "omt_sent"}
    assert sent[0]["chat_id"] == "oc_chat"
    assert sent[0]["metadata"] == {"thread_id": "omt_root"}
    assert sent[0]["reply_to"] == "om_root"
    assert sent[0]["card"]["header"]["title"]["content"] == "发送卡片"
