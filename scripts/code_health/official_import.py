"""Account for verified, byte-identical upstream structural debt.

Non-structural findings always block. A conflict-resolved or fork-edited file keeps
its normal ratchet verdict. Findings against the official tip are also blocking.
"""

from __future__ import annotations

from collections.abc import Callable

from scripts.code_health.config import TARGETS
from scripts.code_health.model import Finding


def reconcile_official_import(
    fork_findings: list[Finding], upstream_findings: list[Finding],
    *, same_blob: Callable[[str], bool], tip: str,
) -> list[Finding]:
    """Annotate exact upstream structure; never reduce a safety or fork-side finding."""
    combined: list[Finding] = []
    by_key: dict[tuple[str, str, str, int], Finding] = {}
    for finding in fork_findings:
        if finding.rule in TARGETS and same_blob(finding.path) and finding.allowed_reason is None:
            finding.blocking = False
            finding.detail += f" [official import {tip[:12]}: identical file blob]"
        key = (finding.path, finding.rule, finding.scope, finding.line)
        by_key[key] = finding
        combined.append(finding)
    for finding in upstream_findings:
        key = (finding.path, finding.rule, finding.scope, finding.line)
        existing = by_key.get(key)
        if existing is not None:
            existing.blocking = existing.blocking or finding.blocking
            continue
        finding.detail += " [fork vs official parent]"
        by_key[key] = finding
        combined.append(finding)
    return combined
