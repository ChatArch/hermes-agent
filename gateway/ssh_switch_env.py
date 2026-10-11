"""Revision-checked environment acquisition/publication for managed SSH scopes."""

from __future__ import annotations

import logging
import time
import threading
import functools
from contextlib import nullcontext, contextmanager, ExitStack
from contextvars import ContextVar

from gateway.ssh_switch_state import assert_snapshot
from gateway.ssh_switch_store import SshStateUnavailable, store_lock

logger = logging.getLogger(__name__)
_retired = {}
_retired_lock = threading.RLock()
_busy = {}
_updating = ContextVar("ssh_projection_update", default=False)


@contextmanager
def projection_update():
    token = _updating.set(True)
    try:
        yield
    finally:
        _updating.reset(token)


def projection_update_active():
    return _updating.get()


def retire_env(env):
    with _retired_lock:
        if getattr(env, "_hermes_ssh_lifetime", "active") == "closed":
            return
        if getattr(env, "_hermes_ssh_lifetime", "active") == "active":
            env._hermes_ssh_lifetime = "retired"
        _retired.setdefault(id(env), (env, time.time()))


@contextmanager
def activity_lease(env):
    """Admission and reaping share one boundary; an admitted activity owns its env."""
    with _retired_lock:
        if getattr(env, "_hermes_ssh_lifetime", "active") not in {"active", "retired"}:
            raise SshStateUnavailable("SSH environment already reaping or closed; execution blocked")
        _busy[id(env)] = _busy.get(id(env), 0) + 1
    try:
        yield
    finally:
        with _retired_lock:
            _busy[id(env)] -= 1
            if not _busy[id(env)] and id(env) not in _retired:
                _busy.pop(id(env))


def _track_activity(env):
    if getattr(env, "_hermes_ssh_activity_tracked", False):
        return
    for name in ("execute", "materialize_file"):
        original = getattr(env, name, None)
        if not callable(original):
            continue

        def tracked(*args, _call=original, **kwargs):
            with activity_lease(env):
                return _call(*args, **kwargs)
        setattr(env, name, functools.wraps(original)(tracked))
    env._hermes_ssh_activity_tracked = True


def _claim_retired(key, lifetime_seconds):
    from tools.process_registry import process_registry
    from tools.environments import base
    # Owner publication and activity admission participate in this claim.
    with process_registry._lock:
        with base._live_foreground_cond:
            with _retired_lock:
                entry = _retired.get(key)
                if entry is None or base._spawns_in_flight or _busy.get(key):
                    return None
                env, retired_at = entry
                if time.time() - retired_at < lifetime_seconds:
                    return None
                if any(session.env_ref is env for session in process_registry._running.values()):
                    return None
                if any(owner is env for owner, _handle in base._live_foreground.values()):
                    return None
                if getattr(env, "_hermes_ssh_lifetime", "retired") == "reaping":
                    return None
                env._hermes_ssh_lifetime = "reaping"
                return env


def reap_retired(lifetime_seconds=300):
    with _retired_lock:
        candidates = tuple(_retired)
    for key in candidates:
        env = _claim_retired(key, lifetime_seconds)
        if env is None:
            continue
        try:
            cleanup = getattr(env, "cleanup", None)
            if cleanup is not None:
                cleanup()
        except Exception as exc:
            diagnostic = RuntimeError(f"Retired environment cleanup failed ({type(exc).__name__})")
            with _retired_lock:
                env._hermes_ssh_lifetime = "quarantined"
            logger.warning("Retired environment kept for later cleanup",
                           exc_info=(RuntimeError, diagnostic, exc.__traceback__))
            continue
        with _retired_lock:
            env._hermes_ssh_lifetime = "closed"
            _retired.pop(key, None)
            _busy.pop(key, None)


def _scope_lock(snapshot):
    return store_lock(snapshot[0]) if snapshot is not None else nullcontext()


def run_admitted(snapshot, owner_env, call, *args, **kwargs):
    """Keep the env leased through foreground startup and BG ownership publication."""
    with ExitStack() as lease:
        with _scope_lock(snapshot):
            assert_snapshot(snapshot)
            lease.enter_context(activity_lease(owner_env))
        return call(*args, **kwargs)


def cached_env(snapshot, cache, activity, lock, keys):
    """Acquisition is admitted under the same scope lock that begins a transition."""
    with _scope_lock(snapshot):
        assert_snapshot(snapshot)
        with lock:
            for key in keys:
                env = cache.get(key) if key else None
                if env is None:
                    continue
                stamp = getattr(env, "_hermes_ssh_snapshot", None)
                if snapshot is None or (stamp is not None and stamp[:5] == snapshot[:5]):
                    if snapshot is not None and len(snapshot) == 7:
                        env.cwd = snapshot[5]
                        env._hermes_ssh_snapshot = snapshot
                    activity[key] = time.time()
                    return env
                cache.pop(key, None)
                activity.pop(key, None)
                # Do not tear down an environment held by a pre-existing operation/process.
                retire_env(env)
    return None


def publish_env(snapshot, env, cache, activity, lock, key):
    try:
        with _scope_lock(snapshot):
            assert_snapshot(snapshot)
            if snapshot is not None:
                env._hermes_ssh_snapshot = snapshot
                _track_activity(env)
            with lock:
                cache[key] = env
                activity[key] = time.time()
    except SshStateUnavailable:
        # Only this unpublished constructor result is owned here, never a cached BG env.
        try:
            cleanup = getattr(env, "cleanup", None)
            if cleanup is not None:
                cleanup()
        except Exception as exc:
            diagnostic = RuntimeError(f"Unpublished environment cleanup failed ({type(exc).__name__})")
            logger.warning("Unpublished environment retained", exc_info=(RuntimeError, diagnostic, exc.__traceback__))
            retire_env(env)
        raise
    return env
