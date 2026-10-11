"""Retired SSH environment activity/reaping interleavings are deterministic."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from gateway import ssh_switch_env as lifetime
from gateway.ssh_switch_store import SshStateUnavailable


class HeldEnvironment:
    def __init__(self):
        self.body_entered = threading.Event()
        self.body_release = threading.Event()
        self.cleanup_selected = threading.Event()
        self.cleanup_release = threading.Event()
        self.closed = 0

    def execute(self):
        self.body_entered.set()
        assert self.body_release.wait(3), "test failed to release execute"
        return "original-result"

    @property
    def cleanup(self):
        self.cleanup_selected.set()
        assert self.cleanup_release.wait(3), "test failed to release cleanup"
        return self._close

    def _close(self):
        self.closed += 1


@pytest.fixture
def isolated_lifetime(monkeypatch):
    from tools.environments import base
    from tools.process_registry import process_registry
    monkeypatch.setattr(lifetime, "_retired", {})
    monkeypatch.setattr(lifetime, "_busy", {})
    monkeypatch.setattr(process_registry, "_running", {})
    monkeypatch.setattr(base, "_live_foreground", {})
    monkeypatch.setattr(base, "_spawns_in_flight", 0)


def test_activity_cannot_enter_a_claimed_reap_or_lose_its_result(isolated_lifetime):
    env = HeldEnvironment()
    lifetime._track_activity(env)
    lifetime.retire_env(env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reaper = pool.submit(lifetime.reap_retired, 0)
        try:
            assert env.cleanup_selected.wait(2)
            operation = pool.submit(env.execute)
            # A claimed reap must deny a new activity before its original body runs.
            try:
                with pytest.raises(SshStateUnavailable, match="retir|closed|reap"):
                    operation.result(timeout=0.3)
            finally:
                env.cleanup_release.set()
                env.body_release.set()
                reaper.result(timeout=2)
                if not operation.done():
                    operation.result(timeout=2)
            assert not env.body_entered.is_set()
            assert env.closed == 1
        finally:
            env.cleanup_release.set()
            env.body_release.set()


def test_an_active_lease_cannot_be_reaped(isolated_lifetime):
    env = HeldEnvironment()
    lifetime._track_activity(env)
    with ThreadPoolExecutor(max_workers=1) as pool:
        operation = pool.submit(env.execute)
        try:
            assert env.body_entered.wait(2)
            lifetime.retire_env(env)
            lifetime.reap_retired(0)
            assert not env.cleanup_selected.is_set()
            assert env.closed == 0
        finally:
            env.body_release.set()
        assert operation.result(timeout=2) == "original-result"
    env.cleanup_release.set()
    lifetime.reap_retired(0)
    assert env.closed == 1


def test_owner_handoff_stays_leased_until_registry_publish(isolated_lifetime):
    from tools.process_registry import process_registry
    env = HeldEnvironment()
    env.cleanup_release.set()
    def publish_owner():
        from types import SimpleNamespace
        lifetime.retire_env(env)
        lifetime.reap_retired(0)
        assert env.closed == 0
        with process_registry._lock:
            process_registry._running["handoff"] = SimpleNamespace(env_ref=env)
        return "handed-off"
    assert lifetime.run_admitted(None, env, publish_owner) == "handed-off"
    lifetime.reap_retired(0)
    assert env.closed == 0
    with process_registry._lock:
        process_registry._running.clear()
    lifetime.reap_retired(0)
    assert env.closed == 1


def test_legacy_backend_without_lifetime_metadata_still_executes(isolated_lifetime):
    """A protocol-constrained legacy backend has no fabricated private state."""
    from unittest.mock import MagicMock
    from tools.environments.local import LocalEnvironment
    env = MagicMock(spec=LocalEnvironment)
    assert not hasattr(env, "_hermes_ssh_lifetime")
    env.execute.return_value = {"output": "original-output", "returncode": 7}
    assert lifetime.run_admitted(None, env, env.execute) == {
        "output": "original-output", "returncode": 7,
    }
    env.execute.assert_called_once_with()
    assert lifetime._busy == {}


@pytest.mark.parametrize("state", ["reaping", "closed", "quarantined", "unknown"])
def test_explicit_nonadmissible_lifetime_still_blocks_dynamic_backend(isolated_lifetime, state):
    from unittest.mock import MagicMock
    from tools.environments.local import LocalEnvironment
    env = MagicMock(spec=LocalEnvironment)
    env._hermes_ssh_lifetime = state
    with pytest.raises(SshStateUnavailable, match="reap|closed"):
        lifetime.run_admitted(None, env, env.execute)
    env.execute.assert_not_called()
    assert lifetime._busy == {}


def test_idle_cache_cleanup_obeys_the_same_activity_lease(isolated_lifetime, monkeypatch):
    from tools import terminal_tool, terminal_tool_lifecycle
    env = HeldEnvironment()
    env._hermes_ssh_snapshot = ("managed",)
    env.cleanup_release.set()
    lifetime._track_activity(env)
    monkeypatch.setattr(terminal_tool, "_active_environments", {"managed-cache": env})
    monkeypatch.setattr(terminal_tool, "_last_activity", {"managed-cache": 0})
    with ThreadPoolExecutor(max_workers=1) as pool:
        operation = pool.submit(env.execute)
        try:
            assert env.body_entered.wait(2)
            terminal_tool_lifecycle._cleanup_inactive_envs(1)
            assert env.closed == 0
        finally:
            env.body_release.set()
        assert operation.result(timeout=2) == "original-result"
    lifetime.reap_retired(0)
    assert env.closed == 1
