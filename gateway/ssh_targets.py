"""SSH target discovery and rendering helpers for gateway /ssh commands.

Hermes owns its `/ssh` target registry.  The user's system OpenSSH config is
not loaded implicitly; it can be parsed only by explicit import flows.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
from typing import Any, Iterable

from hermes_constants import get_hermes_home


@dataclass(frozen=True)
class SshTarget:
    """A redaction-safe view of a Hermes-managed SSH target."""

    alias: str
    host: str | None = None
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    identities_only: bool | None = None
    known_hosts: str | None = None
    host_key_policy: str | None = None
    cwd: str | None = None
    source: str = "hermes"


def _coerce_port(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return None


def _coerce_target(alias: str, data: Any, *, source: str = "hermes") -> SshTarget | None:
    if not alias:
        return None
    if not isinstance(data, dict):
        return None
    host = data.get("host") or data.get("hostname")
    identity_file = data.get("identity_file") or data.get("identityfile") or data.get("key")
    known_hosts = data.get("known_hosts") or data.get("user_known_hosts_file")
    host_key_policy = data.get("host_key_policy") or data.get("strict_host_key_checking")
    return SshTarget(
        alias=str(alias),
        host=str(host) if host else None,
        user=str(data.get("user")) if data.get("user") else None,
        port=_coerce_port(data.get("port")),
        identity_file=str(identity_file) if identity_file else None,
        identities_only=_coerce_bool(data.get("identities_only") or data.get("identitiesonly")),
        known_hosts=str(known_hosts) if known_hosts else None,
        host_key_policy=str(host_key_policy) if host_key_policy else None,
        cwd=str(data.get("cwd") or data.get("remote_cwd")) if (data.get("cwd") or data.get("remote_cwd")) else None,
        source=source,
    )


def _targets_from_mapping(raw_targets: Any, *, source: str = "hermes") -> list[SshTarget]:
    targets: list[SshTarget] = []
    if isinstance(raw_targets, dict):
        for alias, data in raw_targets.items():
            target = _coerce_target(str(alias), data, source=source)
            if target is not None:
                targets.append(target)
    elif isinstance(raw_targets, list):
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            alias = item.get("alias") or item.get("name")
            target = _coerce_target(str(alias or ""), item, source=source)
            if target is not None:
                targets.append(target)
    return targets


def parse_hermes_ssh_targets(config_text: str, *, source: str = "hermes") -> list[SshTarget]:
    """Parse Hermes-managed SSH targets from YAML.

    Accepted shapes:

    ```yaml
    ssh:
      targets:
        alias:
          host: example.com
          user: rex
          port: 22
          identity_file: ~/.hermes/ssh/keys/alias
          identities_only: true
          known_hosts: ~/.hermes/ssh/known_hosts
          host_key_policy: strict
          cwd: /home/rex/Playground
    ```

    or a top-level `targets:` mapping/list for standalone target files.
    """

    try:
        import yaml
    except Exception:
        return []

    try:
        data = yaml.safe_load(config_text) or {}
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    ssh_section = data.get("ssh")
    if isinstance(ssh_section, dict) and "targets" in ssh_section:
        return _targets_from_mapping(ssh_section.get("targets"), source=source)
    if "targets" in data:
        return _targets_from_mapping(data.get("targets"), source=source)
    return []


def parse_system_ssh_config(config_text: str, *, source: str = "system-import") -> list[SshTarget]:
    """Parse a minimal subset of OpenSSH config for explicit import flows only.

    This is deliberately not used by `load_ssh_targets()`.  System SSH hosts
    must not automatically appear in Hermes `/ssh list`; they require an
    explicit import/copy step so Hermes owns the target registry.
    """

    targets: list[SshTarget] = []
    current_aliases: list[str] = []
    current: dict[str, str] = {}

    def flush() -> None:
        nonlocal current_aliases, current
        if not current_aliases:
            current = {}
            return
        for alias in current_aliases:
            if any(ch in alias for ch in "*?"):
                continue
            targets.append(
                SshTarget(
                    alias=alias,
                    host=current.get("hostname"),
                    user=current.get("user"),
                    port=_coerce_port(current.get("port")),
                    identity_file=current.get("identityfile"),
                    identities_only=_coerce_bool(current.get("identitiesonly")),
                    known_hosts=current.get("userknownhostsfile"),
                    host_key_policy=current.get("stricthostkeychecking"),
                    source=source,
                )
            )
        current_aliases = []
        current = {}

    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if not parts:
            continue
        key = parts[0].lower()
        value = " ".join(parts[1:]).strip()
        if key == "host":
            flush()
            current_aliases = parts[1:]
            current = {}
            continue
        if key in {
            "hostname",
            "user",
            "port",
            "identityfile",
            "identitiesonly",
            "userknownhostsfile",
            "stricthostkeychecking",
        } and current_aliases and value:
            current[key] = value
    flush()
    return targets


# Backward-compatible name for internal callers that imported the V0 helper
# during development.  Keep it explicit in docs/tests as system-import only.
parse_ssh_config = parse_system_ssh_config


def default_ssh_targets_path() -> Path:
    """Return the Hermes-owned SSH target registry path."""

    return get_hermes_home() / "ssh" / "targets.yaml"


def load_ssh_targets(config_path: str | Path | None = None) -> list[SshTarget]:
    """Load SSH targets from the Hermes-managed registry only.

    Missing config files are normal and yield an empty list.  This function does
    not read `~/.ssh/config` implicitly.
    """

    path = Path(config_path).expanduser() if config_path is not None else default_ssh_targets_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        return []
    return parse_hermes_ssh_targets(text, source="hermes")


def render_ssh_targets(targets: Iterable[SshTarget]) -> str:
    """Render targets for chat without leaking key paths."""

    target_list = list(targets)
    if not target_list:
        return (
            "No Hermes SSH targets configured. "
            "Add targets to the Hermes SSH registry, then run /ssh list again."
        )

    lines = ["SSH targets:"]
    for index, target in enumerate(target_list, start=1):
        lines.append("")
        lines.append(f"{index}. `{target.alias}`")
        if target.host:
            lines.append(f"   host: {target.host}")
        if target.user:
            lines.append(f"   user: {target.user}")
        if target.port:
            lines.append(f"   port: {target.port}")
        if target.cwd:
            lines.append(f"   cwd: {target.cwd}")
        if target.identity_file:
            lines.append("   identity: [REDACTED_PATH]")
        if target.source:
            lines.append(f"   source: {target.source}")
    return "\n".join(lines)


def find_ssh_target(targets: Iterable[SshTarget], alias: str) -> SshTarget | None:
    """Return the target with the given alias, if present."""

    wanted = alias.strip()
    if not wanted:
        return None
    for target in targets:
        if target.alias == wanted:
            return target
    return None


def validate_ssh_target_for_runtime(target: SshTarget) -> str | None:
    """Return a user-facing error if target is incomplete for SSH runtime."""

    missing = []
    if not target.host:
        missing.append("host")
    if not target.user:
        missing.append("user")
    if missing:
        return f"SSH target `{target.alias}` is incomplete: missing {', '.join(missing)}. No binding was changed."
    return None
