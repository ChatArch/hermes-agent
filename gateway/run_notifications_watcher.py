"""Raw process watcher lifecycle with fail-closed Feishu thread delivery."""
from __future__ import annotations

import asyncio
import logging
import time
from agent.i18n import t
from agent.retry_utils import jittered_backoff
from gateway.config import Platform
from gateway.run_shutdown import _log_suppressed

logger = logging.getLogger("gateway.run")
_WATCHER_ROUTE_FIELDS = ("session_key", "platform", "chat_type", "chat_id", "thread_id", "user_id", "user_name")
_MAX_COMPLETION_INJECTION_ATTEMPTS = 8
_COMPLETION_INJECTION_RETRY_MAX_DELAY = 60


class GatewayProcessWatcherMixin:
    async def _send_watcher_message(self, platform_name: str, chat_id, thread_id, message_text: str, watcher: dict) -> bool:
        from gateway.run import _non_conversational_metadata
        source = await asyncio.to_thread(self._build_process_event_source, watcher)
        if platform_name == Platform.FEISHU.value and source is None:
            logger.warning("Deferring raw Feishu watcher message without a resolved origin")
            return False
        adapter = self._resolve_injection_adapter(platform_name, source)
        target_chat = source.chat_id if source is not None else chat_id
        target_thread = source.thread_id if source is not None else thread_id
        if not adapter or not target_chat:
            return False
        reply_anchor = None
        if platform_name == Platform.FEISHU.value:
            if source is not None and not self._feishu_thread_route_matches(source, watcher):
                logger.warning("Deferring raw Feishu watcher message with conflicting restored thread route")
                return False
            if target_thread:
                reply_anchor = await self._feishu_thread_reply_anchor(source, adapter) if source is not None else None
            if target_thread and not reply_anchor:
                logger.warning("Deferring raw Feishu watcher message without a reply anchor")
                return False
        with _log_suppressed(logging.ERROR, "Watcher delivery error: %s"):
            send_meta = (self._thread_metadata_for_source(source, reply_anchor) if source is not None
                         else ({"thread_id": target_thread} if target_thread else None))
            result = await adapter.send(
                target_chat, message_text,
                metadata=_non_conversational_metadata(send_meta, platform=platform_name),
            )
            return bool(getattr(result, "success", False))
        return False

    @staticmethod
    def _build_process_completion_event(watcher: dict, session, session_id: str) -> dict:
        """Build the synthetic ``completion`` event for an agent-notify watcher."""
        from gateway.run import _redact_gateway_user_facing_secrets
        from agent.redact import redact_terminal_output
        from tools.ansi_strip import strip_ansi
        from tools.process_registry import transform_process_output
        _command = getattr(session, "command", "") or ""
        _raw = strip_ansi(session.output_buffer) if session.output_buffer else ""
        _raw = transform_process_output(_raw, command=_command, returncode=session.exit_code,
                                        task_id=getattr(session, "task_id", "") or "") if _raw else _raw
        _raw = redact_terminal_output(_raw, _command)
        # Keep the last ~2000 chars snapped to a line boundary, with a marker when cut.
        _LIMIT = 2000
        # Truncate at line boundaries so notifications never start mid-line (fixes #23284). Keep the last
        # ~2000 chars but snap to the nearest preceding newline, then prepend a truncation marker when
        # output was cut.
        if len(_raw) > _LIMIT:
            _tail = _raw[-_LIMIT:]
            _nl = _tail.find("\n")
            _tail = _tail[_nl + 1:] if _nl != -1 else _tail
            _out = f"[… output truncated — showing last {len(_tail)} chars]\n{_tail}"
        else:
            _out = _raw
        return {
            "type": "completion",
            "session_id": session_id,
            **{k: watcher.get(k, "") for k in _WATCHER_ROUTE_FIELDS},
            "owner_task_id": getattr(session, "owner_task_id", "") or "",
            "task_id": getattr(session, "task_id", "") or "",
            "message_id": str(watcher.get("message_id") or "").strip() or None,
            "started_at": getattr(session, "started_at", None),
            "command": _redact_gateway_user_facing_secrets(_command),
            "exit_code": session.exit_code,
            "completion_reason": getattr(session, "completion_reason", "exited"),
            "termination_source": getattr(session, "termination_source", ""),
            "output": _redact_gateway_user_facing_secrets(_out),
            # Spawning session-db id: lets pre-flight drop this completion if the user /new'd first.
            "parent_session_id": (
                watcher.get("parent_session_id") or getattr(session, "parent_session_id", "") or ""
            ),
        }

    def _format_process_final_message(self, session_id: str, session, notify_mode: str) -> str:
        """Human-facing completion message. Every mode shares the one-line status header; the
        raw-output modes (all/result/error) append the bounded output tail under it instead of the
        old bracketed ``[Background process proc_… finished~ …]`` debug wrapper (#54266)."""
        from gateway.run import _format_concise_process_notification, _redact_gateway_user_facing_secrets
        new_output = self._redacted_output_tail(session, 1000)
        _started = getattr(session, "started_at", None)
        _dur = max(0.0, time.time() - _started) if isinstance(_started, (int, float)) else None
        command = _redact_gateway_user_facing_secrets(getattr(session, "command", "") or "")
        if notify_mode == "concise":
            return _format_concise_process_notification(session_id, command, session.exit_code, new_output,
                                                        duration_seconds=_dur)
        header = _format_concise_process_notification(session_id, command, session.exit_code, "", duration_seconds=_dur)
        return t("gateway.background.final_output", header=header, output=new_output.strip()) if new_output.strip() else header

    def _format_process_running_message(self, session) -> str:
        from gateway.run import _redact_gateway_user_facing_secrets, _shorten_command_for_display
        new_output = self._redacted_output_tail(session, 500)
        short_cmd = _shorten_command_for_display(_redact_gateway_user_facing_secrets(getattr(session, "command", "") or ""))
        header = t("gateway.background.still_running") + (f" — `{short_cmd}`" if short_cmd else "")
        return t("gateway.background.recent_output", header=header, output=new_output.strip()) if new_output.strip() else header

    def arm_process_watcher(self, watcher: dict) -> bool:
        """Start ``_run_process_watcher`` for a watcher registered mid-turn, from the agent's
        tool thread. Waiting for the post-turn drain leaves a process that finishes while its
        launching turn is still running with no watcher at all (#112033). False = the gateway
        is not serving (startup, shutdown): the caller keeps the descriptor in
        ``pending_watchers`` for the startup / post-turn drain."""
        loop = getattr(self, "_gateway_loop", None)
        if not getattr(self, "_running", False) or loop is None or not loop.is_running():
            return False
        from agent.async_utils import safe_schedule_threadsafe
        future = safe_schedule_threadsafe(
            self._run_process_watcher(watcher), loop, logger=logger,
            log_message="Live process watcher arming failed",
        )
        return future is not None

    async def _run_process_watcher(self, watcher: dict) -> None:
        """Poll a background process and push updates until it exits. Mode
        (``display.background_process_notifications``): concise (default one-liner; failures append
        the output tail) / all (running updates + final raw) / result (final raw) / error (final raw
        if exit != 0) / off."""
        from tools.process_registry import process_registry
        from tools.process_registry_notifications import format_process_notification, should_surface_notification
        session_id = watcher["session_id"]
        interval = watcher["check_interval"]
        platform_name = watcher.get("platform", "")
        chat_id = watcher.get("chat_id", "")
        thread_id = watcher.get("thread_id", "")
        agent_notify = watcher.get("notify_on_complete", False)
        # The mode belongs to the profile that started the process; recovered watchers run in the
        # root context, so resolve it under the owning profile's scope (no-op for the default).
        async with self._completion_event_scope(watcher):
            notify_mode = self._load_background_notifications_mode()
        logger.debug("Process watcher started: %s (every %ss, notify=%s, agent_notify=%s)",
                      session_id, interval, notify_mode, agent_notify)
        silent = notify_mode == "off" and not agent_notify
        last_output_len = 0
        completion_attempts = 0
        retry_interval = interval
        while True:
            await asyncio.sleep(retry_interval)
            session = process_registry.get(session_id)
            if session is None:
                break
            async with self._completion_event_scope(watcher):
                surface = should_surface_notification({
                    "owner_task_id": getattr(session, "owner_task_id", ""),
                    "task_id": getattr(session, "task_id", ""),
                })
            if not surface:
                # Recheck live ownership next tick: handoff retains notification intent.
                if session.exited:
                    break
                continue
            if silent:
                # Still wait for the process to exit so we can log it, but don't push any messages.
                if session.exited:
                    break
                continue
            current_output_len = len(session.output_buffer)
            has_new_output = current_output_len > last_output_len
            last_output_len = current_output_len
            if session.exited:
                # Agent-notify: inject a synthetic message unless the agent already consumed the result via
                # wait/log (poll() is read-only and deliberately does NOT mark consumed).
                if agent_notify and not process_registry.is_completion_consumed(session_id):
                    completion_evt = self._build_process_completion_event(watcher, session, session_id)
                    synth_text = format_process_notification(completion_evt)
                    if not synth_text:
                        break
                    # Captured before injection: afterwards the key is busy either way (the injected
                    # turn itself installs the guard).
                    turn_busy = await self._launching_turn_active(platform_name, watcher)
                    delivered = await self._enqueue_process_completion_notification(synth_text, completion_evt)
                    if delivered is not False:
                        # The agent normally reports the result itself, so the chat gets no separate
                        # receipt. While the launching turn is still running the injection only queues a
                        # follow-up, and the chat would stay mute for as long as that turn lasts
                        # (#112033): send the concise receipt now.
                        if turn_busy and (notify_mode in {"concise", "all", "result"} or (
                            notify_mode == "error" and session.exit_code not in {0, None}
                        )):
                            message_text = self._format_process_final_message(session_id, session, "concise")
                            await self._send_watcher_message(platform_name, chat_id, thread_id, message_text, watcher)
                        break
                    # A refusal may clear (adapter reconnecting, store draining), so retry with backoff
                    # for a few minutes, then fall through to the plain notice instead of dropping it.
                    completion_attempts += 1
                    if completion_attempts < _MAX_COMPLETION_INJECTION_ATTEMPTS:
                        retry_interval = jittered_backoff(
                            completion_attempts, base_delay=max(interval, 1) * 2,
                            max_delay=_COMPLETION_INJECTION_RETRY_MAX_DELAY, jitter_ratio=0.0)
                        continue
                    if (getattr(self, 'session_authority', None) is not None
                            and self._completion_delivery_identity(completion_evt) is not None):
                        from gateway.session_automation import completion_admission
                        try:
                            # The owning profile's authority, as on the delivery path.
                            async with self._completion_event_scope(completion_evt):
                                committed = completion_admission(self, completion_evt) is not None
                        except Exception:
                            # Failing open sends the notice; a dead watcher would send nothing.
                            logger.debug("Process watcher: admission recheck failed for %s", session_id,
                                         exc_info=True)
                            committed = False
                        if committed:
                            break  # The last refusal was a lost ACK: the turn is committed.
                    logger.warning(
                        "Process watcher: completion for %s was refused %d times; falling back to the "
                        "notify_mode=%s notice", session_id, completion_attempts, notify_mode,
                    )
                # Text-only notification; skip when already consumed via wait/log (the agent_notify branch
                # FALLS THROUGH here, hence the re-check).
                if process_registry.is_completion_consumed(session_id):
                    logger.debug(
                        "Process watcher: completion for %s already consumed "
                        "via wait/log — skipping raw notification (#65379)", session_id,
                    )
                    break
                if notify_mode in {"concise", "all", "result"} or (
                    notify_mode == "error" and session.exit_code not in {0, None}
                ):
                    message_text = self._format_process_final_message(session_id, session, notify_mode)
                    from gateway.warning_notifications import present_notification
                    async with self._completion_event_scope(watcher):
                        # Non-zero exit is the automatic diagnostic; a clean completion is the requested result.
                        delivery = None

                        async def _present_final():
                            nonlocal delivery
                            delivery = await self._send_watcher_message(
                                platform_name, chat_id, thread_id, message_text, watcher)

                        await present_notification(
                            _present_final,
                            platform=platform_name, diagnostic=session.exit_code not in {0, None})
                        if delivery is False:
                            continue
                break
            elif has_new_output and notify_mode == "all" and not agent_notify:
                # New output — deliver a status update (only in "all" mode; agent_notify watchers
                # only care about completion).
                await self._send_watcher_message(
                    platform_name, chat_id, thread_id, self._format_process_running_message(session), watcher,
                )
        logger.debug("Process watcher ended%s: %s", " (silent)" if silent else "", session_id)
