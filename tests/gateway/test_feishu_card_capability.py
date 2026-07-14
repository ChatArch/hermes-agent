import asyncio
import json
import threading
import sys
from types import ModuleType, SimpleNamespace

import pytest

from gateway.cards import (
    Actions,
    Button,
    COMMAND_CARD_ACTION_OPEN_GROUP,
    COMMAND_CARD_ACTION_RUN,
    COMMAND_CARD_ACTION_TEXT_HELP,
    COMMAND_CENTER_GROUPS,
    Card,
    CardHeader,
    Image,
    ListItem,
    Markdown,
    MultiSelect,
    Note,
    RawFeishuCard,
    Select,
    SelectOption,
    build_command_center_card,
    build_feishu_authorization_card,
    command_run_payload,
)
from gateway.cards.actions import (
    CardActionContext,
    CardActionResponse,
    CardActionRegistry,
    register_card_action,
)
from gateway.cards.renderers.feishu import render_feishu_card
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, CardReply, SendResult
from plugins.platforms.feishu import adapter as feishu_platform
from plugins.platforms.feishu.adapter import FeishuAdapter


def test_render_feishu_card_supports_composable_authorization_shape():
    card = Card(
        header=CardHeader(title="飞书授权请求", color="blue"),
        elements=[
            Markdown("需要你完成飞书授权后，我才能继续。"),
            Actions(
                buttons=[
                    Button(
                        text="授权",
                        style="primary",
                        action="auth.authorize",
                        url="https://accounts.feishu.cn/oauth/v1/device/verify?user_code=REDACTED",
                        payload={"flow_id": "flow-1"},
                    ),
                    Button(text="取消", style="danger", action="auth.cancel"),
                ],
                layout="equal",
            ),
            Note("Hermes 将在授权完成后继续。"),
        ],
    )

    rendered = render_feishu_card(card, session_key="session-1")

    assert rendered["config"]["wide_screen_mode"] is True
    assert rendered["header"]["template"] == "blue"
    assert rendered["header"]["title"] == {"tag": "plain_text", "content": "飞书授权请求"}
    assert rendered["elements"][0] == {"tag": "markdown", "content": "需要你完成飞书授权后，我才能继续。"}

    button_columns = rendered["elements"][1]
    assert button_columns["tag"] == "column_set"
    assert button_columns["flex_mode"] == "bisect"
    authorize_button = button_columns["columns"][0]["elements"][0]
    cancel_button = button_columns["columns"][1]["elements"][0]

    assert authorize_button["tag"] == "button"
    assert authorize_button["text"]["content"] == "授权"
    assert authorize_button["type"] == "primary"
    assert authorize_button["url"].startswith("https://accounts.feishu.cn/oauth/v1/device/verify")
    assert authorize_button["value"] == {
        "action": "auth.authorize",
        "session_key": "session-1",
        "flow_id": "flow-1",
    }
    assert cancel_button["text"]["content"] == "取消"
    assert cancel_button["value"] == {"action": "auth.cancel", "session_key": "session-1"}
    assert rendered["elements"][2] == {
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "Hermes 将在授权完成后继续。"}],
    }


def test_feishu_adapter_model_picker_drills_down_and_switches(monkeypatch):
    adapter = FeishuAdapter.__new__(FeishuAdapter)
    adapter._client = object()
    sent = []
    selected = []

    async def fake_send_card(chat_id, card, *, reply_to=None, metadata=None):
        sent.append({"chat_id": chat_id, "card": card, "reply_to": reply_to, "metadata": metadata})
        return SendResult(success=True, message_id="om_picker")

    async def on_model_selected(chat_id, model_id, provider_slug):
        selected.append((chat_id, model_id, provider_slug))
        return f"Switched to {model_id} via {provider_slug}"

    monkeypatch.setattr(adapter, "send_card", fake_send_card)
    providers = [
        {
            "name": "CRS",
            "slug": "custom:crs",
            "models": ["gpt-5.6-sol", "gpt-5.5"],
            "total_models": 2,
            "is_current": True,
        },
        {"name": "OpenRouter", "slug": "openrouter", "models": ["openai/gpt-5"], "total_models": 1},
    ]

    result = asyncio.run(
        adapter.send_model_picker(
            chat_id="oc_chat",
            providers=providers,
            current_model="gpt-5.5",
            current_provider="custom:crs",
            session_key="session-model",
            on_model_selected=on_model_selected,
            metadata={"thread_id": "omt_thread"},
        )
    )

    assert result.success is True
    assert sent[0]["chat_id"] == "oc_chat"
    assert sent[0]["metadata"] == {"thread_id": "omt_thread"}
    assert sent[0]["card"]["header"]["title"]["content"] == "选择模型渠道"
    picker_id = next(iter(adapter._model_picker_state))

    model_page = asyncio.run(
        adapter._handle_model_picker_action(
            CardActionContext(
                action="feishu.model_picker",
                payload={"picker_id": picker_id, "op": "provider", "provider": "custom:crs"},
                user_id="ou_user",
                chat_id="oc_chat",
                message_id="om_picker",
                session_key="session-model",
            )
        )
    )
    assert model_page.kind == "replace_card"
    assert model_page.card.header.title == "选择模型"

    switched = asyncio.run(
        adapter._handle_model_picker_action(
            CardActionContext(
                action="feishu.model_picker",
                payload={
                    "picker_id": picker_id,
                    "op": "model",
                    "provider": "custom:crs",
                    "model": "gpt-5.6-sol",
                },
                user_id="ou_user",
                chat_id="oc_chat",
                message_id="om_picker",
                session_key="session-model",
            )
        )
    )
    assert selected == [("oc_chat", "gpt-5.6-sol", "custom:crs")]
    assert switched.kind == "replace_card"
    assert switched.card.header.title == "Model switched"
    assert "gpt-5.6-sol" in switched.card.elements[0].content
    assert picker_id not in adapter._model_picker_state


def test_feishu_adapter_send_card_sends_interactive_payload(monkeypatch):
    adapter = FeishuAdapter.__new__(FeishuAdapter)
    adapter._client = object()
    calls = []

    async def fake_send_with_retry(**kwargs):
        calls.append(kwargs)

        class Response:
            code = 0
            msg = "ok"
            data = SimpleNamespace(message_id="om_card", thread_id="omt_card")

            def success(self):
                return True

        return Response()

    monkeypatch.setattr(adapter, "_feishu_send_with_retry", fake_send_with_retry)
    card = render_feishu_card(
        Card(header=CardHeader(title="自定义", color="purple"), elements=[Markdown("内容")])
    )

    result = asyncio.run(adapter.send_card("oc_chat", card, metadata={"thread_id": "omt_root"}))

    assert result.success is True
    assert result.message_id == "om_card"
    assert calls[0]["chat_id"] == "oc_chat"
    assert calls[0]["msg_type"] == "interactive"
    assert calls[0]["payload"].startswith('{"config"')
    assert calls[0]["metadata"] == {"thread_id": "omt_root"}


def test_feishu_adapter_raw_interactive_reply_uses_metadata_anchor(monkeypatch):
    replies = []
    creates = []

    class MessageApi:
        @staticmethod
        def reply(request):
            replies.append(request)
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_card", thread_id="omt_thread"))

        @staticmethod
        def create(request):  # pragma: no cover - reply path must be used
            creates.append(request)
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_created"))

    adapter = FeishuAdapter.__new__(FeishuAdapter)
    adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=MessageApi)))

    def reply_body(**kwargs):
        return SimpleNamespace(
            content=kwargs["content"],
            msg_type=kwargs["msg_type"],
            reply_in_thread=kwargs["reply_in_thread"],
            uuid=kwargs["uuid_value"],
        )

    monkeypatch.setattr(
        FeishuAdapter,
        "_build_reply_message_body",
        staticmethod(reply_body),
    )
    monkeypatch.setattr(
        FeishuAdapter,
        "_build_reply_message_request",
        staticmethod(lambda message_id, request_body: SimpleNamespace(message_id=message_id, request_body=request_body)),
    )

    payload = json.dumps({"elements": [{"tag": "markdown", "content": "hello"}]}, ensure_ascii=False)
    response = asyncio.run(
        adapter._send_raw_message(
            chat_id="oc_chat",
            msg_type="interactive",
            payload=payload,
            reply_to=None,
            metadata={"thread_id": "omt_current", "reply_to_message_id": "om_trigger"},
        )
    )

    assert response.success()
    assert creates == []
    assert len(replies) == 1
    request = replies[0]
    assert request.message_id == "om_trigger"
    assert request.request_body.msg_type == "interactive"
    assert request.request_body.content == payload
    assert request.request_body.reply_in_thread is True
    assert request.request_body.uuid


def test_raw_feishu_card_escape_hatch_is_returned_without_rewriting():
    raw = {"config": {"wide_screen_mode": False}, "elements": [{"tag": "hr"}]}

    assert render_feishu_card(RawFeishuCard(raw)) is raw


def test_render_feishu_card_supports_image_elements():
    rendered = render_feishu_card(Card(elements=[Image(image_key="img_v3_dummy", alt="chart")]))

    assert rendered["elements"] == [
        {
            "tag": "img",
            "img_key": "img_v3_dummy",
            "alt": {"tag": "plain_text", "content": "chart"},
        }
    ]


def test_render_feishu_card_supports_list_item_and_select_workflows():
    card = Card(
        header=CardHeader(title="操作入口", color="turquoise"),
        elements=[
            ListItem(
                text="**zhihong.oray**\n/home/zhihong/Playground",
                button=Button(
                    text="Use",
                    style="primary",
                    action="gateway.command.act",
                    payload={"view": "ssh", "op": "use", "alias": "zhihong.oray"},
                ),
            ),
            Select(
                placeholder="选择 reasoning effort",
                initial_value="xhigh",
                options=[
                    SelectOption(text="xhigh", value="xhigh", action="gateway.command.act", payload={"view": "reasoning", "effort": "xhigh"}),
                    SelectOption(text="max", value="max", action="gateway.command.act", payload={"view": "reasoning", "effort": "max"}),
                ],
            ),
            MultiSelect(
                placeholder="选择 YOLO 目标",
                initial_values=["zhihong.oray"],
                options=[
                    SelectOption(text="zhihong.oray", value="zhihong.oray", action="gateway.command.act", payload={"view": "ssh", "op": "yolo_set"}),
                    SelectOption(text="hitk", value="hitk", action="gateway.command.act", payload={"view": "ssh", "op": "yolo_set"}),
                ],
            ),
        ],
    )

    rendered = render_feishu_card(card, session_key="session-workflow")

    row = rendered["elements"][0]
    assert row["tag"] == "div"
    assert row["text"] == {"tag": "lark_md", "content": "**zhihong.oray**\n/home/zhihong/Playground"}
    assert row["extra"]["tag"] == "button"
    assert row["extra"]["value"] == {
        "action": "gateway.command.act",
        "session_key": "session-workflow",
        "view": "ssh",
        "op": "use",
        "alias": "zhihong.oray",
    }

    selector_action = rendered["elements"][1]
    assert selector_action["tag"] == "action"
    selector = selector_action["actions"][0]
    assert selector["tag"] == "select_static"
    assert selector["placeholder"] == {"tag": "plain_text", "content": "选择 reasoning effort"}
    assert selector["initial_option"] == selector["options"][0]["value"]
    selected_payload = json.loads(selector["options"][0]["value"])
    assert selected_payload == {
        "action": "gateway.command.act",
        "session_key": "session-workflow",
        "view": "reasoning",
        "effort": "xhigh",
        "value": "xhigh",
    }

    multi_selector_action = rendered["elements"][2]
    assert multi_selector_action["tag"] == "action"
    multi_selector = multi_selector_action["actions"][0]
    assert multi_selector["tag"] == "select_static"
    assert multi_selector["placeholder"] == {"tag": "plain_text", "content": "选择 YOLO 目标"}
    assert multi_selector["initial_option"] == multi_selector["options"][0]["value"]
    yolo_payload = json.loads(multi_selector["options"][0]["value"])
    assert yolo_payload == {
        "action": "gateway.command.act",
        "session_key": "session-workflow",
        "view": "ssh",
        "op": "yolo_set",
        "value": "zhihong.oray",
    }


def test_build_feishu_authorization_card_is_generic_card_composition():
    card = build_feishu_authorization_card(
        verification_url="https://accounts.feishu.cn/oauth/v1/device/verify?user_code=REDACTED",
        flow_id="flow-2",
        title="飞书授权请求",
        body="需要授权。",
    )

    assert isinstance(card, Card)
    rendered = render_feishu_card(card, session_key="session-2")
    actions = rendered["elements"][1]["actions"]
    assert actions[0]["value"]["action"] == "auth.authorize"
    assert actions[0]["value"]["flow_id"] == "flow-2"
    assert actions[1]["value"]["action"] == "auth.cancel"


def test_card_action_registry_routes_registered_handlers():
    registry = CardActionRegistry()

    async def authorize(ctx: CardActionContext) -> CardActionResponse:
        return CardActionResponse.replace_card(
            Card(header=CardHeader(title="授权已确认", color="green"), elements=[Markdown(ctx.payload["flow_id"])])
        )

    registry.register("auth.authorize", authorize)

    response = asyncio.run(
        registry.dispatch(
            CardActionContext(
                action="auth.authorize",
                payload={"flow_id": "flow-3"},
                user_id="ou_user",
                chat_id="oc_chat",
                message_id="om_msg",
                session_key="session-3",
            )
        )
    )

    assert response.kind == "replace_card"
    assert response.card.header.title == "授权已确认"
    assert response.card.elements[0].content == "flow-3"


def test_card_action_registry_rejects_unknown_action():
    registry = CardActionRegistry()

    with pytest.raises(KeyError, match="No card action handler registered"):
        asyncio.run(
            registry.dispatch(
                CardActionContext(
                    action="missing.action",
                    payload={},
                    user_id="ou_user",
                    chat_id="oc_chat",
                    message_id="om_msg",
                    session_key="session-4",
                )
            )
        )


def _start_background_loop():
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_loop():
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    ready.wait(timeout=2)
    return loop, thread


def _stop_background_loop(loop, thread):
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()


def _patch_feishu_callback_classes(monkeypatch):
    class FakeCallBackCard:
        pass

    class FakeP2CardActionTriggerResponse:
        def __init__(self):
            self.card = None

    monkeypatch.setattr(feishu_platform, "CallBackCard", FakeCallBackCard)
    monkeypatch.setattr(
        feishu_platform,
        "P2CardActionTriggerResponse",
        FakeP2CardActionTriggerResponse,
    )


def _allow_all_interactive_callbacks(adapter):
    adapter._admins = {"*"}
    adapter._allowed_group_users = set()
    adapter._chat_info_cache = {}


def test_feishu_card_action_trigger_registered_action_returns_replacement_card(monkeypatch):
    loop, thread = _start_background_loop()
    action_name = "test.auth.cancel.phase2"

    async def cancel(ctx: CardActionContext) -> CardActionResponse:
        assert ctx.action == action_name
        assert ctx.payload["flow_id"] == "flow-click"
        assert ctx.session_key == "session-click"
        assert ctx.chat_id == "oc_chat"
        assert ctx.message_id == "om_msg"
        return CardActionResponse.replace_card(
            Card(
                header=CardHeader(title="授权已取消", color="red"),
                elements=[Markdown("flow=" + ctx.payload["flow_id"])],
            )
        )

    register_card_action(action_name, cancel)
    _patch_feishu_callback_classes(monkeypatch)
    adapter = FeishuAdapter.__new__(FeishuAdapter)
    adapter._loop = loop
    _allow_all_interactive_callbacks(adapter)
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value={
                    "action": action_name,
                    "flow_id": "flow-click",
                    "session_key": "session-click",
                }
            ),
            operator=SimpleNamespace(open_id="ou_user", user_id="user_id"),
            context=SimpleNamespace(open_chat_id="oc_chat", open_message_id="om_msg"),
        )
    )

    try:
        response = adapter._on_card_action_trigger(data)
    finally:
        _stop_background_loop(loop, thread)

    assert response is not None
    assert response.card.type == "raw"
    assert response.card.data["header"]["title"] == {"tag": "plain_text", "content": "授权已取消"}
    assert response.card.data["header"]["template"] == "red"
    assert response.card.data["elements"][0] == {"tag": "markdown", "content": "flow=flow-click"}


def test_feishu_card_action_trigger_parses_select_option_json_action(monkeypatch):
    loop, thread = _start_background_loop()
    action_name = "test.select.option.phase2"

    async def handler(ctx: CardActionContext) -> CardActionResponse:
        assert ctx.action == action_name
        assert ctx.payload["effort"] == "xhigh"
        assert ctx.session_key == "session-select"
        return CardActionResponse.replace_card(
            Card(header=CardHeader(title="已选择", color="green"), elements=[Markdown(ctx.payload["effort"])])
        )

    register_card_action(action_name, handler)
    _patch_feishu_callback_classes(monkeypatch)
    adapter = FeishuAdapter.__new__(FeishuAdapter)
    adapter._loop = loop
    _allow_all_interactive_callbacks(adapter)
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value=json.dumps(
                    {"action": action_name, "effort": "xhigh", "session_key": "session-select"},
                    separators=(",", ":"),
                )
            ),
            operator=SimpleNamespace(open_id="ou_user", user_id="user_id"),
            context=SimpleNamespace(open_chat_id="oc_chat", open_message_id="om_msg"),
        )
    )

    try:
        response = adapter._on_card_action_trigger(data)
    finally:
        _stop_background_loop(loop, thread)

    assert response is not None
    assert response.card.type == "raw"
    assert response.card.data["header"]["title"] == {"tag": "plain_text", "content": "已选择"}
    assert response.card.data["elements"][0] == {"tag": "markdown", "content": "xhigh"}


def test_feishu_card_action_trigger_parses_multi_select_json_actions(monkeypatch):
    loop, thread = _start_background_loop()
    action_name = "test.multi.select.phase2"

    async def handler(ctx: CardActionContext) -> CardActionResponse:
        assert ctx.action == action_name
        assert ctx.payload["values"] == ["zhihong.oray", "hitk"]
        assert ctx.session_key == "session-multi"
        return CardActionResponse.replace_card(
            Card(header=CardHeader(title="多选已处理", color="green"), elements=[Markdown(",".join(ctx.payload["values"]))])
        )

    register_card_action(action_name, handler)
    _patch_feishu_callback_classes(monkeypatch)
    adapter = FeishuAdapter.__new__(FeishuAdapter)
    adapter._loop = loop
    _allow_all_interactive_callbacks(adapter)
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value=[
                    json.dumps({"action": action_name, "session_key": "session-multi", "op": "yolo_set", "value": "zhihong.oray"}, separators=(",", ":")),
                    json.dumps({"action": action_name, "session_key": "session-multi", "op": "yolo_set", "value": "hitk"}, separators=(",", ":")),
                ]
            ),
            operator=SimpleNamespace(open_id="ou_user", user_id="user_id"),
            context=SimpleNamespace(open_chat_id="oc_chat", open_message_id="om_msg"),
        )
    )

    try:
        response = adapter._on_card_action_trigger(data)
    finally:
        _stop_background_loop(loop, thread)

    assert response is not None
    assert response.card.type == "raw"
    assert response.card.data["header"]["title"] == {"tag": "plain_text", "content": "多选已处理"}
    assert response.card.data["elements"][0] == {"tag": "markdown", "content": "zhihong.oray,hitk"}


def test_feishu_card_action_trigger_unknown_action_falls_back(monkeypatch):
    loop, thread = _start_background_loop()
    _patch_feishu_callback_classes(monkeypatch)
    adapter = FeishuAdapter.__new__(FeishuAdapter)
    adapter._loop = loop
    _allow_all_interactive_callbacks(adapter)
    submitted = []

    async def fake_handle(data):
        return None

    def fake_submit(active_loop, coro):
        submitted.append((active_loop, coro))
        coro.close()
        return True

    monkeypatch.setattr(adapter, "_handle_card_action_event", fake_handle)
    monkeypatch.setattr(adapter, "_submit_on_loop", fake_submit)
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(value={"action": "missing.phase2.action"}),
            operator=SimpleNamespace(open_id="ou_user", user_id="user_id"),
            context=SimpleNamespace(open_chat_id="oc_chat", open_message_id="om_msg"),
        )
    )

    try:
        response = adapter._on_card_action_trigger(data)
    finally:
        _stop_background_loop(loop, thread)

    assert response is not None
    assert submitted and submitted[0][0] is loop


def test_feishu_card_action_trigger_resolves_pending_authorization_request(monkeypatch):
    loop, thread = _start_background_loop()
    _patch_feishu_callback_classes(monkeypatch)
    sent = []

    class SendAdapter:
        async def send_card(self, chat_id, card, *, reply_to=None, metadata=None):
            sent.append({"chat_id": chat_id, "card": card, "reply_to": reply_to, "metadata": metadata})
            return SimpleNamespace(success=True, message_id="om_auth", thread_id="omt_thread", error=None)

    fake_gateway_run = ModuleType("gateway.run")
    fake_gateway_run._gateway_runner_ref = lambda: SimpleNamespace(adapters={Platform.FEISHU: SendAdapter()})
    monkeypatch.setitem(sys.modules, "gateway.run", fake_gateway_run)

    from gateway.session_context import clear_session_vars, set_session_vars
    from plugins.feishu_card import register
    from plugins.feishu_card.tools import feishu_card_tool_async

    class Ctx:
        def register_tool(self, **_kwargs):
            pass

    register(Ctx())
    adapter = FeishuAdapter.__new__(FeishuAdapter)
    adapter._loop = loop
    _allow_all_interactive_callbacks(adapter)
    tokens = set_session_vars(
        platform="feishu",
        chat_id="oc_chat",
        thread_id="omt_thread",
        session_key="session-auth",
        message_id="om_trigger",
    )

    async def scenario():
        task = asyncio.create_task(
            feishu_card_tool_async(
                {
                    "action": "request_authorization",
                    "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify?user_code=REDACTED",
                    "flow_id": "flow-adapter-cancel",
                }
            )
        )
        await asyncio.sleep(0)
        data = SimpleNamespace(
            event=SimpleNamespace(
                action=SimpleNamespace(
                    value={
                        "action": "card.respond",
                        "request_id": "flow-adapter-cancel",
                        "choice": "cancel",
                        "kind": "cancel",
                        "flow_id": "flow-adapter-cancel",
                        "session_key": "session-auth",
                    }
                ),
                operator=SimpleNamespace(open_id="ou_user", user_id="user_id"),
                context=SimpleNamespace(open_chat_id="oc_chat", open_message_id="om_auth"),
            )
        )
        response = adapter._on_card_action_trigger(data)
        raw = await task
        return response, json.loads(raw)

    try:
        response, result = asyncio.run(scenario())
    finally:
        clear_session_vars(tokens)
        _stop_background_loop(loop, thread)

    assert sent[0]["chat_id"] == "oc_chat"
    assert sent[0]["metadata"] == {"thread_id": "omt_thread", "reply_to_message_id": "om_trigger"}
    assert response is not None
    assert response.card.type == "raw"
    assert response.card.data["header"]["title"] == {"tag": "plain_text", "content": "已收到反馈"}
    assert result["success"] is True
    assert result["choice"] == "cancel"
    assert result["flow_id"] == "flow-adapter-cancel"


def test_feishu_card_action_trigger_registered_action_rejects_unauthorized_user(monkeypatch):
    loop, thread = _start_background_loop()
    action_name = "test.auth.unauthorized.phase2"
    called = []

    async def handler(ctx: CardActionContext) -> CardActionResponse:
        called.append(ctx)
        return CardActionResponse.replace_card(Card(header=CardHeader(title="不应出现"), elements=[Markdown("bad")]))

    register_card_action(action_name, handler)
    _patch_feishu_callback_classes(monkeypatch)
    adapter = FeishuAdapter.__new__(FeishuAdapter)
    adapter._loop = loop
    adapter._admins = set()
    adapter._allowed_group_users = set()
    adapter._chat_info_cache = {}
    monkeypatch.setattr(adapter, "_allow_group_message", lambda *_args, **_kwargs: False)
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(value={"action": action_name, "session_key": "session-click"}),
            operator=SimpleNamespace(open_id="ou_intruder", user_id="user_id"),
            context=SimpleNamespace(open_chat_id="oc_chat", open_message_id="om_msg"),
        )
    )

    try:
        response = adapter._on_card_action_trigger(data)
    finally:
        _stop_background_loop(loop, thread)

    assert response is not None
    assert response.card is None
    assert called == []


def test_feishu_card_action_trigger_registered_action_on_same_loop_does_not_deadlock(monkeypatch):
    action_name = "test.auth.same_loop.phase2"

    async def handler(ctx: CardActionContext) -> CardActionResponse:
        return CardActionResponse.replace_card(
            Card(header=CardHeader(title="同 loop 已处理", color="green"), elements=[Markdown(ctx.payload["request_id"])])
        )

    register_card_action(action_name, handler)
    _patch_feishu_callback_classes(monkeypatch)

    async def scenario():
        adapter = FeishuAdapter.__new__(FeishuAdapter)
        adapter._loop = asyncio.get_running_loop()
        _allow_all_interactive_callbacks(adapter)
        data = SimpleNamespace(
            event=SimpleNamespace(
                action=SimpleNamespace(
                    value={
                        "action": action_name,
                        "request_id": "req-same-loop",
                        "session_key": "session-click",
                    }
                ),
                operator=SimpleNamespace(open_id="ou_user", user_id="user_id"),
                context=SimpleNamespace(open_chat_id="oc_chat", open_message_id="om_msg"),
            )
        )
        return adapter._on_card_action_trigger(data)

    response = asyncio.run(scenario())

    assert response is not None
    assert response.card.type == "raw"
    assert response.card.data["header"]["title"] == {"tag": "plain_text", "content": "同 loop 已处理"}


def test_feishu_card_action_trigger_open_link_is_explicit_noop_not_legacy_fallback(monkeypatch):
    loop, thread = _start_background_loop()
    _patch_feishu_callback_classes(monkeypatch)
    adapter = FeishuAdapter.__new__(FeishuAdapter)
    adapter._loop = loop
    _allow_all_interactive_callbacks(adapter)
    submitted = []

    async def fake_handle(data):
        return None

    def fake_submit(active_loop, coro):
        submitted.append((active_loop, coro))
        coro.close()
        return True

    monkeypatch.setattr(adapter, "_handle_card_action_event", fake_handle)
    monkeypatch.setattr(adapter, "_submit_on_loop", fake_submit)
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(value={"action": "open_link", "terminal": "false", "flow_id": "flow"}),
            operator=SimpleNamespace(open_id="ou_user", user_id="user_id"),
            context=SimpleNamespace(open_chat_id="oc_chat", open_message_id="om_msg"),
        )
    )

    try:
        response = adapter._on_card_action_trigger(data)
    finally:
        _stop_background_loop(loop, thread)

    assert response is not None
    assert response.card is None
    assert submitted == []


def test_feishu_card_action_trigger_registered_action_allows_p2p_callback(monkeypatch):
    loop, thread = _start_background_loop()
    action_name = "test.auth.p2p.phase2"

    async def handler(ctx: CardActionContext) -> CardActionResponse:
        return CardActionResponse.replace_card(
            Card(header=CardHeader(title="DM 已处理", color="green"), elements=[Markdown(ctx.payload["request_id"])])
        )

    register_card_action(action_name, handler)
    _patch_feishu_callback_classes(monkeypatch)
    adapter = FeishuAdapter.__new__(FeishuAdapter)
    adapter._loop = loop
    adapter._admins = set()
    adapter._allowed_group_users = set()
    adapter._chat_info_cache = {}
    monkeypatch.setattr(adapter, "_allow_group_message", lambda *_args, **_kwargs: False)
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value={"action": action_name, "request_id": "req-p2p", "session_key": "session-p2p"}
            ),
            operator=SimpleNamespace(open_id="ou_user", user_id="user_id"),
            context=SimpleNamespace(open_chat_id="ou_user", open_message_id="om_msg", chat_type="p2p"),
        )
    )

    try:
        response = adapter._on_card_action_trigger(data)
    finally:
        _stop_background_loop(loop, thread)

    assert response is not None
    assert response.card.type == "raw"
    assert response.card.data["header"]["title"] == {"tag": "plain_text", "content": "DM 已处理"}


class _CardCapableAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(), Platform.FEISHU)
        self.card_sends = []
        self.text_sends = []

    async def connect(self, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.text_sends.append((chat_id, content, reply_to, metadata))
        return SendResult(success=True, message_id="om_text")

    async def send_card(self, chat_id, card, *, reply_to=None, metadata=None):
        self.card_sends.append((chat_id, card, reply_to, metadata))
        return SendResult(success=True, message_id="om_card")

    async def get_chat_info(self, chat_id):
        return {"name": chat_id, "type": "group"}


class _TextOnlyAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(), Platform.TELEGRAM)
        self.text_sends = []

    async def connect(self, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.text_sends.append((chat_id, content, reply_to, metadata))
        return SendResult(success=True, message_id="msg_text")

    async def get_chat_info(self, chat_id):
        return {"name": chat_id, "type": "group"}


def test_command_center_card_uses_gateway_action_payloads():
    card = build_command_center_card(
        profile="work",
        provider="custom:Crs.tencent-am.wzhecnu.cn",
        model="gpt-5.5",
        busy=True,
    )

    rendered = render_feishu_card(card, session_key="session-command-center")

    assert len(COMMAND_CENTER_GROUPS) == 6
    assert rendered["header"]["title"] == {"tag": "plain_text", "content": "Hermes Command Center"}
    first_group = rendered["elements"][2 + 1]["columns"][0]["elements"][0]
    assert first_group["value"] == {
        "action": COMMAND_CARD_ACTION_OPEN_GROUP,
        "session_key": "session-command-center",
        "group": "session",
    }
    actions = rendered["elements"][6]["actions"]
    assert actions[0]["value"] == {
        "action": COMMAND_CARD_ACTION_TEXT_HELP,
        "session_key": "session-command-center",
        "command": "help",
        "scope": "session",
    }
    assert actions[1]["value"]["action"] == COMMAND_CARD_ACTION_RUN
    assert actions[1]["value"]["command"] == "commands"


def test_command_run_payload_normalizes_without_synthetic_message_text():
    assert command_run_payload("/model", args="gpt-5.5", scope="global") == {
        "command": "model",
        "args": "gpt-5.5",
        "scope": "global",
    }
    with pytest.raises(ValueError, match="command cannot be empty"):
        command_run_payload("/")


def test_card_reply_renders_native_feishu_card_before_text_fallback():
    adapter = _CardCapableAdapter()
    reply = CardReply(
        card=build_command_center_card(model="gpt-5.5"),
        fallback_text="text fallback",
        session_key="session-42",
    )

    result = asyncio.run(
        adapter.send_card_reply(
            "oc_chat",
            reply,
            reply_to="om_anchor",
            metadata={"thread_id": "omt_thread"},
        )
    )

    assert result.success is True
    assert result.message_id == "om_card"
    assert adapter.text_sends == []
    chat_id, card, reply_to, metadata = adapter.card_sends[0]
    assert chat_id == "oc_chat"
    assert reply_to == "om_anchor"
    assert metadata == {"thread_id": "omt_thread"}
    assert card["header"]["title"] == {"tag": "plain_text", "content": "Hermes Command Center"}
    assert card["elements"][0]["content"].endswith("Agent: `idle`")


def test_card_reply_falls_back_to_text_on_non_card_platform():
    adapter = _TextOnlyAdapter()
    reply = CardReply(card=build_command_center_card(), fallback_text="plain help")

    result = asyncio.run(adapter.send_card_reply("chat", reply, reply_to="reply-1"))

    assert result.success is True
    assert result.message_id == "msg_text"
    assert adapter.text_sends == [("chat", "plain help", "reply-1", None)]
