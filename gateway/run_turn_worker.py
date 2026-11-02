"""Executor worker lifecycle and displaced-turn lease handoff for GatewayRunner."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any, Callable

from agent.memory_provider import spawn_context_thread
from gateway.turn_context import TurnContext

if TYPE_CHECKING:
    from gateway.run import GatewayRunner


class GatewayTurnWorkerMixin:
    def _run_agent_start_turn_worker(self, turn_ctx: TurnContext, run_sync: Callable[[], Any]) -> GatewayRunner._RunAgentWorker:
        """Schedule ``run_sync`` on the executor plus the inactivity watchdog thread.

        *Inactivity* timeout (agent.gateway_timeout / HERMES_AGENT_TIMEOUT, env wins; 0 = unlimited),
        not wall-clock. The daemon watchdog is independent of asyncio: cgroup memory reclaim can
        starve the loop that runs the normal timeout poll."""
        from gateway.run import _float_env, _watch_gateway_turn_inactivity
        from tools.process_registry import process_registry
        agent_holder, session_key, run_generation = turn_ctx.agent_holder, turn_ctx.session_key, turn_ctx.run_generation
        _agent_timeout, _agent_warning = (
            v if v > 0 else None
            for v in (_float_env("HERMES_AGENT_TIMEOUT", 1800), _float_env("HERMES_AGENT_TIMEOUT_WARNING", 900))
        )

        # background=true processes survive a turn: reap only children created by THIS turn on timeout.
        _turn_task_id = turn_ctx.session_id or ""
        # The daemon watchdog is independent of asyncio: cgroup memory reclaim may starve the event loop
        # that runs the normal timeout poll, but it need not also postpone cleanup until the loop recovers
        # (#76115).
        _turn_process_baseline = process_registry.snapshot_running_ids(_turn_task_id)
        turn_ctx.process_task_id = _turn_task_id
        turn_ctx.process_baseline = _turn_process_baseline
        # task_id is session-scoped: gate the reap on this claim still being current so a replacement
        # turn's fresh process isn't killed by this turn's stale baseline.
        worker = self._RunAgentWorker(
            agent_timeout=_agent_timeout, agent_warning=_agent_warning, task_id=_turn_task_id,
            process_baseline=_turn_process_baseline, worker_done=threading.Event(),
            timeout_fired=threading.Event(), cleanup_lock=threading.Lock(),
            is_current=(
                (lambda: self._is_session_run_current(session_key, run_generation))
                if run_generation is not None
                else (lambda: True)
            ),
        )
        lease_state = self._peek_session_state(session_key) if session_key and run_generation is not None else None
        if lease_state is not None and run_generation in lease_state.turn.lease_tokens:
            lease_state.turn.lease_worker_done[run_generation] = worker.worker_done
        loop = asyncio.get_running_loop()

        def _run_sync_with_timeout_lifecycle():
            try:
                return run_sync()
            finally:
                worker.worker_done.set()
                if lease_state is not None and run_generation in lease_state.turn.lease_release_pending:
                    # The displaced outer coroutine may have ended before run_sync released its
                    # durable lease. Release this generation on the gateway loop after that exit.
                    loop.call_soon_threadsafe(self._release_turn_lease, session_key, run_generation)
                # `.turn.agent` stays reachable until the *next* turn is claimed; clearing the
                # ownership markers now means a /stop on the finished turn no longer reaps background
                # work it left running.
                # `.turn.agent` on the session state is only reset to _AGENT_PENDING_SENTINEL when the
                # *next* turn is claimed (see _session_state(...).turn.agent = ... at claim time), so a
                # stale reference to this exact agent instance stays reachable from
                # _interrupt_and_clear_session() until then. See #76115.
                _finished_agent = agent_holder[0] if agent_holder else None
                if _finished_agent is not None:
                    _finished_agent._gateway_turn_process_task_id = ""
                    _finished_agent._gateway_turn_process_baseline = frozenset()

        if _agent_timeout is not None:
            spawn_context_thread(
                target=_watch_gateway_turn_inactivity,
                kwargs={
                    "agent_holder": agent_holder, "timeout": _agent_timeout, "poll_interval": 5.0,
                    **self._reaper_kwargs(worker),
                },
                name=f"gateway-turn-watchdog-{_turn_task_id[:12]}",
                daemon=True,
            ).start()
        from gateway.session_runtime_workers import start_turn_worker
        start_turn_worker(self, worker, agent_holder, _run_sync_with_timeout_lifecycle)
        return worker
