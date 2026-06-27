"""Section-scoped SSH backend bindings for gateway `/ssh`.

Bindings are keyed by durable gateway ``session_key`` values (Feishu
Thread/Section lanes), not by short-lived transcript ``session_id`` values.
The binding store intentionally contains only target aliases and optional cwd;
connection details are resolved from the Hermes-managed SSH target registry at
runtime so key paths stay in one auditable place.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import time

from hermes_constants import get_hermes_home
from gateway.ssh_targets import find_ssh_target, load_ssh_targets, SshTarget


@dataclass(frozen=True)
class SshBinding:
    """A section binding from session_key to a Hermes-managed SSH target."""

    session_key: str
    alias: str
    cwd: str | None = None
    created_at: float | None = None
    updated_at: float | None = None


def default_ssh_bindings_path() -> Path:
    """Return the Hermes-owned SSH section binding store path."""

    return get_hermes_home() / "ssh" / "bindings.json"


def _read_store(path: str | Path | None = None) -> dict[str, Any]:
    store_path = Path(path).expanduser() if path is not None else default_ssh_bindings_path()
    try:
        data = json.loads(store_path.read_text(encoding="utf-8") or "{}")
    except FileNotFoundError:
        return {"bindings": {}}
    except Exception:
        return {"bindings": {}}
    if not isinstance(data, dict):
        return {"bindings": {}}
    bindings = data.get("bindings")
    if not isinstance(bindings, dict):
        data["bindings"] = {}
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
    return SshBinding(
        session_key=session_key,
        alias=alias,
        cwd=str(cwd).strip() if cwd else None,
        created_at=raw.get("created_at") if isinstance(raw.get("created_at"), (int, float)) else None,
        updated_at=raw.get("updated_at") if isinstance(raw.get("updated_at"), (int, float)) else None,
    )


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
    path: str | Path | None = None,
) -> SshBinding:
    """Persist a section SSH binding."""

    if not session_key:
        raise ValueError("session_key is required")
    alias = str(alias or "").strip()
    if not alias:
        raise ValueError("alias is required")
    data = _read_store(path)
    bindings = data.setdefault("bindings", {})
    now = time.time()
    existing = bindings.get(session_key) if isinstance(bindings.get(session_key), dict) else {}
    created_at = existing.get("created_at") if isinstance(existing.get("created_at"), (int, float)) else now
    record: dict[str, Any] = {
        "alias": alias,
        "created_at": created_at,
        "updated_at": now,
    }
    if cwd:
        record["cwd"] = str(cwd).strip()
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
