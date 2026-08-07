"""Model-facing SSH/backend mode control for gateway sessions.

The model sees the same first-level backend verbs as the `/ssh` gateway command:
status, list, test, use, on, off. Permission toggles are user-owned; when the
model tries to switch to an off backend, the tool asks the gateway to request
approval instead of switching silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
import threading

from gateway.session_context import get_session_env
from gateway.ssh_bindings import (
    LOCAL_BACKEND,
    clear_ssh_binding,
    get_backend_auto_policy,
    get_ssh_binding,
    is_local_backend,
    list_backend_auto_policies,
    normalize_backend_name,
    resolve_binding_task_overrides,
    set_backend_auto_enabled,
    set_ssh_binding,
)
from gateway.ssh_targets import find_ssh_target, load_ssh_targets, validate_ssh_target_for_runtime
from tools.registry import registry, tool_error, tool_result
from tools.terminal_tool import check_terminal_requirements

_BACKEND_GRANT_TIMEOUT_SECONDS = 300


@dataclass
class _BackendGrantEntry:
    data: dict[str, Any]
    event: threading.Event
    result: str | None = None


_grant_lock = threading.RLock()
_gateway_grant_queues: dict[str, list[_BackendGrantEntry]] = {}
_gateway_grant_notify_cbs: dict[str, Callable[[dict[str, Any]], None]] = {}


def register_gateway_ssh_grant_notify(session_key: str, cb) -> None:
    """Register a per-session callback for model-initiated backend grants."""
    if not session_key:
        return
    with _grant_lock:
        _gateway_grant_notify_cbs[session_key] = cb


def unregister_gateway_ssh_grant_notify(session_key: str) -> None:
    """Unregister backend grant callback and release any blocked waiters."""
    if not session_key:
        return
    with _grant_lock:
        _gateway_grant_notify_cbs.pop(session_key, None)
        entries = _gateway_grant_queues.pop(session_key, [])
    for entry in entries:
        entry.result = "deny"
        entry.event.set()


def resolve_gateway_ssh_grant(session_key: str, choice: str) -> int:
    """Resolve the oldest pending backend grant request for *session_key*."""
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


def _await_gateway_backend_grant(session_key: str, data: dict[str, Any]) -> str | None:
    with _grant_lock:
        notify_cb = _gateway_grant_notify_cbs.get(session_key)
        if notify_cb is None:
            return None
        entry = _BackendGrantEntry(data=data, event=threading.Event())
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
    if not entry.event.wait(_BACKEND_GRANT_TIMEOUT_SECONDS):
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


def _target_summary(target, *, session_key: str = "", current: bool = False) -> dict[str, Any]:
    return {
        "backend": target.alias,
        "type": "ssh",
        "current": current,
        "auto_switch": get_backend_auto_policy(session_key, target.alias).enabled if session_key else False,
        "host": target.host,
        "user": target.user,
        "port": target.port,
        "cwd": target.cwd,
        "identity": "[REDACTED_PATH]" if target.identity_file else None,
        "known_hosts": "[REDACTED_PATH]" if target.known_hosts else None,
        "host_key_policy": target.host_key_policy,
        "identities_only": target.identities_only,
    }


def _known_backend_names() -> list[str]:
    return [LOCAL_BACKEND, *[target.alias for target in load_ssh_targets()]]


def _current_backend(session_key: str) -> str:
    binding = get_ssh_binding(session_key)
    return binding.alias if binding else LOCAL_BACKEND


def _status(ctx: dict[str, str]) -> str:
    session_key = ctx["session_key"]
    if not session_key:
        return tool_error("ssh_mode requires a live gateway session")
    targets = load_ssh_targets()
    binding = get_ssh_binding(session_key)
    resolved = resolve_binding_task_overrides(session_key, targets=targets)
    current = binding.alias if binding else LOCAL_BACKEND
    backends = [
        {
            "backend": LOCAL_BACKEND,
            "type": "local",
            "current": current == LOCAL_BACKEND,
            "auto_switch": get_backend_auto_policy(session_key, LOCAL_BACKEND).enabled,
        }
    ]
    backends.extend(_target_summary(target, session_key=session_key, current=current == target.alias) for target in targets)
    return tool_result(
        ok=True,
        backend="ssh" if resolved else LOCAL_BACKEND,
        current_backend=current,
        session_key=session_key,
        platform=ctx["platform"],
        thread_id=ctx["thread_id"],
        binding={
            "alias": binding.alias,
            "cwd": binding.cwd,
            "source": binding.source,
            "reason": binding.reason,
        } if binding else None,
        backends=backends,
        auto_switch=list_backend_auto_policies(session_key, [item["backend"] for item in backends]),
    )


def _list(ctx: dict[str, str]) -> str:
    session_key = ctx["session_key"]
    current = _current_backend(session_key) if session_key else LOCAL_BACKEND
    targets = load_ssh_targets()
    backends: list[dict[str, Any]] = [
        {
            "backend": LOCAL_BACKEND,
            "type": "local",
            "current": current == LOCAL_BACKEND,
            "auto_switch": get_backend_auto_policy(session_key, LOCAL_BACKEND).enabled if session_key else True,
        }
    ]
    backends.extend(_target_summary(target, session_key=session_key, current=current == target.alias) for target in targets)
    return tool_result(ok=True, backends=backends)


def _test_backend(ctx: dict[str, str], args: dict[str, Any]) -> str:
    backend = normalize_backend_name(str(args.get("backend") or args.get("alias") or ""))
    if not backend:
        return tool_error("backend is required for ssh_mode.test")
    if is_local_backend(backend):
        return tool_result(ok=True, backend=LOCAL_BACKEND, type="local", message="local backend is available")
    target = find_ssh_target(load_ssh_targets(), backend)
    if target is None:
        return tool_error(f"Unknown backend: {backend}", known_backends=_known_backend_names())
    target_error = validate_ssh_target_for_runtime(target)
    if target_error:
        return tool_result(ok=False, backend=backend, error=target_error)
    return tool_result(ok=True, backend=backend, type="ssh", target=_target_summary(target))


def _switch_to_backend(ctx: dict[str, str], backend: str, args: dict[str, Any], *, source: str) -> str:
    session_key = ctx["session_key"]
    if is_local_backend(backend):
        previous = get_ssh_binding(session_key)
        clear_ssh_binding(session_key)
        try:
            from tools.terminal_tool import clear_task_env_overrides

            clear_task_env_overrides(ctx["session_id"] or session_key)
            clear_task_env_overrides(session_key)
        except Exception:
            pass
        return tool_result(
            ok=True,
            backend=LOCAL_BACKEND,
            current_backend=LOCAL_BACKEND,
            changed=previous is not None,
            previous_alias=previous.alias if previous else None,
            source=source,
            reason=str(args.get("reason") or "").strip() or None,
        )

    targets = load_ssh_targets()
    target = find_ssh_target(targets, backend)
    if target is None:
        return tool_error(f"Unknown backend: {backend}", known_backends=_known_backend_names())
    target_error = validate_ssh_target_for_runtime(target)
    if target_error:
        return tool_error(target_error)
    cwd = str(args.get("cwd") or "").strip() or None
    reason = str(args.get("reason") or "").strip() or None
    binding = set_ssh_binding(session_key, alias=backend, cwd=cwd, source=source, reason=reason)
    overrides = resolve_binding_task_overrides(session_key, targets=targets)
    if overrides:
        try:
            from tools.terminal_tool import register_task_env_overrides

            register_task_env_overrides(ctx["session_id"] or session_key, overrides)
            if session_key and session_key != (ctx["session_id"] or session_key):
                register_task_env_overrides(session_key, overrides)
        except Exception:
            pass
    return tool_result(
        ok=True,
        backend="ssh",
        current_backend=binding.alias,
        alias=binding.alias,
        cwd=binding.cwd or target.cwd,
        source=binding.source,
        message="Backend switched for this session. Subsequent terminal/file/execute_code calls should use the selected backend.",
    )


# CHATARCH_LOCAL_SEAM: model-side backend switching depends on the gateway grant
# callback below. Preserve the path that sends a Feishu/Lark authorization card
# before falling back to typed /ssh guidance.
def _use(ctx: dict[str, str], args: dict[str, Any], task_id: str | None) -> str:
    session_key = ctx["session_key"]
    if not session_key:
        return tool_error("ssh_mode.use requires a live gateway session")
    backend = normalize_backend_name(str(args.get("backend") or args.get("alias") or ""))
    if not backend:
        return tool_error("backend is required for ssh_mode.use")

    if ctx["platform"] == "feishu" and not ctx["thread_id"] and not is_local_backend(backend):
        return tool_result(
            ok=False,
            approval_required=True,
            backend=backend,
            reason=(
                "Feishu parent chats cannot directly enter an SSH backend from the model tool. "
                "Ask the user to run /ssh use <backend>; Hermes will create a Thread by default when needed."
            ),
        )

    if not is_local_backend(backend) and find_ssh_target(load_ssh_targets(), backend) is None:
        return tool_error(f"Unknown backend: {backend}", known_backends=_known_backend_names())

    policy = get_backend_auto_policy(session_key, backend)
    if not policy.enabled:
        decision = _await_gateway_backend_grant(
            session_key,
            {
                "kind": "backend_grant",
                "session_key": session_key,
                "backend": backend,
                "alias": backend,
                "reason": str(args.get("reason") or "").strip(),
                "cwd": str(args.get("cwd") or "").strip() or None,
            },
        )
        if decision == "allow_all":
            set_backend_auto_enabled(session_key, backend, True)
        elif decision == "allow_current":
            pass
        elif decision in {"deny", "timeout"}:
            return tool_result(
                ok=False,
                approval_required=True,
                denied=True,
                backend=backend,
                reason="User denied backend switch." if decision == "deny" else "Timed out waiting for backend authorization.",
                auto_switch=get_backend_auto_policy(session_key, backend).enabled,
            )
        else:
            return tool_result(
                ok=False,
                approval_required=True,
                backend=backend,
                reason=(
                    f"Backend `{backend}` is off for model-initiated switching. "
                    f"Ask the user to run /ssh on {backend} or /ssh use {backend}."
                ),
                auto_switch=policy.enabled,
            )

    return _switch_to_backend(ctx, backend, args, source="agent-auto" if get_backend_auto_policy(session_key, backend).enabled else "agent-approved")


def _user_owned_policy_change(ctx: dict[str, str], args: dict[str, Any], enabled: bool) -> str:
    backend = normalize_backend_name(str(args.get("backend") or args.get("alias") or ""))
    if not backend:
        return tool_error("backend is required")
    return tool_result(
        ok=False,
        approval_required=True,
        backend=backend,
        message=(
            f"Only the user can run /ssh {'on' if enabled else 'off'} {backend}. "
            "The model cannot grant or revoke its own backend switching permission."
        ),
    )


def ssh_mode_tool(args: dict[str, Any], **kw) -> str:
    action = str(args.get("action") or "status").strip().lower()
    ctx = _current_context(kw.get("task_id"))
    if action == "status":
        return _status(ctx)
    if action == "list":
        return _list(ctx)
    if action == "test":
        return _test_backend(ctx, args)
    if action == "use":
        return _use(ctx, args, kw.get("task_id"))
    if action == "on":
        return _user_owned_policy_change(ctx, args, True)
    if action == "off":
        return _user_owned_policy_change(ctx, args, False)
    return tool_error("unknown ssh_mode action", allowed_actions=["status", "list", "test", "use", "on", "off"])


SSH_MODE_SCHEMA = {
    "name": "ssh_mode",
    "description": (
        "Inspect or request the current gateway session's execution backend. "
        "Backends are 'local' plus configured SSH targets. Actions mirror the "
        "gateway /ssh command: status, list, test, use, on, off. The model may "
        "request use <backend>; if that backend is off for auto-switching, Hermes "
        "requests user approval instead of switching silently. on/off are user-owned "
        "permission changes and the model cannot use them to grant itself access."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "list", "test", "use", "on", "off"],
                "description": "Action to perform.",
            },
            "backend": {
                "type": "string",
                "description": "Backend name: 'local' or a configured SSH target alias.",
            },
            "reason": {
                "type": "string",
                "description": "Short user-visible reason for switching backends.",
            },
            "cwd": {
                "type": "string",
                "description": "Optional remote working directory override for SSH backend use.",
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
