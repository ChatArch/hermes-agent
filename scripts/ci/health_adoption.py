"""One-time health-ratchet adoption for the verified official sync.

Keep the complete report. Subsequent PRs, unknown origins and tool errors block.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

_OFFICIAL_PARENT = "c57331677d7fc46298a7159cf3a650853f0966c5"
_SWITCH_FILE = "scripts/code_health/config.py"


def adoption_verdict(code: int, *, repository: str, head_ref: str, base_ref: str,
                     base_has_checker: bool, official_ancestor: bool) -> int:
    if (code == 1 and repository == "ChatArch/hermes-agent"
            and head_ref == "sync/official-20261010" and base_ref == "main"
            and not base_has_checker and official_ancestor):
        return 0
    return code


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=15, check=False)


def initial_adoption_state(repo: Path, base: str) -> tuple[bool, bool]:
    resolved = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
    if resolved.returncode:
        raise RuntimeError("Health baseline commit unavailable")
    tree = _git(repo, "ls-tree", "--name-only", resolved.stdout.strip(), "--", _SWITCH_FILE)
    if tree.returncode:
        raise RuntimeError("Health baseline tree unavailable")
    ancestor = _git(repo, "merge-base", "--is-ancestor", _OFFICIAL_PARENT, "HEAD")
    if ancestor.returncode not in (0, 1):
        raise RuntimeError("Official import ancestry unavailable")
    return bool(tree.stdout.strip()), ancestor.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    args = parser.parse_args(argv)
    repo = Path.cwd()
    try:
        has_checker, official = initial_adoption_state(repo, args.base)
        result = subprocess.run([sys.executable, "scripts/check", "--only", "health", "--base", args.base],
                                cwd=repo, timeout=1200, check=False)
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        print("Health adoption verification or measurement unavailable", file=sys.stderr)
        return 2
    code = adoption_verdict(result.returncode, repository=os.environ.get("GITHUB_REPOSITORY", ""),
                            head_ref=os.environ.get("GITHUB_HEAD_REF", ""),
                            base_ref=os.environ.get("GITHUB_BASE_REF", ""),
                            base_has_checker=has_checker, official_ancestor=official)
    if code == 0 and result.returncode == 1:
        print("::warning::Initial verified upstream adoption: the full health findings above are retained as migration debt. All functional, security-lint and build gates remain blocking; later PRs enforce the ratchet.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
