"""Official Astra overrides cannot bypass normalization via SDK extra_body."""
from copy import deepcopy

import pytest

from agent.transports.codex import ResponsesApiTransport


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("ttl", ["24h", "30m", None])
def test_astra_normalizes_both_override_layers_without_mutating_config(nested, ttl):
    body = {
        "reasoning": {"effort": "none", "summary": "auto"},
        "temperature": 0.5,
        "top_p": 0.9,
        "logprobs": True,
        "include": ["reasoning.encrypted_content", "message.output_text.logprobs"],
        "prompt_cache_retention": "24h",
        # Opaque future options must survive removing only the unsupported TTL.
        "prompt_cache_options": {"ttl": ttl, "future_option": "preserve"},
    }
    overrides = {"extra_body": body} if nested else body
    original = deepcopy(overrides)
    kwargs = ResponsesApiTransport().build_kwargs(
        model="gpt-6-astra", base_url="https://api.openai.com/v1",
        provider="openai", messages=[{"role": "user", "content": "Hi"}],
        tools=[], request_overrides=overrides,
    )
    effective = kwargs["extra_body"] if nested else kwargs
    assert effective["reasoning"] == {"effort": "low", "summary": "auto"}
    assert "prompt_cache_options" not in kwargs  # not an OpenAI SDK keyword
    assert kwargs["extra_body"]["prompt_cache_options"] == {"future_option": "preserve"}
    assert effective["include"] == ["reasoning.encrypted_content"]
    assert not {"temperature", "top_p", "logprobs", "prompt_cache_retention"} & effective.keys()
    assert overrides == original


@pytest.mark.parametrize("nested", [False, True])
def test_astra_omits_empty_cache_options_after_removing_ttl(nested):
    body = {"prompt_cache_options": {"ttl": "24h"}}
    kwargs = ResponsesApiTransport().build_kwargs(
        model="gpt-6-astra", base_url="https://api.openai.com/v1",
        messages=[{"role": "user", "content": "Hi"}], tools=[],
        request_overrides={"extra_body": body} if nested else body,
    )
    assert "prompt_cache_options" not in (kwargs["extra_body"] if nested else kwargs)


def test_astra_proxy_extra_body_is_not_rewritten():
    overrides = {"extra_body": {"reasoning": {"effort": "none"},
                                "temperature": 0.5,
                                "prompt_cache_options": {"ttl": "24h"}}}
    original = deepcopy(overrides)
    kwargs = ResponsesApiTransport().build_kwargs(
        model="gpt-6-astra", base_url="https://proxy.example.test/v1",
        messages=[{"role": "user", "content": "Hi"}], tools=[],
        request_overrides=overrides,
    )
    assert kwargs["extra_body"] == original["extra_body"]
    assert overrides == original


def _serialized_sdk_body(kwargs):
    """Exercise the real SDK serializer against an in-process mock transport."""
    import json
    import httpx
    from openai import OpenAI

    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "resp_test", "object": "response", "status": "completed",
            "model": "gpt-6-astra", "output": [],
        })

    with OpenAI(api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client:
        client.responses.create(**kwargs)
    assert len(captured) == 1
    return captured[0]


def test_astra_cache_options_cross_real_sdk_boundary_without_unknown_kwargs():
    kwargs = ResponsesApiTransport().build_kwargs(
        model="gpt-6-astra", base_url="https://api.openai.com/v1",
        messages=[{"role": "user", "content": "Hi"}], tools=[],
        request_overrides={"prompt_cache_options": {"ttl": "24h", "future_option": "preserve"}},
    )
    assert _serialized_sdk_body(kwargs)["prompt_cache_options"] == {"future_option": "preserve"}


@pytest.mark.parametrize("nested_effort,expected", [(None, "max"), ("none", "low")])
def test_astra_sdk_mixed_reasoning_overrides_preserve_effort(nested_effort, expected):
    nested = {"summary": "auto"}
    if nested_effort is not None:
        nested["effort"] = nested_effort
    overrides = {"reasoning": {"effort": "max"}, "extra_body": {"reasoning": nested}}
    original = deepcopy(overrides)
    kwargs = ResponsesApiTransport().build_kwargs(
        model="gpt-6-astra", base_url="https://api.openai.com/v1",
        messages=[{"role": "user", "content": "Hi"}], tools=[],
        request_overrides=overrides,
    )
    assert _serialized_sdk_body(kwargs)["reasoning"] == {"effort": expected, "summary": "auto"}
    assert overrides == original
