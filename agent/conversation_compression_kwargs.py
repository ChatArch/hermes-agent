"""Compression engine keyword negotiation, including explicit no-LLM rescue support."""

from __future__ import annotations

import inspect
from typing import Any, Optional


def _supported_compression_kwargs(
    compress_fn: Any, *, current_tokens: Optional[int], focus_topic: Optional[str], force: bool,
    memory_context: str, bypass_cooldown: bool = False, local_fallback_only: bool = False,
) -> dict:
    """Return only compression kwargs accepted by an engine callable.
    Inspecting first keeps older plugin signatures compatible without catching ``TypeError`` and running a
    stateful compressor twice."""
    candidates = {"current_tokens": current_tokens, "focus_topic": focus_topic, "force": force}
    if bypass_cooldown:
        candidates["bypass_cooldown"] = True
    if memory_context:
        candidates["memory_context"] = memory_context
    if local_fallback_only:
        candidates["local_fallback_only"] = True
    try:
        parameters = inspect.signature(compress_fn).parameters
    except (TypeError, ValueError):
        # current_tokens has always been in the ContextEngine ABC; use the oldest call
        # shape when the callable has no inspectable signature.
        return {"current_tokens": current_tokens}

    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts_kwargs:
        supported = dict(candidates)
        if "local_fallback_only" not in parameters:
            # A **kwargs plugin may silently ignore this flag and still call its
            # own summarizer. Require an explicit parameter for the no-LLM rescue path.
            supported.pop("local_fallback_only", None)
        return supported
    return {name: value for name, value in candidates.items() if name in parameters}