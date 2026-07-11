from __future__ import annotations

from hermes_cli.oneshot import _run_agent


def test_oneshot_passes_configured_reasoning_effort(monkeypatch):
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def chat(self, prompt: str) -> str:
            return "ok"

    def fake_load_config():
        return {
            "model": {
                "default": "gpt-5.6-sol",
                "provider": "custom:crs.tencent-am.wzhecnu.cn",
            },
            "agent": {"reasoning_effort": "max"},
        }

    def fake_runtime(**_kwargs):
        return {
            "api_key": "test-key",
            "base_url": "https://gateway.example.com/openai/v1",
            "provider": "custom",
            "api_mode": "codex_responses",
            "credential_pool": None,
        }

    monkeypatch.setattr("hermes_cli.config.load_config", fake_load_config)
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fake_runtime)
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda _cfg, _platform: set())
    monkeypatch.setattr("hermes_cli.oneshot._create_session_db_for_oneshot", lambda: None)
    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)

    assert _run_agent("hello", toolsets=[], use_config_toolsets=False) == "ok"
    assert captured["reasoning_config"] == {"enabled": True, "effort": "max"}
