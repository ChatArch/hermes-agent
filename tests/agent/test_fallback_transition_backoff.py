"""Fallback transitions are paced independently from the existing model retry budget."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.config import load_config, save_config
from run_agent import AIAgent


class RateLimitError(Exception):
    status_code = 429

    def __init__(self, retry_after=None):
        super().__init__("rate limit exceeded")
        self.response = SimpleNamespace(headers={"Retry-After": str(retry_after)} if retry_after else {})
        self.body = {"error": {"message": "rate limit exceeded"}}


def _agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="test-key", provider="openrouter", model="primary",
            base_url="https://openrouter.ai/api/v1", quiet_mode=True,
            skip_context_files=True, skip_memory=True,
            fallback_model=[
                {"provider": "deepseek", "model": "fallback-one"},
                {"provider": "deepseek", "model": "fallback-two"},
            ],
        )
    agent.client = MagicMock()
    return agent


def _response():
    message = SimpleNamespace(content="OK", tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="fallback-two", usage=None,
    )


@pytest.mark.parametrize(
    "delay, retry_after, interrupt, expected_waits",
    [(0, None, False, []), (1, None, False, [1, 2]),
     (1, 90, False, [1, 90]), (1, None, True, [1])],
)
def test_real_turn_paces_each_fallback_transition(delay, retry_after, interrupt, expected_waits):
    save_config({"fallback": {"inter_switch_backoff_seconds": delay}}, merge_existing=True)
    assert load_config()["fallback"]["inter_switch_backoff_seconds"] == delay
    agent = _agent()
    calls, waits = [], []
    client = MagicMock()
    client.base_url, client.api_key = "https://api.deepseek.com/v1", "fb-key"

    def api_call(_kwargs):
        calls.append(agent.model)
        if agent.model != "fallback-two":
            raise RateLimitError(retry_after if agent.model == "fallback-one" else None)
        return _response()

    def wait(_agent, seconds, _retry, **kwargs):
        waits.append(seconds)
        if interrupt:
            return {"final_response": "Stopped", "messages": kwargs["messages"],
                    "api_calls": 1, "completed": False, "interrupted": True}
        return None

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(client, "fb")),
        patch("agent.retry_utils.jittered_backoff", side_effect=lambda attempt, *, base_delay, **kw: base_delay * 2 ** (attempt - 1)),
        patch("agent.turn_recovery.interruptible_backoff_sleep", side_effect=wait),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("hi")

    assert waits == expected_waits
    assert calls == (["primary"] if interrupt else ["primary", "fallback-one", "fallback-two"])
    assert result.get("interrupted", False) is interrupt
    assert result["completed"] is (not interrupt)
    assert agent._api_max_retries == 3


@pytest.mark.parametrize("status, text", [(429, "insufficient_quota"), (401, "invalid api key")])
def test_deterministic_billing_and_auth_failover_is_not_paced(status, text):
    save_config({"fallback": {"inter_switch_backoff_seconds": 1}}, merge_existing=True)
    agent = _agent()
    calls = []
    client = MagicMock()
    client.base_url, client.api_key = "https://api.deepseek.com/v1", "fb-key"

    def api_call(_kwargs):
        calls.append(agent.model)
        if agent.model == "primary":
            error = Exception(text)
            error.status_code = status
            error.body = {"error": {"code": text, "message": text}}
            raise error
        return _response()

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=api_call),
        patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(client, "fb")),
        patch("agent.turn_recovery.interruptible_backoff_sleep") as wait,
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("hi")
    assert result["completed"] is True
    assert calls == ["primary", "fallback-one"]
    wait.assert_not_called()


class ServerError(Exception):
    status_code = 500

    def __init__(self):
        super().__init__("internal server error")
        self.response = SimpleNamespace(headers={})
        self.body = {"error": {"message": "internal server error"}}


def test_exhaustion_preserves_model_retries_and_paces_fallbacks():
    save_config({"fallback": {"inter_switch_backoff_seconds": 1}}, merge_existing=True)
    agent = _agent()
    calls, retry_waits, fallback_waits = [], [], []
    client = MagicMock()
    client.base_url, client.api_key = "https://api.deepseek.com/v1", "fb-key"

    def api_call(_kwargs):
        calls.append(agent.model)
        if agent.model != "fallback-two":
            raise ServerError()
        return _response()

    def wait_for_fallback(_agent, seconds, _retry, **kwargs):
        fallback_waits.append(seconds)
        return None

    def wait_for_retry(_agent, seconds, _retry, **kwargs):
        retry_waits.append(seconds)
        return None

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=api_call),
        patch.object(agent, "_try_recover_primary_transport", return_value=False),
        patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(client, "fb")),
        patch("agent.retry_utils.jittered_backoff", side_effect=lambda attempt, *, base_delay, **kw: base_delay * 2 ** (attempt - 1)),
        patch("agent.turn_api_error.interruptible_backoff_sleep", side_effect=wait_for_retry),
        patch("agent.turn_recovery.interruptible_backoff_sleep", side_effect=wait_for_fallback),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("hi")

    assert result["completed"] is True
    assert calls.count("primary") == agent._api_max_retries
    assert calls.count("fallback-one") == agent._api_max_retries
    assert calls[-1] == "fallback-two"
    assert len(retry_waits) == 2 * (agent._api_max_retries - 1)
    assert fallback_waits == [1, 2]
