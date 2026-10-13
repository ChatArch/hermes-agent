"""Best-effort, payload-free logging for business fallback boundaries."""

from __future__ import annotations

import logging


class PrivateDiagnosticLogger:
    """Opt-in diagnostic sink; ``exc_info`` is the caught business exception.

    Only standard Formatter privacy is promised, not custom rendering of locals.
    Handler, helper and optional diagnostic imports cannot replace the fallback.
    This is not a general Logger replacement or a fallback/result policy.
    """

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def warning(self, message: str, *, exc_info: Exception) -> None:
        """Log a fixed message without exposing the active private exception chain."""
        try:
            from hermes_cli.plugin_host_diagnostics import private_exc_info

            info = private_exc_info(exc_info)
            if info is None:
                return
            # Standard StreamHandler.handleError prints the active exception
            # chain, independent of record.exc_info. Keep the original untouched.
            try:
                raise RuntimeError("Private diagnostic context")
            except RuntimeError as context:
                context.__context__ = None
                context.__cause__ = None
                self._logger.warning(message, exc_info=info)
        except Exception:  # health: allow BLE001 S110 -- This diagnostic-only sink must retain its caller's business fallback when import/metadata/logging fails; another log would recurse into the same broken sink.
            return
