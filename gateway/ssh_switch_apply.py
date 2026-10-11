"""The SSH feature's single apply boundary; authorization stays with its callers."""

from __future__ import annotations

import logging
import time

from gateway import ssh_bindings as bindings
from gateway.ssh_switch_state import begin_switch, commit_switch, fail_switch, inspect_switch, committed_overrides
from gateway.ssh_switch_store import SshStateUnavailable, store_lock, serialized_store
from gateway.ssh_targets import find_ssh_target, validate_ssh_target_for_runtime

logger = logging.getLogger(__name__)


def _failure_trace(exc):
    return RuntimeError, RuntimeError(f"SSH transition failed ({type(exc).__name__})"), exc.__traceback__


@serialized_store
def apply_backend_selection(session_key, task_ids, backend, *, targets, cwd=None,
                            source="user", reason=None, path=None):
    """Publish ready only after every managed projection was updated successfully."""
    from tools import terminal_tool
    from gateway.ssh_switch_env import projection_update_active
    if projection_update_active():
        raise SshStateUnavailable("SSH switch already active; new execution blocked")

    backend = bindings.normalize_backend_name(backend)
    previous = bindings.get_ssh_binding(session_key, path=path)
    if bindings.is_local_backend(backend):
        selected = None
        overrides = None
    else:
        target = find_ssh_target(targets, backend)
        if target is None or validate_ssh_target_for_runtime(target):
            raise SshStateUnavailable("SSH target unavailable; selection was not changed")
        selected = bindings.SshBinding(
            session_key=session_key, alias=backend, cwd=cwd, source=source,
            reason=reason, created_at=previous.created_at if previous else time.time(),
        )
        overrides = bindings.binding_to_task_overrides(selected, target)
    keys = tuple(dict.fromkeys(key for key in (*task_ids, session_key) if key))
    with store_lock(path):
        ready = inspect_switch(session_key, path=path)
        if ready and ready["phase"] == "ready" and ready["backend"] == backend:
            current = committed_overrides(session_key, targets=targets, path=path)
            current_config = {key: value for key, value in current.items() if not key.startswith("_ssh_switch_")}
            if current_config == (overrides or {"env_type": "local"}):
                return previous
    operation = begin_switch(session_key, backend, path=path, recover_pending=True)
    try:
        from gateway.ssh_switch_env import projection_update
        with projection_update():
            for key in keys:
                if overrides is None:
                    terminal_tool.clear_task_env_overrides(key)
                else:
                    terminal_tool.register_task_env_overrides(key, {
                        **overrides, "_ssh_switch_scope": session_key,
                        "_ssh_switch_revision": operation["revision"],
                    })
        commit_switch(session_key, operation["operation"], selected, path=path)
    except Exception as exc:
        logger.warning("SSH backend selection incomplete", exc_info=_failure_trace(exc))
        try:
            fail_switch(session_key, operation["operation"], path=path)
        except Exception as journal_exc:
            # The already-durable pending marker also denies execution after restart.
            logger.warning("SSH pending marker retained", exc_info=_failure_trace(journal_exc))
        raise SshStateUnavailable("SSH switch incomplete; new execution blocked. Retry explicit backend selection.") from None
    return selected
