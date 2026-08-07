"""Section-scoped backend bindings for gateway `/ssh`.

SSH Mode treats ``local`` and configured SSH targets as peer backends for
model-initiated switching. The current execution backend remains usable even
when its auto-switch policy is off; ``off`` means the model must request
approval before switching to that backend again.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import time

from hermes_constants import get_hermes_home
from gateway.ssh_targets import find_ssh_target, load_ssh_targets, SshTarget

LOCAL_BACKEND = "local"


@dataclass(frozen=True)
class SshBinding:
    """A section binding from session_key to a Hermes-managed SSH target."""

    session_key: str
    alias: str
    cwd: str | None = None
    source: str = "user"
    reason: str | None = None
    created_at: float | None = None
    updated_at: float | None = None


@dataclass(frozen=True)
class BackendAutoPolicy:
    """Session-scoped model auto-switch policy for one backend."""

    session_key: str
    backend: str
    enabled: bool
    created_at: float | None = None
    updated_at: float | None = None

    @property
    def local_enabled(self) -> bool:
        """Compatibility alias for PR #44's local-only policy object."""

        return self.enabled


@dataclass(frozen=True)
class BackendPolicyUpdate:
    """Result for a backend auto-switch policy mutation."""

    ok: bool
    policy: BackendAutoPolicy
    backend: str
    enabled: bool
    reason: str | None = None
    message: str | None = None


def normalize_backend_name(backend: str) -> str:
    """Return the canonical backend name used by SSH Mode policy."""

    clean = str(backend or "").strip()
    return LOCAL_BACKEND if clean.lower() == LOCAL_BACKEND else clean


def is_local_backend(backend: str) -> bool:
    return normalize_backend_name(backend).lower() == LOCAL_BACKEND


def default_ssh_bindings_path() -> Path:
    """Return the Hermes-owned SSH section binding store path."""

    return get_hermes_home() / "ssh" / "bindings.json"


def _empty_store() -> dict[str, Any]:
    return {"bindings": {}, "backend_policy": {}}


def _read_store(path: str | Path | None = None) -> dict[str, Any]:
    store_path = Path(path).expanduser() if path is not None else default_ssh_bindings_path()
    try:
        data = json.loads(store_path.read_text(encoding="utf-8") or "{}")
    except FileNotFoundError:
        return _empty_store()
    except Exception:
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    bindings = data.get("bindings")
    if not isinstance(bindings, dict):
        data["bindings"] = {}
    policy = data.get("backend_policy")
    if not isinstance(policy, dict):
        data["backend_policy"] = {}

    # Narrow migration for the PR #44 local-only policy shape.  This keeps
    # existing user state readable while the user-facing SSH Mode interface no
    # longer exposes the old two-level commands.
    legacy_local_policy = data.get("destination_policy")
    if isinstance(legacy_local_policy, dict):
        policies = data.setdefault("backend_policy", {})
        for session_key, raw in legacy_local_policy.items():
            if not isinstance(raw, dict):
                continue
            session_policy = policies.setdefault(str(session_key), {})
            if not isinstance(session_policy, dict):
                session_policy = {}
                policies[str(session_key)] = session_policy
            session_policy.setdefault(
                LOCAL_BACKEND,
                {
                    "enabled": bool(raw.get("local_enabled", True)),
                    "created_at": raw.get("created_at"),
                    "updated_at": raw.get("updated_at"),
                },
            )
    return data


def _write_store(data: dict[str, Any], path: str | Path | None = None) -> None:
    store_path = Path(path).expanduser() if path is not None else default_ssh_bindings_path()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = store_path.with_suffix(store_path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(store_path)
    try:
        store_path.chmod(0o600)
    except OSError:
        pass


def _coerce_binding(session_key: str, raw: Any) -> SshBinding | None:
    if not session_key or not isinstance(raw, dict):
        return None
    alias = str(raw.get("alias") or "").strip()
    if not alias:
        return None
    cwd = raw.get("cwd")
    source = str(raw.get("source") or "user").strip() or "user"
    reason = raw.get("reason")
    return SshBinding(
        session_key=session_key,
        alias=alias,
        cwd=str(cwd).strip() if cwd else None,
        source=source,
        reason=str(reason).strip() if reason else None,
        created_at=raw.get("created_at") if isinstance(raw.get("created_at"), (int, float)) else None,
        updated_at=raw.get("updated_at") if isinstance(raw.get("updated_at"), (int, float)) else None,
    )


def _coerce_backend_policy(session_key: str, backend: str, raw: Any) -> BackendAutoPolicy:
    clean = normalize_backend_name(backend)
    default_enabled = is_local_backend(clean)
    if not session_key or not clean or not isinstance(raw, dict):
        return BackendAutoPolicy(session_key=session_key, backend=clean, enabled=default_enabled)
    return BackendAutoPolicy(
        session_key=session_key,
        backend=clean,
        enabled=bool(raw.get("enabled", default_enabled)),
        created_at=raw.get("created_at") if isinstance(raw.get("created_at"), (int, float)) else None,
        updated_at=raw.get("updated_at") if isinstance(raw.get("updated_at"), (int, float)) else None,
    )


def get_backend_auto_policy(
    session_key: str,
    backend: str,
    *,
    path: str | Path | None = None,
) -> BackendAutoPolicy:
    """Return whether the model may auto-switch to *backend*.

    ``local`` defaults to on for historical behavior. SSH targets default to off
    and require either ``/ssh on <backend>`` or an approval request. The current
    backend is still usable when its policy is off.
    """

    clean = normalize_backend_name(backend)
    data = _read_store(path)
    policies = data.get("backend_policy")
    raw = None
    if isinstance(policies, dict):
        session_policy = policies.get(session_key)
        if isinstance(session_policy, dict):
            raw = session_policy.get(clean)
    return _coerce_backend_policy(session_key, clean, raw)


def list_backend_auto_policies(
    session_key: str,
    backends: list[str] | tuple[str, ...],
    *,
    path: str | Path | None = None,
) -> dict[str, bool]:
    """Return auto-switch state for the requested backends."""

    return {
        normalize_backend_name(item): get_backend_auto_policy(session_key, item, path=path).enabled
        for item in backends
    }


def set_backend_auto_enabled(
    session_key: str,
    backend: str,
    enabled: bool,
    *,
    path: str | Path | None = None,
) -> BackendPolicyUpdate:
    """Enable/disable model-initiated switching to *backend*.

    This does not clear or move the current backend. An off backend is not
    authorized for model entry; model-initiated ``use`` must request approval.
    """

    clean = normalize_backend_name(backend)
    if not session_key:
        policy = BackendAutoPolicy(session_key=session_key, backend=clean, enabled=is_local_backend(clean))
        return BackendPolicyUpdate(
            ok=False,
            policy=policy,
            backend=clean,
            enabled=bool(enabled),
            reason="session_key_required",
            message="session_key is required",
        )
    if not clean:
        policy = BackendAutoPolicy(session_key=session_key, backend=clean, enabled=False)
        return BackendPolicyUpdate(
            ok=False,
            policy=policy,
            backend=clean,
            enabled=bool(enabled),
            reason="backend_required",
            message="backend is required",
        )

    data = _read_store(path)
    policies = data.setdefault("backend_policy", {})
    session_policy = policies.setdefault(session_key, {})
    if not isinstance(session_policy, dict):
        session_policy = {}
        policies[session_key] = session_policy
    now = time.time()
    raw_existing = session_policy.get(clean) if isinstance(session_policy.get(clean), dict) else {}
    created_at = raw_existing.get("created_at") if isinstance(raw_existing.get("created_at"), (int, float)) else now
    record = {
        "enabled": bool(enabled),
        "created_at": created_at,
        "updated_at": now,
    }
    session_policy[clean] = record
    _write_store(data, path)
    policy = _coerce_backend_policy(session_key, clean, record)
    return BackendPolicyUpdate(ok=True, policy=policy, backend=clean, enabled=bool(enabled))


# Compatibility for existing code/tests that still refer to the PR #44 local-only
# names. New SSH Mode code should use get_backend_auto_policy / set_backend_auto_enabled.
SshDestinationPolicy = BackendAutoPolicy
DestinationPolicyUpdate = BackendPolicyUpdate


def get_destination_policy(session_key: str, *, path: str | Path | None = None) -> BackendAutoPolicy:
    return get_backend_auto_policy(session_key, LOCAL_BACKEND, path=path)


def set_destination_enabled(
    session_key: str,
    destination: str,
    enabled: bool,
    *,
    path: str | Path | None = None,
) -> BackendPolicyUpdate:
    return set_backend_auto_enabled(session_key, destination, enabled, path=path)


def get_ssh_binding(session_key: str, *, path: str | Path | None = None) -> SshBinding | None:
    """Return the SSH binding for *session_key*, if any."""

    if not session_key:
        return None
    data = _read_store(path)
    return _coerce_binding(session_key, data.get("bindings", {}).get(session_key))


def set_ssh_binding(
    session_key: str,
    *,
    alias: str,
    cwd: str | None = None,
    source: str = "user",
    reason: str | None = None,
    path: str | Path | None = None,
) -> SshBinding:
    """Persist a section SSH binding."""

    if not session_key:
        raise ValueError("session_key is required")
    alias = normalize_backend_name(alias)
    if not alias or is_local_backend(alias):
        raise ValueError("ssh binding alias must be a non-local SSH target")
    data = _read_store(path)
    bindings = data.setdefault("bindings", {})
    now = time.time()
    existing = bindings.get(session_key) if isinstance(bindings.get(session_key), dict) else {}
    created_at = existing.get("created_at") if isinstance(existing.get("created_at"), (int, float)) else now
    record: dict[str, Any] = {
        "alias": alias,
        "source": str(source or "user").strip() or "user",
        "created_at": created_at,
        "updated_at": now,
    }
    if cwd:
        record["cwd"] = str(cwd).strip()
    if reason:
        record["reason"] = str(reason).strip()
    bindings[session_key] = record
    _write_store(data, path)
    return _coerce_binding(session_key, record)  # type: ignore[return-value]


def clear_ssh_binding(session_key: str, *, path: str | Path | None = None) -> bool:
    """Remove a section SSH binding, returning True if one existed."""

    if not session_key:
        return False
    data = _read_store(path)
    bindings = data.setdefault("bindings", {})
    existed = session_key in bindings
    if existed:
        bindings.pop(session_key, None)
        _write_store(data, path)
    return existed


def resolve_binding_target(
    session_key: str,
    *,
    targets: list[SshTarget] | None = None,
    path: str | Path | None = None,
) -> tuple[SshBinding, SshTarget] | None:
    """Resolve a section binding to its current SSH target details."""

    binding = get_ssh_binding(session_key, path=path)
    if binding is None:
        return None
    target = find_ssh_target(targets if targets is not None else load_ssh_targets(), binding.alias)
    if target is None:
        return None
    return binding, target


def binding_to_task_overrides(binding: SshBinding, target: SshTarget) -> dict[str, Any]:
    """Convert a resolved binding into terminal/file/code task overrides."""

    overrides: dict[str, Any] = {
        "env_type": "ssh",
        "ssh_alias": binding.alias,
        "ssh_host": target.host or "",
        "ssh_user": target.user or "",
        "ssh_port": target.port or 22,
        "ssh_key": target.identity_file or "",
        "ssh_persistent": True,
    }
    if target.identity_file:
        overrides["ssh_key"] = target.identity_file
    if target.identities_only is not None:
        overrides["ssh_identities_only"] = target.identities_only
    if target.known_hosts:
        overrides["ssh_known_hosts"] = target.known_hosts
    if target.host_key_policy:
        overrides["ssh_host_key_policy"] = target.host_key_policy
    cwd = binding.cwd or target.cwd
    if cwd:
        overrides["cwd"] = cwd
    return overrides


def resolve_binding_task_overrides(
    session_key: str,
    *,
    targets: list[SshTarget] | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Return task env overrides for a session_key binding, or {}."""

    resolved = resolve_binding_target(session_key, targets=targets, path=path)
    if resolved is None:
        return {}
    binding, target = resolved
    return binding_to_task_overrides(binding, target)
