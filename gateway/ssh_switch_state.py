"""Durable, session-scoped SSH transition and routing revision checks."""

from __future__ import annotations

import time
import uuid
import hashlib
import json
from pathlib import Path

from gateway.ssh_switch_store import SshStateUnavailable, state_path, store_lock


_SCOPE = "_ssh_switch_scope"
_REVISION = "_ssh_switch_revision"


def _records(data):
    records = data.setdefault("ssh_switches", {})
    if not isinstance(records, dict):
        raise SshStateUnavailable("SSH transition state invalid; execution blocked")
    return records


def inspect_switch(session_key: str, *, path=None):
    from gateway.ssh_bindings import _read_store
    record = _records(_read_store(path)).get(session_key)
    if record is None:
        return None
    if not isinstance(record, dict) or record.get("phase") not in {"pending", "failed", "ready"}:
        raise SshStateUnavailable("SSH transition unknown; execution blocked")
    if not isinstance(record.get("revision"), int) or not isinstance(record.get("operation"), str):
        raise SshStateUnavailable("SSH transition revision invalid; execution blocked")
    return record


def begin_switch(session_key: str, backend: str, *, path=None, recover_pending=False):
    from gateway.ssh_bindings import _read_store, _write_store, get_ssh_binding
    if not session_key:
        raise ValueError("session_key is required")
    with store_lock(path) as target:
        data = _read_store(target)
        records = _records(data)
        old = inspect_switch(session_key, path=target)
        if old and old["phase"] == "pending" and not recover_pending:
            raise SshStateUnavailable("SSH switch already pending; execution blocked")
        previous = get_ssh_binding(session_key, path=target)
        record = {"phase": "pending", "backend": backend, "operation": uuid.uuid4().hex,
                  "revision": (old["revision"] if old else 0) + 1,
                  "previous_backend": previous.alias if previous else "local"}
        records[session_key] = record
        _write_store(data, target)
        return dict(record)


def fail_switch(session_key: str, operation: str, *, path=None):
    from gateway.ssh_bindings import _read_store, _write_store
    with store_lock(path) as target:
        data = _read_store(target)
        record = _records(data).get(session_key)
        if not isinstance(record, dict) or record.get("operation") != operation:
            raise SshStateUnavailable("SSH operation superseded; execution blocked")
        if record.get("phase") == "ready":
            return
        record["phase"] = "failed"
        _write_store(data, target)


def commit_switch(session_key: str, operation: str, binding, *, path=None):
    from gateway.ssh_bindings import _read_store, _write_store
    with store_lock(path) as target:
        data = _read_store(target)
        record = _records(data).get(session_key)
        if not isinstance(record, dict) or record.get("operation") != operation or record.get("phase") != "pending":
            raise SshStateUnavailable("SSH operation no longer pending; execution blocked")
        if binding is None:
            data["bindings"].pop(session_key, None)
        else:
            data["bindings"][session_key] = {
                "alias": binding.alias, "cwd": binding.cwd, "source": binding.source,
                "reason": binding.reason, "created_at": binding.created_at,
                "updated_at": time.time(),
            }
        record["phase"] = "ready"
        _write_store(data, target)


def committed_overrides(session_key: str, *, targets=None, path=None):
    from gateway.ssh_bindings import binding_to_task_overrides, get_ssh_binding, load_ssh_targets
    from gateway.ssh_targets import find_ssh_target, validate_ssh_target_for_runtime
    record = inspect_switch(session_key, path=path)
    if record is None:
        return None
    if record["phase"] != "ready":
        raise SshStateUnavailable("SSH switch incomplete; new execution blocked")
    if record["backend"] == "local":
        overrides = {"env_type": "local"}
    else:
        binding = get_ssh_binding(session_key, path=path)
        if binding is None or binding.alias != record["backend"]:
            raise SshStateUnavailable("SSH selection inconsistent; execution blocked")
        target = find_ssh_target(targets if targets is not None else load_ssh_targets(), binding.alias)
        if target is None or validate_ssh_target_for_runtime(target):
            raise SshStateUnavailable("SSH target unavailable; execution blocked")
        overrides = binding_to_task_overrides(binding, target)
    return {**overrides, _SCOPE: session_key, _REVISION: record["revision"],
            "_ssh_switch_operation": record["operation"]}


def snapshot_from_overrides(overrides):
    scope = overrides.get(_SCOPE)
    if not scope:
        return None
    config = {key: value for key, value in overrides.items() if not key.startswith("_ssh_switch_")}
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return str(state_path()), scope, overrides["_ssh_switch_operation"], overrides[_REVISION], digest


def routing_snapshot(task_id=None):
    from tools.terminal_tool import resolve_task_overrides
    overrides = resolve_task_overrides(task_id)
    return snapshot_from_overrides(overrides)


def runtime_config_snapshot(task_id, overrides, base_config):
    """All managed constructors share effective cwd; unrelated backends stay unchanged."""
    from tools.terminal_tool import apply_task_env_overrides, get_session_cwd
    from tools.terminal_tool_config import coerce_ssh_remote_cwd
    config = apply_task_env_overrides(base_config, overrides)
    snapshot = snapshot_from_overrides(overrides)
    if snapshot is not None:
        config["cwd"] = coerce_ssh_remote_cwd(get_session_cwd(snapshot[1]) or config["cwd"], config["env_type"])
        snapshot = (*snapshot, config["cwd"], task_id)
    return config, snapshot


def record_admitted_cwd(snapshot, session_key, cwd):
    """A completed old command keeps its result but cannot write into a new route."""
    from tools.terminal_tool import record_session_cwd
    if snapshot is None:
        record_session_cwd(session_key, cwd)
        return True
    try:
        with store_lock(snapshot[0]):
            assert_snapshot(snapshot[:5])
            record_session_cwd(snapshot[1], cwd)
            return True
    except (SshStateUnavailable, OSError):
        return False


def assert_snapshot(snapshot):
    if snapshot is None:
        return
    path, scope, operation, revision, digest = snapshot[:5]
    record = inspect_switch(scope, path=Path(path))
    if record is None or record["phase"] != "ready" or record["operation"] != operation or record["revision"] != revision:
        raise SshStateUnavailable("SSH routing revision changed; execution blocked")
    current = committed_overrides(scope, path=Path(path))
    if snapshot_from_overrides(current)[-1] != digest:
        raise SshStateUnavailable("SSH target configuration changed; execution blocked")
    if len(snapshot) == 7:
        from tools.terminal_tool import _get_env_config
        config, _stamp = runtime_config_snapshot(snapshot[6], current, _get_env_config())
        if config["cwd"] != snapshot[5]:
            raise SshStateUnavailable("SSH cwd configuration changed; execution blocked")


def route_from_context(task_id, stored_overrides):
    from gateway.session_context import get_session_env
    session = get_session_env("HERMES_SESSION_KEY", "")
    session_id = get_session_env("HERMES_SESSION_ID", "")
    direct = stored_overrides or {}
    if direct.get("env_type") and _SCOPE not in direct and task_id not in (session, session_id):
        return None  # A separate CLI/RL/ACP/delegated rollout owns its override.
    scope = direct.get(_SCOPE) or session
    return committed_overrides(scope) if scope else None
