"""Fallback-switch pacing; independent of the provider's existing retry budget."""

from __future__ import annotations

import logging
import math
from typing import Any

from agent.error_classifier import FailoverReason

logger = logging.getLogger("agent.conversation_loop")
_TRANSIENT = frozenset({
    FailoverReason.rate_limit, FailoverReason.upstream_rate_limit,
    FailoverReason.overloaded, FailoverReason.timeout, FailoverReason.server_error,
})


def wait_before_fallback(
    agent: Any, api_error: Exception, reason: FailoverReason, retry: Any, *,
    messages: Any, conversation_history: Any, api_call_count: int,
    reset_at: Any = None,
) -> dict[str, Any] | None:
    """Wait 1→2→4… seconds between transiently failing routes when enabled.

    No provider request is retried here. The caller retains model retry/fallback decisions;
    this only paces the transition and preserves the ordinary interrupt/steer contract.
    """
    if reason not in _TRANSIENT or not agent._has_pending_fallback():
        return None
    from agent.fallback_cooldown import switch_deferred_by_reset
    if switch_deferred_by_reset(agent, reason, reset_at):
        return None
    from hermes_cli.config import load_config
    try:
        base = float((load_config().get("fallback") or {}).get("inter_switch_backoff_seconds") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(base) or base <= 0:
        return None
    from agent.retry_utils import jittered_backoff, retry_after_seconds
    from agent.turn_recovery import interruptible_backoff_sleep

    hop = max(1, int(agent._fallback_index) + 1)
    wait_s = min(jittered_backoff(hop, base_delay=min(base, 60), max_delay=60, jitter_ratio=0.2), 60)
    declared = retry_after_seconds(api_error)
    if declared is not None:
        wait_s = max(wait_s, min(declared, 600))
    notice = f"Waiting {wait_s:.1f}s before fallback hop {hop}"
    agent._emit_diagnostic_wait(notice)
    if wait_s > 10:
        agent._emit_diagnostic_status(notice)
    else:
        agent._buffer_diagnostic_status(notice)
    logger.warning("%s; reason=%s", notice, reason.value)
    return interruptible_backoff_sleep(
        agent, wait_s, retry, messages=messages, conversation_history=conversation_history,
        api_call_count=api_call_count,
        abort_message="Interrupt detected during fallback wait, aborting.",
        interrupt_text="Operation interrupted while waiting to try a fallback.",
        activity_label="fallback switch backoff",
    )
