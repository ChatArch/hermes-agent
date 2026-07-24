"""Model-facing SSH mode control for gateway sessions.

This is intentionally a thin control surface over the existing gateway SSH
binding store. It lets the model inspect SSH state and request a session-scoped
switch only when the user has granted YOLO for the target.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
import threading

from gateway.session_context import get_session_env
from gateway.ssh_bindings import (
    add_ssh_yolo_alias,
    clear_ssh_binding,
    get_ssh_binding,
    get_ssh_yolo_grant,
    resolve_binding_task_overrides,
    set_ssh_binding,
)
from gateway.ssh_targets import find_ssh_target, load_ssh_targets, validate_ssh_target_for_runtime
from tools.registry import registry, tool_error, tool_result
from tools.terminal_tool import check_terminal_requirements


_AGENT_BINDING_SOURCES = {"agent-once", "agent-yolo"}
_SSH_GRANT_TIMEOUT_SECONDS = 300


@dataclass
class _SshGrantEntry:
    data: dict[str, Any]
    event: threading.Event
    result: str | None = None


_grant_lock = threading.RLock()
_gateway_grant_queues: dict[str, list[_SshGrantEntry]] = {}
_gateway_grant_notify_cbs: dict[str, Callable[[dict[str, Any]], None]] = {}


def register_gateway_ssh_grant_notify(session_key: str, cb) -> None:
    """Register a per-session callback for model-initiated SSH grants."""
    if not session_key:
        return
    with _grant_lock:
        _gateway_grant_notify_cbs[session_key] = cb


def unregister_gateway_ssh_grant_notify(session_key: str) -> None:
    """Unregister SSH grant callback and release any blocked waiters."""
    if not session_key:
        return
    with _grant_lock:
        _gateway_grant_notify_cbs.pop(session_key, None)
        entries = _gateway_grant_queues.pop(session_key, [])
    for entry in entries:
        entry.result = "deny"
        entry.event.set()


def resolve_gateway_ssh_grant(session_key: str, choice: str) -> int:
    """Resolve the oldest pending SSH grant request for *session_key*."""
    if not session_key:
        return 0
    clean = str(choice or "").strip().lower()
    if clean not in {"allow_current", "allow_all", "deny"}:
        clean = "deny"
    with _grant_lock:
        queue = _gateway_grant_queues.get(session_key)
        if not queue:
            return 0
        entry = queue.pop(0)
        if not queue:
            _gateway_grant_queues.pop(session_key, None)
    entry.result = clean
    entry.event.set()
    return 1


def _await_gateway_ssh_grant(session_key: str, data: dict[str, Any]) -> str | None:
    with _grant_lock:
        notify_cb = _gateway_grant_notify_cbs.get(session_key)
        if notify_cb is None:
            return None
        entry = _SshGrantEntry(data=data, event=threading.Event())
        _gateway_grant_queues.setdefault(session_key, []).append(entry)
    try:
        notify_cb(data)
    except Exception:
        with _grant_lock:
            queue = _gateway_grant_queues.get(session_key, [])
            if entry in queue:
                queue.remove(entry)
            if not queue:
                _gateway_grant_queues.pop(session_key, None)
        return None
    if not entry.event.wait(_SSH_GRANT_TIMEOUT_SECONDS):
        with _grant_lock:
            queue = _gateway_grant_queues.get(session_key, [])
            if entry in queue:
                queue.remove(entry)
            if not queue:
                _gateway_grant_queues.pop(session_key, None)
        return "timeout"
    return entry.result or "deny"


def _current_context(task_id: str | None = None) -> dict[str, str]:
    session_id = task_id or get_session_env("HERMES_SESSION_ID", "")
    return {
        "platform": get_session_env("HERMES_SESSION_PLATFORM", ""),
        "thread_id": get_session_env("HERMES_SESSION_THREAD_ID", ""),
        "session_key": get_session_env("HERMES_SESSION_KEY", ""),
        "session_id": session_id,
    }


def _target_summary(target) -> dict[str, Any]:
    return {
        "alias": target.alias,
        "host": target.host,
        "user": target.user,
        "port": target.port,
        "cwd": target.cwd,
        "identity": "[REDACTED_PATH]" if target.identity_file else None,
        "known_hosts": "[REDACTED_PATH]" if target.known_hosts else None,
        "host_key_policy": target.host_key_policy,
        "identities_only": target.identities_only,
    }


def _yolo_summary(session_key: str) -> dict[str, Any]:
    grant = get_ssh_yolo_grant(session_key)
    return {
        "enabled": grant.enabled,
        "aliases": list(grant.aliases),
        "allows_all": grant.allows_all,
    }


def _status(ctx: dict[str, str]) -> str:
    session_key = ctx["session_key"]
    if not session_key:
        return tool_error("ssh_mode requires a live gateway session")
    binding = get_ssh_binding(session_key)
    resolved = resolve_binding_task_overrides(session_key)
    return tool_result(
        ok=True,
        backend="ssh" if resolved else "local",
        session_key=session_key,
        platform=ctx["platform"],
        thread_id=ctx["thread_id"],
        binding={
            "alias": binding.alias,
            "cwd": binding.cwd,
            "source": binding.source,
            "reason": binding.reason,
        } if binding else None,
        yolo=_yolo_summary(session_key),
    )


def _list_targets() -> str:
    return tool_result(ok=True, targets=[_target_summary(target) for target in load_ssh_targets()])


# CHATARCH_LOCAL_SEAM: model-side SSH switching depends on the gateway grant
# callback below. If this code conflicts with upstream, preserve the path that
# sends a Feishu/Lark SSH authorization card before falling back to `/ssh yolo`.
def _request_use(ctx: dict[str, str], args: dict[str, Any], task_id: str | None) -> str:
    session_key = ctx["session_key"]
    if not session_key:
        return tool_error("ssh_mode.request_use requires a live gateway session")
    if ctx["platform"] == "feishu" and not ctx["thread_id"]:
        return tool_result(
            ok=False,
            approval_required=True,
            reason=(
                "Feishu parent chats cannot directly enter SSH mode from the model tool. "
                "Ask the user to run /ssh use <alias>; Hermes will create a Thread "
                "by default and bind SSH there."
            ),
        )

    alias = str(args.get("alias") or "").strip()
    if not alias:
        return tool_error("alias is required for ssh_mode.request_use")
    reason = str(args.get("reason") or "").strip()
    cwd = str(args.get("cwd") or "").strip() or None

    targets = load_ssh_targets()
    target = find_ssh_target(targets, alias)
    if target is None:
        return tool_error(f"Unknown SSH target: {alias}", known_targets=[t.alias for t in targets])
    target_error = validate_ssh_target_for_runtime(target)
    if target_error:
        return tool_error(target_error)

    grant = get_ssh_yolo_grant(session_key)
    if not grant.allows(alias):
        decision = _await_gateway_ssh_grant(
            session_key,
            {
                "kind": "ssh_grant",
                "session_key": session_key,
                "alias": alias,
                "reason": reason,
                "cwd": cwd,
            },
        )
        if decision == "allow_current":
            add_ssh_yolo_alias(session_key, alias)
        elif decision == "allow_all":
            add_ssh_yolo_alias(session_key, "all")
        elif decision in {"deny", "timeout"}:
            return tool_result(
                ok=False,
                approval_required=True,
                denied=True,
                alias=alias,
                reason="User denied SSH authorization." if decision == "deny" else "Timed out waiting for SSH authorization.",
                yolo=_yolo_summary(session_key),
            )
        else:
            return tool_result(
                ok=False,
                approval_required=True,
                alias=alias,
                reason=(
                    "This session has no YOLO grant for the requested SSH target. "
                    f"Ask the user to run /ssh yolo on {alias}, or /ssh yolo on all, inside this Thread."
                ),
                yolo=_yolo_summary(session_key),
            )
        grant = get_ssh_yolo_grant(session_key)

    binding = set_ssh_binding(
        session_key,
        alias=alias,
        cwd=cwd,
        source="agent-yolo",
        reason=reason or "model requested SSH mode under YOLO grant",
    )
    overrides = resolve_binding_task_overrides(session_key, targets=targets)
    if overrides:
        try:
            from tools.terminal_tool import register_task_env_overrides

            register_task_env_overrides(ctx["session_id"] or task_id or session_key, overrides)
        except Exception:
            pass
    return tool_result(
        ok=True,
        backend="ssh",
        alias=binding.alias,
        cwd=binding.cwd or target.cwd,
        source=binding.source,
        message="SSH mode enabled for this session. Subsequent terminal/file/execute_code calls in this turn should use the SSH backend.",
    )


def _request_local(ctx: dict[str, str], args: dict[str, Any], task_id: str | None) -> str:
    session_key = ctx["session_key"]
    if not session_key:
        return tool_error("ssh_mode.request_local requires a live gateway session")
    binding = get_ssh_binding(session_key)
    if binding is None:
        return tool_result(ok=True, backend="local", changed=False, message="No SSH binding is active.")
    if binding.source not in _AGENT_BINDING_SOURCES:
        return tool_result(
            ok=False,
            changed=False,
            protected=True,
            alias=binding.alias,
            source=binding.source,
            message="Current SSH binding was created by the user; use /ssh local to clear it (/ssh off is a compatibility alias).",
        )
    clear_ssh_binding(session_key)
    try:
        from tools.terminal_tool import clear_task_env_overrides

        clear_task_env_overrides(ctx["session_id"] or task_id or session_key)
    except Exception:
        pass
    return tool_result(
        ok=True,
        backend="local",
        changed=True,
        previous_alias=binding.alias,
        reason=str(args.get("reason") or "").strip() or None,
    )


def ssh_mode_tool(args: dict[str, Any], **kw) -> str:
    action = str(args.get("action") or "status").strip().lower()
    ctx = _current_context(kw.get("task_id"))
    if action == "status":
        return _status(ctx)
    if action == "list_targets":
        return _list_targets()
    if action == "request_use":
        return _request_use(ctx, args, kw.get("task_id"))
    if action == "request_local":
        return _request_local(ctx, args, kw.get("task_id"))
    return tool_error("unknown ssh_mode action", allowed_actions=["status", "list_targets", "request_use", "request_local"])


SSH_MODE_SCHEMA = {
    "name": "ssh_mode",
    "description": (
        "Inspect or request the current gateway session's SSH backend. Use this "
        "when the user asks you to check SSH state, list SSH targets, enter an "
        "SSH target, or return to local mode. Read-only status/list actions need "
        "no authorization. request_use switches only when this session has a "
        "YOLO grant for the target; otherwise it asks the gateway to prompt the "
        "user with an SSH authorization card when supported (allow current, allow all, deny). "
        "If no card flow is available it reports approval_required and the user "
        "should run /ssh yolo on <alias> or manually run /ssh use <alias> "
        "(in a Feishu parent chat, /ssh use <alias> creates a Thread by default). "
        "request_local is the model-facing equivalent of /ssh local, but may clear "
        "only model-created SSH bindings, never user-created sticky /ssh use bindings."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "list_targets", "request_use", "request_local"],
                "description": "Action to perform.",
            },
            "alias": {
                "type": "string",
                "description": "SSH target alias for request_use.",
            },
            "reason": {
                "type": "string",
                "description": "Short user-visible reason for switching or returning to local.",
            },
            "cwd": {
                "type": "string",
                "description": "Optional remote working directory override for request_use.",
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="ssh_mode",
    toolset="terminal",
    schema=SSH_MODE_SCHEMA,
    handler=ssh_mode_tool,
    check_fn=check_terminal_requirements,
    emoji="🔐",
    max_result_size_chars=20_000,
)
