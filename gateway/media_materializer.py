"""Resolve outbound MEDIA resource references onto the gateway filesystem."""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from time import monotonic as _monotonic
from typing import Any
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

_ENV_UNSET = object()
_MAX_REMOTE_MEDIA_REFS = 10


@dataclass(frozen=True)
class ArtifactRef:
    scheme: str
    path: str
    authority: str | None = None


@dataclass(frozen=True)
class MediaMaterializationFailure:
    source_path: str
    error: str


@dataclass(frozen=True)
class MediaMaterializationResult:
    response: str
    materialized_paths: tuple[str, ...] = ()
    failures: tuple[MediaMaterializationFailure, ...] = ()


def _mask_json_resource_media(content: str) -> str:
    """Mask resource URI directives embedded inside serialized JSON values."""
    if '"' not in content or "MEDIA:" not in content:
        return content
    chars = list(content)
    for match in re.finditer(r'(?<=[:,{\[])\s*"((?:[^"\\\n]|\\.)*)"', content):
        if re.search(r"MEDIA:\s*(?:file|ssh)://", match.group(1), re.IGNORECASE):
            for index in range(match.start(1), match.end(1)):
                if chars[index] != "\n":
                    chars[index] = " "
    return "".join(chars)


def _clean_legacy_path(value: str) -> str:
    path = str(value or "").strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "`\"'":
        path = path[1:-1].strip()
    return path.lstrip("`\"'").rstrip("`\"',.;:)}]")


def _parse_resource(value: str, *, ssh_alias: str | None) -> ArtifactRef:
    raw = _clean_legacy_path(value)
    lowered = raw.lower()
    if lowered.startswith("file://"):
        parsed = urlsplit(raw)
        if parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port:
            raise ValueError("file URI must not contain credentials, port, query, or fragment")
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError("file URI authority must be empty or localhost")
        path = unquote(parsed.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        if not path:
            raise ValueError("file URI path is empty")
        return ArtifactRef(scheme="file", path=path)

    if lowered.startswith("ssh://"):
        parsed = urlsplit(raw)
        if parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port:
            raise ValueError("ssh URI must not contain credentials, port, query, or fragment")
        authority = parsed.netloc.strip()
        if not authority:
            raise ValueError("ssh URI requires a target alias")
        if not ssh_alias or authority.lower() != str(ssh_alias).strip().lower():
            raise PermissionError("ssh URI target does not match the current session binding")
        path = unquote(parsed.path)
        if not path.startswith("/"):
            raise ValueError("ssh URI requires an absolute remote path")
        return ArtifactRef(scheme="ssh", authority=authority, path=path)

    return ArtifactRef(scheme="legacy", path=raw, authority=ssh_alias)


def _cache_dir_for_suffix(suffix: str) -> Path:
    from gateway.platforms.base import (
        get_audio_cache_dir,
        get_document_cache_dir,
        get_image_cache_dir,
        get_video_cache_dir,
    )

    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg"}:
        return get_image_cache_dir()
    if suffix in {".mp3", ".wav", ".ogg", ".opus", ".m4a", ".flac"}:
        return get_audio_cache_dir()
    if suffix in {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}:
        return get_video_cache_dir()
    return get_document_cache_dir()


def _destination_for(ref: ArtifactRef, cache_dir: Path | None) -> Path:
    suffix = Path(ref.path).suffix.lower()
    target_dir = cache_dir if cache_dir is not None else _cache_dir_for_suffix(suffix)
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"remote_{uuid.uuid4().hex[:12]}{suffix}"


def _media_spans(response: str):
    from gateway.platforms.base import (
        BasePlatformAdapter,
        MEDIA_EXTENSIONLESS_TAG_RE,
        MEDIA_RESOURCE_URI_RE,
        MEDIA_TAG_CLEANUP_RE,
    )

    masked = BasePlatformAdapter._mask_protected_spans(response)
    masked = BasePlatformAdapter._mask_json_string_media(masked)
    masked = _mask_json_resource_media(masked)

    spans = []

    def _append_non_overlapping(pattern, group_name):
        for match in pattern.finditer(masked):
            start, end = match.span()
            if any(not (end <= seen_start or start >= seen_end) for seen_start, seen_end, _ in spans):
                continue
            spans.append((start, end, match.group(group_name)))

    # URI refs must win before the two legacy path patterns. The extensionless
    # matcher intentionally overlaps known-extension paths, so dedupe by span.
    _append_non_overlapping(MEDIA_RESOURCE_URI_RE, "uri")
    _append_non_overlapping(MEDIA_TAG_CLEANUP_RE, "path")
    _append_non_overlapping(MEDIA_EXTENSIONLESS_TAG_RE, "path")
    spans.sort(key=lambda item: item[0])
    return spans


def materialize_response_media(
    response: str,
    *,
    task_id: str,
    fallback_task_ids: tuple[str, ...] = (),
    ssh_alias: str | None = None,
    env: Any = _ENV_UNSET,
    cache_dir: str | Path | None = None,
    max_bytes: int | None = None,
    timeout: int = 60,
) -> MediaMaterializationResult:
    """Rewrite explicit file/SSH MEDIA references to gateway-local paths."""
    if not response or "MEDIA:" not in response:
        return MediaMaterializationResult(response=response)

    from gateway.platforms.base import (
        DEFAULT_INBOUND_MEDIA_MAX_BYTES,
        _media_delivery_recency_seconds,
        _media_delivery_strict_mode,
        validate_media_delivery_path,
    )

    if env is _ENV_UNSET:
        try:
            from tools.terminal_tool import get_active_env

            seen_task_ids: set[str] = set()
            for candidate_task_id in (task_id, *fallback_task_ids):
                candidate_task_id = str(candidate_task_id or "").strip()
                if not candidate_task_id or candidate_task_id in seen_task_ids:
                    continue
                seen_task_ids.add(candidate_task_id)
                candidate_env = get_active_env(candidate_task_id)
                if candidate_env is not None and getattr(
                    candidate_env, "supports_file_materialization", False
                ):
                    env = candidate_env
                    break
        except Exception:
            env = None

    effective_max = max_bytes
    if effective_max is None:
        try:
            from gateway.platforms.base import get_inbound_media_max_bytes

            effective_max = get_inbound_media_max_bytes()
        except Exception:
            effective_max = DEFAULT_INBOUND_MEDIA_MAX_BYTES
    if not effective_max or effective_max <= 0:
        effective_max = DEFAULT_INBOUND_MEDIA_MAX_BYTES

    target_cache = Path(cache_dir) if cache_dir is not None else None
    response_deadline = _monotonic() + max(1, int(timeout))
    replacements: list[tuple[int, int, str]] = []
    materialized: list[str] = []
    failures: list[MediaMaterializationFailure] = []
    resolved_refs: dict[ArtifactRef, str | Exception] = {}
    attempted_remote_refs: set[ArtifactRef] = set()
    materialized_bytes = 0

    for start, end, raw_value in _media_spans(response):
        source_for_error = _clean_legacy_path(raw_value)
        destination: Path | None = None
        try:
            ref = _parse_resource(raw_value, ssh_alias=ssh_alias)
            source_for_error = ref.path

            if ref.scheme == "file":
                local_path = validate_media_delivery_path(ref.path)
                if not local_path:
                    raise ValueError("file URI is not a safe gateway-local file")
                replacements.append((start, end, f"MEDIA:{local_path}"))
                continue

            if ref.scheme == "legacy":
                local_path = validate_media_delivery_path(ref.path)
                if local_path:
                    # Existing local MEDIA behavior remains byte-for-byte stable.
                    continue
                if env is None or not getattr(env, "supports_file_materialization", False):
                    continue
                ref = ArtifactRef(scheme="ssh", authority=ssh_alias, path=ref.path)

            if ref in resolved_refs:
                prior = resolved_refs[ref]
                if isinstance(prior, Exception):
                    raise prior
                replacements.append((start, end, f"MEDIA:{prior}"))
                continue

            if env is None or not getattr(env, "supports_file_materialization", False):
                raise RuntimeError("current SSH environment cannot materialize files")

            if len(attempted_remote_refs) >= _MAX_REMOTE_MEDIA_REFS:
                raise ValueError(
                    f"remote MEDIA response exceeds the {_MAX_REMOTE_MEDIA_REFS}-file limit"
                )
            attempted_remote_refs.add(ref)
            remaining_bytes = int(effective_max) - materialized_bytes
            if remaining_bytes <= 0:
                raise ValueError("remote MEDIA response exceeds the total transfer limit")
            remaining_seconds = response_deadline - _monotonic()
            if remaining_seconds <= 0:
                raise TimeoutError("remote MEDIA response deadline exceeded")

            destination = _destination_for(ref, target_cache)
            recent_window = (
                _media_delivery_recency_seconds()
                if _media_delivery_strict_mode()
                else None
            )
            metadata = env.materialize_file(
                ref.path,
                destination,
                max_bytes=remaining_bytes,
                timeout=max(1, ceil(remaining_seconds)),
                require_recent_seconds=recent_window,
            )
            # The destination is selected by the gateway. Never trust a remote
            # backend implementation to redirect validation to another host path.
            local_path = validate_media_delivery_path(str(destination))
            if not local_path:
                try:
                    destination.unlink()
                except OSError:
                    pass
                raise RuntimeError("materialized file failed gateway media validation")
            actual_size = destination.stat().st_size
            reported_size = int(metadata.get("size", actual_size))
            if reported_size != actual_size:
                raise RuntimeError("materialized file size metadata did not match the gateway file")
            resolved_refs[ref] = local_path
            materialized.append(local_path)
            materialized_bytes += actual_size
            replacements.append((start, end, f"MEDIA:{local_path}"))
        except Exception as exc:
            if destination is not None:
                try:
                    destination.unlink()
                except OSError:
                    pass
            try:
                parsed = _parse_resource(raw_value, ssh_alias=ssh_alias)
                resolved_refs[parsed] = exc
            except Exception:
                pass
            failures.append(
                MediaMaterializationFailure(
                    source_path=source_for_error,
                    error=str(exc),
                )
            )
            replacements.append((start, end, ""))
            logger.warning("Remote MEDIA materialization failed: %s", type(exc).__name__)

    if not replacements:
        return MediaMaterializationResult(response=response)

    rewritten = response
    for start, end, replacement in sorted(replacements, reverse=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    rewritten = re.sub(r"\n{3,}", "\n\n", rewritten).strip()
    return MediaMaterializationResult(
        response=rewritten,
        materialized_paths=tuple(materialized),
        failures=tuple(failures),
    )
