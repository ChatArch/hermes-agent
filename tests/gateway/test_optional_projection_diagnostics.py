"""Soft gateway projections retain fallback results and private diagnostic tracebacks."""
import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from gateway.config import Platform
from gateway.relay.adapter_discord_interaction import RelayDiscordInteractionMixin
from gateway.run_inbound_transcription import GatewayInboundTranscriptionMixin
from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
from gateway.run_turn_progress import GatewayTurnProgressMixin

CANARY = "DIAGNOSTIC_PRIVATE_VALUE"


def _private_trace(caplog, message):
    records = [record for record in caplog.records if record.getMessage() == message]
    assert records and records[-1].exc_info is not None
    formatter = logging.Formatter("%(message)s")
    assert CANARY not in "\n".join(formatter.format(record) for record in caplog.records)


def test_unusable_relay_body_is_still_ignored_with_a_private_trace(caplog):
    caplog.set_level(logging.DEBUG)
    runner = RelayDiscordInteractionMixin()
    assert runner._discord_interaction_to_event(SimpleNamespace(body=CANARY.encode())) is None
    _private_trace(caplog, "Relay Discord interaction could not be decoded; ignoring unusable payload")


def test_reasoning_config_failure_keeps_mattermost_scratch_hidden(monkeypatch, caplog):
    import gateway.run as facade
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(facade, "_load_gateway_config", Mock(side_effect=RuntimeError(CANARY)))
    runner = GatewayTurnPersistenceMixin()
    runner._show_reasoning = True
    result = runner._hmwa_prepend_reasoning({"last_reasoning": "private scratch"}, "answer", SimpleNamespace(platform=Platform.MATTERMOST), False)
    assert result == "answer"
    _private_trace(caplog, "Reasoning display configuration unavailable; retaining platform fallback")


def test_reasoning_style_failure_retains_code_style(monkeypatch, caplog):
    import gateway.run as facade
    import gateway.display_config as display
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(facade, "_load_gateway_config", dict)
    monkeypatch.setattr(facade, "_resolve_gateway_display_bool", lambda *a, **k: True)
    monkeypatch.setattr(display, "resolve_display_setting", Mock(side_effect=RuntimeError(CANARY)))
    runner = GatewayTurnPersistenceMixin()
    result = runner._hmwa_prepend_reasoning({"last_reasoning": "scratch"}, "answer", SimpleNamespace(platform=Platform.TELEGRAM), False)
    assert "scratch" in result and "answer" in result
    _private_trace(caplog, "Reasoning style unavailable; retaining code-block fallback")


def test_footer_config_failure_stays_an_empty_footer(monkeypatch, caplog):
    import gateway.run as facade
    import gateway.session_finite as finite
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(finite, "finite_turn_required", lambda: False)
    monkeypatch.setattr(facade, "_load_gateway_config", Mock(side_effect=RuntimeError(CANARY)))
    assert GatewayTurnPersistenceMixin()._hmwa_runtime_footer_line({}, SimpleNamespace(platform=Platform.TELEGRAM), 1) == ""
    _private_trace(caplog, "Runtime footer unavailable; retaining normal final response")


def _delivery_runner(adapter):
    runner = GatewayTurnPersistenceMixin()
    runner._delivery_adapter_for = lambda source: adapter
    runner._should_send_voice_reply = lambda *a, **k: False
    runner._deliver_media_from_response = AsyncMock()
    runner._event_thread_metadata = lambda *a: None
    return runner


def test_remote_media_failure_keeps_safe_fallback(caplog):
    caplog.set_level(logging.DEBUG)
    runner = _delivery_runner(None)
    runner._materialize_media_for_delivery = AsyncMock(side_effect=RuntimeError(CANARY))
    runner._remote_media_failure_response = lambda text: "safe attachment fallback"
    event = SimpleNamespace(internal=False, metadata={})
    result = asyncio.run(runner._hmwa_deliver_turn_response(event, SimpleNamespace(), SimpleNamespace(session_id="sid"), "key", 1, {}, [], "MEDIA:ssh://example.test/file.png", "", False))
    assert result == "safe attachment fallback"
    _private_trace(caplog, "Remote media materialization failed; retaining safe attachment fallback")


def test_streamed_footer_send_failure_never_duplicates_final(caplog):
    caplog.set_level(logging.DEBUG)
    adapter = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError(CANARY)))
    runner = _delivery_runner(adapter)
    event = SimpleNamespace(internal=False, metadata={})
    result = asyncio.run(runner._hmwa_deliver_turn_response(event, SimpleNamespace(chat_id="chat"), SimpleNamespace(session_id="sid"), "key", 1, {"already_sent": True}, [], "answer", "footer", False))
    assert result is None and event._streamed_final_response == "answer"
    _private_trace(caplog, "Trailing footer send failed; retaining streamed final response")


def test_progress_adapter_failure_retains_plain_progress(caplog):
    caplog.set_level(logging.DEBUG)
    runner = GatewayTurnProgressMixin()
    runner._ctx = SimpleNamespace(source=object(), progress_mode="new", last_was_terminal_block=[False])
    runner._runner = SimpleNamespace(_delivery_adapter_for=Mock(side_effect=RuntimeError(CANARY)))
    runner._progress_terminal_blocks = lambda *a: (None, None)
    assert runner._progress_build_message("terminal", "", {})
    _private_trace(caplog, "Progress adapter unavailable; retaining plain progress fallback")


def test_voice_ack_schedule_failure_keeps_turn_running(caplog):
    caplog.set_level(logging.DEBUG)
    runner = GatewayTurnProgressMixin()
    runner._ctx = SimpleNamespace(_voice_ack_fired=[False], _voice_ack_guild=[1], _run_still_current=lambda: True, _voice_ack_loop=None, source=object())
    runner._runner = SimpleNamespace(_delivery_adapter_for=lambda source: SimpleNamespace(play_ack_in_voice=lambda guild: object()))
    runner._schedule = Mock(side_effect=RuntimeError(CANARY))
    assert runner.voice_ack_callback("call", "terminal", {}) is None
    assert runner._ctx._voice_ack_fired == [True]
    _private_trace(caplog, "Voice acknowledgement scheduling failed; keeping normal turn output")


def test_disabled_stt_path_resolution_is_off_loop(monkeypatch):
    import gateway.run as facade
    import gateway.run_inbound_transcription as transcription
    caller, observed = threading.get_ident(), []
    original = transcription.os.path.abspath

    def resolve(path):
        if path == "voice.ogg":
            observed.append(threading.get_ident())
        return original(path)

    monkeypatch.setattr(transcription.os.path, "abspath", resolve)
    monkeypatch.setattr(facade, "_probe_audio_duration", AsyncMock(return_value=""))
    runner = GatewayInboundTranscriptionMixin()
    runner.config = SimpleNamespace(stt_enabled=False)
    text, transcripts = asyncio.run(runner._enrich_message_with_transcription("caption", ["voice.ogg"]))
    assert "caption" in text and transcripts == []
    assert observed and caller not in observed


def test_transcription_fault_retains_audio_note(caplog):
    caplog.set_level(logging.DEBUG)
    runner = GatewayInboundTranscriptionMixin()
    runner.config = SimpleNamespace(stt_enabled=True)
    runner._transcribe_one_clip = AsyncMock(side_effect=RuntimeError(CANARY))
    text, transcripts = asyncio.run(runner._enrich_message_with_transcription("caption", ["voice.ogg"]))
    assert "caption" in text and "could not be transcribed" in text and transcripts == []
    _private_trace(caplog, "Voice transcription failed; retaining untranscribed audio note")


def test_pending_transcription_fault_retains_caption(caplog):
    caplog.set_level(logging.DEBUG)
    runner = GatewayInboundTranscriptionMixin()
    runner._pending_event_audio_paths = lambda event: ["voice.ogg"]
    runner._transcribe_pending_audio_event_once = AsyncMock(side_effect=RuntimeError(CANARY))
    assert asyncio.run(runner._transcribe_and_echo_pending_voice(object(), None, None, "caption", log_context=CANARY)) == ("caption", [])
    _private_trace(caplog, "Pending voice transcription failed; retaining original text")
