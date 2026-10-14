"""Payload-free traceback diagnostics at plugin fallback boundaries."""

from __future__ import annotations

from types import TracebackType


def private_exc_info(
    exc: Exception,
) -> tuple[type[RuntimeError], RuntimeError, TracebackType | None] | None:
    """Keep the catching boundary's frames for standard logging, not plugin source or messages.

    Call this inside the catching boundary: its frame heads the real exception chain.
    A fresh exception removes payload-bearing messages/causes. Filtering by that code
    object also excludes third-party source literals. Kept frames still hold locals;
    this is not a guarantee for custom formatters that serialize frame locals.
    Return None when audit policy rejects frame access; diagnostics must not replace
    the original business fallback, and logging itself may request the denied metadata.
    """
    try:
        # Bypass exception-subclass hooks without requesting a new caller frame.
        current = BaseException.__traceback__.__get__(exc)
        boundary = current.tb_frame.f_code if current is not None else None
        frames = []
        while current is not None:
            if current.tb_frame.f_code is boundary:
                frames.append(current)
            current = current.tb_next
        traceback = None
        for frame in reversed(frames):
            traceback = TracebackType(traceback, frame.tb_frame, frame.tb_lasti, frame.tb_lineno)
        return RuntimeError, RuntimeError("Plugin fallback failed"), traceback
    except (RuntimeError, PermissionError):
        return None
