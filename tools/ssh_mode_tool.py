"""Model-facing SSH mode control for gateway sessions.

This is intentionally a thin control surface over the existing gateway SSH
binding store. It lets the model inspect SSH state and request a session-scoped
switch only when the user has granted YOLO for the target.
"""

from __future__ import annotations

from typing import Any

from gateway.session_context import get_session_env
from gateway.ssh_bindings import (
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


def _request_use(ctx: dict[str, str], args: dict[str, Any], task_id: str | None) -> str:
    session_key = ctx["session_key"]
    if not session_key:
        return tool_error("ssh_mode.request_use requires a live gateway session")
    if ctx["platform"] == "feishu" and not ctx["thread_id"]:
        return tool_result(
            ok=False,
            approval_required=True,
            reason=(
                "Feishu MainThread/root chat cannot directly enter SSH mode. "
                "Create a Thread first, for example with /ssh use <alias> -t."
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
            message="Current SSH binding was created by the user; use /ssh off or /ssh local to clear it.",
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
        "Inspect or request the current gateway session's SSH backend. "
        "Read-only status/list actions need no authorization. request_use only "
        "switches when this session has a YOLO grant for the target; otherwise "
        "it reports approval_required. request_local may clear only model-created "
        "SSH bindings, never user-created sticky /ssh use bindings."
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
