"""Synthetic goal, heartbeat and loop turns retain Feishu native-topic delivery."""

from __future__ import annotations

import asyncio
import time
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_identity import RoutingIdentity, identity_of


class _Messages:
    """Feishu's message endpoint with thread listing unavailable."""

    def __init__(self) -> None:
        self.replies: list[tuple[str, bool]] = []
        self.list_attempts = 0
        self.creates = []

    def reply(self, request):
        self.replies.append((request.message_id, request.request_body.reply_in_thread))
        return SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id=f"om_sent_{len(self.replies)}"),
        )

    def list(self, request):
        self.list_attempts += 1
        raise PermissionError("thread listing is unavailable")

    def create(self, request):
        self.creates.append(request)
        raise AssertionError("a topic reply must not create a parent-chat message")


@pytest.fixture
def feishu_wire(monkeypatch):
    from plugins.platforms.feishu import adapter as feishu_module

    # A missing anchor must fail promptly; retry timing is outside this contract.
    monkeypatch.setattr(feishu_module, "_FEISHU_SEND_ATTEMPTS", 1)
    # Processing badges use a separate external API; this file exercises message delivery.
    monkeypatch.setenv("FEISHU_REACTIONS", "false")
    adapter = feishu_module.FeishuAdapter(
        PlatformConfig(enabled=True, typing_indicator=False)
    )
    messages = _Messages()
    adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=messages)))
    yield adapter, messages
    adapter._shutdown_sdk_executor()


def _topic_source(*, thread_id: str = "omt_origin", message_id: str = "om_origin") -> SessionSource:
    return SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_chat",
        chat_type="dm",
        user_id="ou_user",
        thread_id=thread_id,
        message_id=message_id,
    )


async def _deliver(adapter, event: MessageEvent) -> None:
    key = adapter._event_session_key(event)
    await adapter.handle_message(event)
    assert event._gateway_accepted
    await asyncio.wait_for(adapter._session_tasks[key], timeout=5)


async def _wait_for_idle(adapter, key: str) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while (key in adapter._active_sessions or any(not task.done() for task in adapter._background_tasks)) \
            and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert key not in adapter._active_sessions
    assert all(task.done() for task in adapter._background_tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "internal"),
    [("goal", False), ("heartbeat", False), ("loop", True)],
)
async def test_synthetic_topic_final_uses_source_anchor_without_thread_listing(
    feishu_wire, kind, internal
):
    adapter, messages = feishu_wire
    source = _topic_source()

    async def agent_turn(event):
        return f"final for {event.text}"

    adapter.set_message_handler(agent_turn)
    event = GatewayRunner._synthetic_prompt_event(source, kind, internal=internal)
    if kind == "heartbeat":
        event._heartbeat_session_id = "heartbeat-session"

    await _deliver(adapter, event)

    assert messages.replies == [("om_origin", True)]
    assert messages.list_attempts == 0
    assert messages.creates == []
    assert source.message_id == "om_origin"


@pytest.mark.parametrize(
    ("platform", "thread_id", "expected_anchor"),
    [
        (Platform.FEISHU, "omt_origin", "om_origin"),
        (Platform.FEISHU, None, None),
        (Platform.TELEGRAM, "topic_1", None),
        (Platform.DISCORD, "thread_1", None),
    ],
)
def test_synthetic_source_keeps_routing_provenance_and_cleans_only_stale_quotes(
    tmp_path: Path, platform, thread_id, expected_anchor
):
    class Transport:
        pass

    transport = Transport()
    transport_ref = weakref.ref(transport)
    source = SessionSource(
        platform=platform,
        chat_id="chat_1",
        chat_type="dm",
        user_id="user_1",
        thread_id=thread_id,
        message_id="om_origin",
        profile="work",
    )
    identity = RoutingIdentity(
        transport_profile="default",
        runtime_profile="work",
        authorization_home=tmp_path,
        runtime_home=tmp_path,
        transport=transport_ref,
    )
    source._identity = identity
    source._transport_adapter_ref = transport_ref
    source._authorization_profile_home = tmp_path

    event = GatewayRunner._synthetic_prompt_event(source, "next step")

    assert source.message_id == "om_origin"
    assert event.source is not source
    assert event.source.message_id == expected_anchor
    assert event.source.thread_id == thread_id
    assert event.source.profile == "work"
    assert identity_of(event.source) is identity
    assert event.source._transport_adapter_ref is transport_ref
    assert event.source._authorization_profile_home == tmp_path


@pytest.mark.asyncio
async def test_chained_goal_and_status_stay_in_origin_topic_across_foreground_turn(feishu_wire):
    adapter, messages = feishu_wire
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {Platform.FEISHU: adapter}
    queued = []

    async def agent_turn(event):
        if event.text == "start goal":
            continuation = runner._synthetic_prompt_event(event.source, "goal step one")
            queued.append(continuation)
            runner._enqueue_fifo(adapter._event_session_key(event), continuation, adapter)
        return f"final for {event.text}"

    adapter.set_message_handler(agent_turn)
    origin = _topic_source()
    key = adapter._source_session_key(origin)
    await _deliver(adapter, MessageEvent(text="start goal", source=origin, message_id="om_origin"))
    await _wait_for_idle(adapter, key)
    assert len(queued) == 1

    foreground = _topic_source(thread_id="omt_other", message_id="om_other")
    await _deliver(adapter, MessageEvent(text="foreground", source=foreground, message_id="om_other"))

    second = runner._synthetic_prompt_event(queued[0].source, "goal step two")
    await _deliver(adapter, second)

    await runner._send_goal_status_notice(second.source, "goal status")

    assert messages.replies == [
        ("om_origin", True),
        ("om_origin", True),
        ("om_other", True),
        ("om_origin", True),
        ("om_origin", True),
    ]
    assert messages.list_attempts == 0
    assert messages.creates == []
    assert origin.message_id == "om_origin"
    assert foreground.message_id == "om_other"


@pytest.mark.asyncio
async def test_persisted_loop_wakeup_delivers_without_thread_listing(feishu_wire):
    from hermes_cli import loops

    adapter, messages = feishu_wire
    runner = GatewayRunner(GatewayConfig())
    runner.adapters = {Platform.FEISHU: adapter}
    source = _topic_source()

    async def agent_turn(event):
        return "loop tick finished"

    adapter.set_message_handler(agent_turn)
    event = MessageEvent(text="/loop 30s route check", source=source, message_id="om_origin")
    response = await runner._handle_loop_command(event)
    assert "Loop set" in response
    entry = await runner.async_session_store.get_or_create_session(source)
    state = loops.load_loop(entry.session_id)
    assert state is not None
    now = time.time()
    state.next_due_at = now - 1
    loops.save_loop(entry.session_id, state)
    restored = loops.load_loop(entry.session_id)

    await runner._loop_wakeup_fire_one(entry.session_id, restored, now, set())
    await _wait_for_idle(adapter, adapter._source_session_key(source))

    assert messages.replies == [("om_origin", True)]
    assert messages.list_attempts == 0
    assert messages.creates == []
