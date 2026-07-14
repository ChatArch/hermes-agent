"""Tests for gateway /reasoning command and hot reload behavior."""

import asyncio
import inspect
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.cards.actions import CardActionContext, get_card_action_registry
from gateway.cards.renderers.feishu import render_feishu_card
from gateway.platforms.base import CardReply, MessageEvent
from gateway.session import SessionSource


def _make_event(text="/reasoning", platform=Platform.TELEGRAM, user_id="12345", chat_id="67890"):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    """Create a bare GatewayRunner without calling __init__."""
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._session_reasoning_overrides = {}
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    runner._session_db = None
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    return runner


class _CapturingAgent:
    """Fake agent that records init kwargs for assertions."""

    last_init = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message: str, conversation_history=None, task_id=None):
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
        }


class TestReasoningCommand:
    @pytest.mark.asyncio
    async def test_reasoning_in_help_output(self):
        runner = _make_runner()
        event = _make_event(text="/help")

        result = await runner._handle_help_command(event)

        # Behaviour contract: /reasoning is surfaced in help. Don't freeze the
        # exact args-hint literal — it changes whenever a new arg is added
        # (e.g. full/clamp). Assert the command + its category-defining args.
        assert "/reasoning" in result
        assert "level" in result and "show" in result and "hide" in result

    def test_reasoning_is_known_command(self):
        source = inspect.getsource(gateway_run.GatewayRunner._handle_message)
        assert '"reasoning"' in source

    def test_parse_reasoning_command_args_accepts_ascii_and_smart_global_flags(self):
        assert gateway_run.GatewayRunner._parse_reasoning_command_args("high --global") == ("high", True)
        assert gateway_run.GatewayRunner._parse_reasoning_command_args("—global xhigh") == ("xhigh", True)

    @pytest.mark.asyncio
    async def test_reasoning_command_reloads_current_state_from_config(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "agent:\n  reasoning_effort: none\ndisplay:\n  show_reasoning: true\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        runner._reasoning_config = {"enabled": True, "effort": "xhigh"}
        runner._show_reasoning = False

        result = await runner._handle_reasoning_command(_make_event("/reasoning"))

        assert "**Effort:** `none (disabled)`" in result
        assert "**Display:** on ✓" in result
        assert runner._reasoning_config == {"enabled": False}
        assert runner._show_reasoning is True

    @pytest.mark.asyncio
    async def test_reasoning_feishu_returns_selector_and_click_sets_session(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "agent:\n  reasoning_effort: medium\ndisplay:\n  show_reasoning: false\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        event = _make_event("/reasoning", platform=Platform.FEISHU)
        session_key = runner._session_key_for_source(event.source)

        result = await runner._handle_reasoning_command(event)

        assert isinstance(result, CardReply)
        assert result.session_key == session_key
        assert result.card.header.title == "推理强度"
        rendered = render_feishu_card(result.card, session_key=result.session_key)
        assert rendered["header"]["title"]["content"] == "推理强度"
        assert rendered["elements"][0]["tag"] == "markdown"
        assert "当前：`medium`" in rendered["elements"][0]["content"]
        effort_block = rendered["elements"][1]
        assert effort_block["tag"] == "action"
        effort_select = effort_block["actions"][0]
        assert effort_select["tag"] == "select_static"
        assert effort_select["placeholder"]["content"] == "选择推理强度"
        assert [
            option["text"]["content"] for option in effort_select["options"]
        ] == ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
        reset_block = rendered["elements"][2]
        assert reset_block["tag"] == "action"
        assert reset_block["actions"][0]["tag"] == "button"
        assert reset_block["actions"][0]["text"]["content"] == "重置"
        assert result.fallback_text

        response = await get_card_action_registry().dispatch(
            CardActionContext(
                action="gateway.reasoning.select",
                payload={"op": "set", "effort": "xhigh"},
                user_id="ou_user",
                chat_id="oc_chat",
                message_id="om_reasoning",
                session_key=session_key,
            )
        )

        assert response.kind == "replace_card"
        assert response.card.header.title == "推理强度已更新"
        assert runner._session_reasoning_overrides[session_key] == {"enabled": True, "effort": "xhigh"}

    @pytest.mark.asyncio
    async def test_handle_reasoning_command_updates_config_and_cache(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("agent:\n  reasoning_effort: medium\n", encoding="utf-8")

        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        runner._reasoning_config = {"enabled": True, "effort": "medium"}

        result = await runner._handle_reasoning_command(_make_event("/reasoning low --global"))

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert saved["agent"]["reasoning_effort"] == "low"
        assert runner._reasoning_config == {"enabled": True, "effort": "low"}
        assert "takes effect on next message" in result

    @pytest.mark.asyncio
    async def test_handle_reasoning_command_accepts_shared_max_effort(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("agent:\n  reasoning_effort: medium\n", encoding="utf-8")

        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        runner._reasoning_config = {"enabled": True, "effort": "medium"}

        result = await runner._handle_reasoning_command(_make_event("/reasoning max --global"))

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert saved["agent"]["reasoning_effort"] == "max"
        assert runner._reasoning_config == {"enabled": True, "effort": "max"}
        assert "saved to config" in result

    @pytest.mark.asyncio
    async def test_handle_reasoning_command_defaults_to_session_only(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("agent:\n  reasoning_effort: medium\n", encoding="utf-8")

        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        event = _make_event("/reasoning high")
        session_key = runner._session_key_for_source(event.source)

        result = await runner._handle_reasoning_command(event)

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert saved["agent"]["reasoning_effort"] == "medium"
        assert runner._session_reasoning_overrides[session_key] == {"enabled": True, "effort": "high"}
        assert runner._reasoning_config == {"enabled": True, "effort": "high"}
        assert "session only" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("effort", ["max", "ultra"])
    async def test_handle_reasoning_command_accepts_extended_efforts(
        self, tmp_path, monkeypatch, effort
    ):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "agent:\n  reasoning_effort: medium\n", encoding="utf-8"
        )
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        event = _make_event(f"/reasoning {effort}")
        session_key = runner._session_key_for_source(event.source)

        await runner._handle_reasoning_command(event)

        assert runner._session_reasoning_overrides[session_key] == {
            "enabled": True,
            "effort": effort,
        }

    @pytest.mark.asyncio
    async def test_reasoning_global_clears_existing_session_override(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("agent:\n  reasoning_effort: medium\n", encoding="utf-8")

        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        event = _make_event("/reasoning low --global")
        session_key = runner._session_key_for_source(event.source)
        runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "xhigh"}

        result = await runner._handle_reasoning_command(event)

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert saved["agent"]["reasoning_effort"] == "low"
        assert session_key not in runner._session_reasoning_overrides
        assert "saved to config" in result

    @pytest.mark.asyncio
    async def test_reasoning_reset_clears_session_override_without_config_write(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("agent:\n  reasoning_effort: medium\n", encoding="utf-8")

        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        event = _make_event("/reasoning reset")
        session_key = runner._session_key_for_source(event.source)
        runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "xhigh"}

        result = await runner._handle_reasoning_command(event)

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert saved["agent"]["reasoning_effort"] == "medium"
        assert session_key not in runner._session_reasoning_overrides
        assert "cleared" in result

    def test_resolve_session_reasoning_prefers_session_override(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("agent:\n  reasoning_effort: low\n", encoding="utf-8")

        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        source = _make_event("/reasoning").source
        session_key = runner._session_key_for_source(source)
        runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "xhigh"}

        assert runner._resolve_session_reasoning_config(source=source) == {"enabled": True, "effort": "xhigh"}

    def test_run_agent_reloads_reasoning_config_per_message(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("agent:\n  reasoning_effort: low\n", encoding="utf-8")

        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
        monkeypatch.setattr(gateway_run, "_env_path", hermes_home / ".env")
        monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            gateway_run,
            "_resolve_runtime_agent_kwargs",
            lambda: {
                "provider": "openrouter",
                "api_mode": "chat_completions",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "test-key",
            },
        )
        fake_run_agent = types.ModuleType("run_agent")
        fake_run_agent.AIAgent = _CapturingAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

        _CapturingAgent.last_init = None
        runner = _make_runner()
        runner._reasoning_config = {"enabled": True, "effort": "xhigh"}

        source = SessionSource(
            platform=Platform.LOCAL,
            chat_id="cli",
            chat_name="CLI",
            chat_type="dm",
            user_id="user-1",
        )

        result = asyncio.run(
            runner._run_agent(
                message="ping",
                context_prompt="",
                history=[],
                source=source,
                session_id="session-1",
                session_key="agent:main:local:dm",
            )
        )

        assert result["final_response"] == "ok"
        assert _CapturingAgent.last_init is not None
        assert _CapturingAgent.last_init["reasoning_config"] == {"enabled": True, "effort": "low"}

    def test_run_agent_prefers_session_reasoning_override(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("agent:\n  reasoning_effort: low\n", encoding="utf-8")

        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
        monkeypatch.setattr(gateway_run, "_env_path", hermes_home / ".env")
        monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            gateway_run,
            "_resolve_runtime_agent_kwargs",
            lambda: {
                "provider": "openrouter",
                "api_mode": "chat_completions",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "***",
            },
        )
        fake_run_agent = types.ModuleType("run_agent")
        fake_run_agent.AIAgent = _CapturingAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

        _CapturingAgent.last_init = None
        runner = _make_runner()
        session_key = "agent:main:local:dm"
        runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}

        source = SessionSource(
            platform=Platform.LOCAL,
            chat_id="cli",
            chat_name="CLI",
            chat_type="dm",
            user_id="user-1",
        )

        result = asyncio.run(
            runner._run_agent(
                message="ping",
                context_prompt="",
                history=[],
                source=source,
                session_id="session-1",
                session_key=session_key,
            )
        )

        assert result["final_response"] == "ok"
        assert _CapturingAgent.last_init is not None
        assert _CapturingAgent.last_init["reasoning_config"] == {"enabled": True, "effort": "high"}

    def test_run_agent_includes_enabled_mcp_servers_in_gateway_toolsets(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "platform_toolsets:\n"
            "  cli: [web, memory]\n"
            "mcp_servers:\n"
            "  exa:\n"
            "    url: https://mcp.exa.ai/mcp\n"
            "  web-search-prime:\n"
            "    url: https://api.z.ai/api/mcp/web_search_prime/mcp\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
        monkeypatch.setattr(gateway_run, "_env_path", hermes_home / ".env")
        monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            gateway_run,
            "_resolve_runtime_agent_kwargs",
            lambda: {
                "provider": "openrouter",
                "api_mode": "chat_completions",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "test-key",
            },
        )
        fake_run_agent = types.ModuleType("run_agent")
        fake_run_agent.AIAgent = _CapturingAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

        _CapturingAgent.last_init = None
        runner = _make_runner()

        source = SessionSource(
            platform=Platform.LOCAL,
            chat_id="cli",
            chat_name="CLI",
            chat_type="dm",
            user_id="user-1",
        )

        result = asyncio.run(
            runner._run_agent(
                message="ping",
                context_prompt="",
                history=[],
                source=source,
                session_id="session-1",
                session_key="agent:main:local:dm",
            )
        )

        assert result["final_response"] == "ok"
        assert _CapturingAgent.last_init is not None
        enabled_toolsets = set(_CapturingAgent.last_init["enabled_toolsets"])
        assert "web" in enabled_toolsets
        assert "memory" in enabled_toolsets
        assert "exa" in enabled_toolsets
        assert "web-search-prime" in enabled_toolsets

    def test_run_agent_homeassistant_uses_default_platform_toolset(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("", encoding="utf-8")

        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
        monkeypatch.setattr(gateway_run, "_env_path", hermes_home / ".env")
        monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            gateway_run,
            "_resolve_runtime_agent_kwargs",
            lambda: {
                "provider": "openrouter",
                "api_mode": "chat_completions",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "test-key",
            },
        )
        fake_run_agent = types.ModuleType("run_agent")
        fake_run_agent.AIAgent = _CapturingAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

        _CapturingAgent.last_init = None
        runner = _make_runner()

        source = SessionSource(
            platform=Platform.HOMEASSISTANT,
            chat_id="ha",
            chat_name="Home Assistant",
            chat_type="dm",
            user_id="user-1",
        )

        result = asyncio.run(
            runner._run_agent(
                message="ping",
                context_prompt="",
                history=[],
                source=source,
                session_id="session-1",
                session_key="agent:main:homeassistant:dm",
            )
        )

        assert result["final_response"] == "ok"
        assert _CapturingAgent.last_init is not None
        assert "homeassistant" in set(_CapturingAgent.last_init["enabled_toolsets"])


class TestLoadShowReasoningCoercion:
    """Regression: display.show_reasoning must be coerced, not bool()'d."""

    def _load_with_config(self, tmp_path, monkeypatch, yaml_body: str) -> bool:
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(yaml_body, encoding="utf-8")
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
        return gateway_run.GatewayRunner._load_show_reasoning()

    def test_quoted_false_is_false(self, tmp_path, monkeypatch):
        assert self._load_with_config(
            tmp_path, monkeypatch,
            'display:\n  show_reasoning: "false"\n',
        ) is False

    def test_quoted_off_is_false(self, tmp_path, monkeypatch):
        assert self._load_with_config(
            tmp_path, monkeypatch,
            'display:\n  show_reasoning: "off"\n',
        ) is False

    def test_quoted_true_is_true(self, tmp_path, monkeypatch):
        assert self._load_with_config(
            tmp_path, monkeypatch,
            'display:\n  show_reasoning: "true"\n',
        ) is True

    def test_bare_true_is_true(self, tmp_path, monkeypatch):
        assert self._load_with_config(
            tmp_path, monkeypatch,
            'display:\n  show_reasoning: true\n',
        ) is True

    def test_missing_is_false(self, tmp_path, monkeypatch):
        assert self._load_with_config(
            tmp_path, monkeypatch,
            'display: {}\n',
        ) is False
