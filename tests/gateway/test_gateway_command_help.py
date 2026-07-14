"""Gateway command help rendering tests."""

import pytest

from gateway.config import Platform
from gateway.cards.actions import CardActionContext, get_card_action_registry
from gateway.platforms.base import CardReply, MessageEvent
from gateway.session import SessionSource


def _make_event(text: str, platform: Platform) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=platform,
            chat_id="chat-1",
            user_id="user-1",
            user_name="tester",
            chat_type="dm",
        ),
    )


def _make_runner():
    from gateway.run import GatewayRunner

    return object.__new__(GatewayRunner)


def test_start_is_known_gateway_command():
    """Telegram sends /start automatically; gateway should intercept it as a no-op."""
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command

    cmd = resolve_command("start")
    assert "start" in GATEWAY_KNOWN_COMMANDS
    assert cmd is not None
    assert cmd.name == "start"


@pytest.mark.asyncio
async def test_help_sanitizes_slash_command_mentions_for_telegram(monkeypatch):
    """Telegram help output must not expose invalid uppercase/hyphenated slashes."""
    monkeypatch.setattr(
        "agent.skill_commands.get_skill_commands",
        lambda: {
            "/Linear": {"description": "Open Linear"},
            "/Custom-Thing": {"description": "Run a custom thing"},
        },
    )

    result = await _make_runner()._handle_help_command(
        _make_event("/help", Platform.TELEGRAM)
    )

    assert "`/linear`" in result
    assert "`/custom_thing`" in result
    assert "`/Linear`" not in result
    assert "`/Custom-Thing`" not in result


@pytest.mark.asyncio
async def test_commands_sanitizes_slash_command_mentions_for_telegram(monkeypatch):
    """Paginated Telegram /commands output uses Telegram-valid slash mentions."""
    monkeypatch.setattr(
        "agent.skill_commands.get_skill_commands",
        lambda: {"/Linear": {"description": "Open Linear"}},
    )

    result = await _make_runner()._handle_commands_command(
        _make_event("/commands 999", Platform.TELEGRAM)
    )

    assert "`/linear`" in result
    assert "`/Linear`" not in result


@pytest.mark.asyncio
async def test_help_keeps_non_telegram_slash_command_mentions_unchanged(monkeypatch):
    """Only Telegram needs slash mentions rewritten to Telegram command names."""
    monkeypatch.setattr(
        "agent.skill_commands.get_skill_commands",
        lambda: {"/Linear": {"description": "Open Linear"}},
    )

    result = await _make_runner()._handle_help_command(
        _make_event("/help", Platform.DISCORD)
    )

    assert "`/Linear`" in result


class _FakeFeishuCardAdapter:
    async def send_card(self, *args, **kwargs):  # pragma: no cover - handler only checks capability
        raise AssertionError("/help should return CardReply, not send directly")


@pytest.mark.asyncio
async def test_help_returns_command_center_card_for_feishu(monkeypatch):
    """Feishu /help should enter the command-card path when cards are available."""
    monkeypatch.setattr("agent.skill_commands.get_skill_commands", lambda: {})
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"model": {"provider": "custom:Crs.tencent-am.wzhecnu.cn", "default": "gpt-5.5"}},
    )
    runner = _make_runner()
    runner.adapters = {Platform.FEISHU: _FakeFeishuCardAdapter()}
    runner.config = None

    result = await runner._handle_help_command(_make_event("/help", Platform.FEISHU))

    assert isinstance(result, CardReply)
    assert result.fallback_text
    assert result.session_key
    assert result.card.header.title == "Hermes Command Center"
    assert result.card.elements[0].content == "Model: `custom:Crs.tencent-am.wzhecnu.cn/gpt-5.5`  |  Agent: `idle`"


@pytest.mark.asyncio
async def test_help_card_actions_replace_with_group_and_text_help(monkeypatch):
    """The first visible command-card buttons should resolve through the registry."""
    monkeypatch.setattr("agent.skill_commands.get_skill_commands", lambda: {})
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"model": {}})
    runner = _make_runner()
    runner.adapters = {Platform.FEISHU: _FakeFeishuCardAdapter()}
    runner.config = None
    await runner._handle_help_command(_make_event("/help", Platform.FEISHU))

    registry = get_card_action_registry()
    group_response = await registry.dispatch(
        CardActionContext(
            action="gateway.command.open_group",
            payload={"group": "model"},
            user_id="ou_user",
            chat_id="oc_chat",
            message_id="om_msg",
            session_key="session-1",
        )
    )
    assert group_response.kind == "replace_card"
    assert group_response.card.header.title == "Hermes Commands - Model"

    text_response = await registry.dispatch(
        CardActionContext(
            action="gateway.command.text_help",
            payload={},
            user_id="ou_user",
            chat_id="oc_chat",
            message_id="om_msg",
            session_key="session-1",
        )
    )
    assert text_response.kind == "replace_card"
    assert text_response.card.header.title == "Hermes Text Help"
