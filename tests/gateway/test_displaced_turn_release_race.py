"""The worker can finish between observing it alive and registering deferred release."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway.run import GatewayRunner
from gateway.turn_lease import SessionTurnLeaseRegistry


@pytest.mark.asyncio
async def test_worker_exit_during_deferred_release_registration_does_not_pin_lease(monkeypatch):
    key = "agent:main:feishu:dm:chat:topic"
    session_id = "release-race-session"
    runner = object.__new__(GatewayRunner)
    runner._persist_active_agents = lambda: None
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._turn_leases = SessionTurnLeaseRegistry()
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")
    entered, finish, worker_exited = (threading.Event() for _ in range(3))

    class ExitDuringRead:
        """Hold one stale is_set result until the real worker has left its finally."""

        def __init__(self):
            self.done = threading.Event()
            self.forced = False

        def set(self):
            self.done.set()

        def is_set(self):
            observed = self.done.is_set()
            if not observed and not self.forced:
                self.forced = True
                finish.set()
                assert worker_exited.wait(5), "worker failed to exit during the handoff"
            return observed

    worker_type = runner._RunAgentWorker

    def make_worker(**kwargs):
        kwargs["worker_done"] = ExitDuringRead()
        return worker_type(**kwargs)

    runner._RunAgentWorker = make_worker

    async def run_in_executor(fn):
        def observed_worker():
            try:
                return fn()
            finally:
                worker_exited.set()
        return await asyncio.to_thread(observed_worker)

    runner._run_in_executor_with_context = run_in_executor

    def old_turn():
        entered.set()
        assert finish.wait(5), "test did not release its owned worker"
        return {"final_response": "done"}

    token = await runner._turn_leases.acquire(session_id, owner_key=key, generation=1)
    state = runner._session_state(key)
    state.turn.lease_tokens[1] = token
    state.persistent.run_generation = 1
    ctx = SimpleNamespace(agent_holder=[None], session_key=key, run_generation=1, session_id=session_id)
    worker = runner._run_agent_start_turn_worker(ctx, old_turn)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        generation = runner._invalidate_session_run_generation(key, reason="user_stop")
        runner._drop_turn_slot(key, run_generation=generation)
        assert await worker.executor_task == {"final_response": "done"}
        await asyncio.sleep(0)
        assert token.released, "finished worker left its transcript lease permanently fenced"
        successor = await runner._turn_leases.acquire(session_id, owner_key=key, generation=generation + 1)
        assert runner._turn_leases.release(successor)
        assert not state.turn.lease_worker_done
        assert not state.turn.lease_release_pending
    finally:
        finish.set()
        await worker.executor_task
        runner._release_turn_lease(key, 1)
