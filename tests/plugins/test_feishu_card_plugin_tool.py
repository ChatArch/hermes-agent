import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

from gateway.config import Platform
from plugins.feishu_card.tools import feishu_card_tool, feishu_card_tool_async


def _json_result(payload):
    result = feishu_card_tool(payload)
    return json.loads(result)


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
