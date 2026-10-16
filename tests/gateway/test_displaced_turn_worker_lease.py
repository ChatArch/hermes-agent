"""A displaced gateway turn retains its transcript fence until its worker exits."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway.run import GatewayRunner
from gateway.turn_lease import SessionTurnLeaseRegistry
from hermes_state import SessionDB


@pytest.mark.asyncio
async def test_displaced_worker_keeps_lease_until_durable_turn_exits(tmp_path, monkeypatch):
    key = "agent:main:feishu:dm:chat-a:thread-a"
    session_id = "shared-session"
    db = SessionDB(tmp_path / "state.db")
    runner = object.__new__(GatewayRunner)
    runner._persist_active_agents = lambda: None
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._turn_leases = SessionTurnLeaseRegistry()
    runner._run_in_executor_with_context = lambda fn: asyncio.to_thread(fn)
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")

    entered = threading.Event()
    finish = threading.Event()
    durable_released = threading.Event()
    holder = "pid=test:turn=old:platform=feishu"

    def old_turn():
        assert db.acquire_session_turn_lease(session_id, holder, wait_seconds=1)
        try:
            entered.set()
            assert finish.wait(10)
        finally:
            db.release_session_turn_lease(session_id, holder)
            durable_released.set()
        return {"final_response": "old turn done"}

    try:
        token = await runner._turn_leases.acquire(session_id, owner_key=key, generation=1)
        state = runner._session_state(key)
        state.turn.lease_tokens[1] = token
        state.persistent.run_generation = 1
        ctx = SimpleNamespace(agent_holder=[None], session_key=key, run_generation=1, session_id=session_id)
        worker = runner._run_agent_start_turn_worker(ctx, old_turn)
        assert await asyncio.to_thread(entered.wait, 5)

        displaced_generation = runner._invalidate_session_run_generation(key, reason="user_stop")
        runner._drop_turn_slot(key, run_generation=displaced_generation)
        assert runner._turn_leases._leases[session_id].holder is token
        assert not durable_released.is_set()

        # A different transcript remains usable while this one is still unwinding.
        sibling = await runner._turn_leases.acquire("sibling-session", owner_key="thread-b", generation=1)
        assert runner._turn_leases.release(sibling)

        successor = asyncio.create_task(runner._turn_leases.acquire(
            session_id, owner_key=key, generation=displaced_generation + 1, timeout=5,
        ))
        await asyncio.sleep(0)
        assert not successor.done()
        finish.set()
        assert await worker.executor_task == {"final_response": "old turn done"}
        new_token = await asyncio.wait_for(successor, 5)
        assert durable_released.is_set()
        assert runner._release_turn_lease(key, 1) is False
        assert runner._turn_leases._leases[session_id].holder is new_token
        assert runner._turn_leases.release(new_token)
    finally:
        finish.set()
        db.close()
