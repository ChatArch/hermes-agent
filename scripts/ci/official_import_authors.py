"""Identify exact upstream-authored commits introduced by a verified official merge.

This does not invent GitHub author identities. It delegates attribution for commits
already in the public upstream repository while keeping fork-authored commits under
the existing exact-email mapping gate. An unverified claimed official parent fails
closed instead of silently shrinking the PR's author range.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from http.client import IncompleteRead
import urllib.error
import urllib.request
from pathlib import Path

OFFICIAL_COMMIT_API = "https://api.github.com/repos/NousResearch/hermes-agent/git/commits/"
OFFICIAL_MERGE_PREFIX = "Merge official Hermes main "
# This one-cycle allowlist contains the three upstream-main snapshots independently
# read back before the PR #66 merge. A future sync must review and pin its own tips.
VERIFIED_MAIN_TIPS = frozenset({
    "f86276d2ebcccf0530873c11e2965c3f1cbd5a31",
    "66605471e9f0b0832abbefaf625ce08e948ca540",
    "c57331677d7fc46298a7159cf3a650853f0966c5",
})


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60, check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"git {args[0]} failed ({proc.returncode}): {proc.stderr.strip()[:240]}")
    return proc.stdout.strip()


def verify_official_commit(sha: str) -> bool:
    if sha not in VERIFIED_MAIN_TIPS:
        return False
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "hermes-official-sync-ci"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def read_json(url: str) -> dict:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                if response.status != 200:
                    raise RuntimeError(f"official commit verification HTTP {response.status}")
                result = json.load(response)
        except (urllib.error.URLError, TimeoutError, IncompleteRead, ValueError) as exc:
            raise RuntimeError(f"official commit verification unavailable for {sha[:12]}") from exc
        return result

    return read_json(OFFICIAL_COMMIT_API + sha).get("sha") == sha


def _new_commits(repo: Path, base: str, head: str) -> list[tuple[str, str]]:
    lines = _git(repo, "log", "--no-merges", "--format=%H %ae", f"{base}..{head}")
    return [tuple(line.split(" ", 1)) for line in lines.splitlines() if line.strip()]


def fork_authors(repo: Path, base: str, head: str = "HEAD", *, verify=verify_official_commit) -> tuple[list[str], list[str]]:
    """Return (fork emails, verified upstream tips), retaining mixed-email fork commits."""
    base = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
    head = _git(repo, "rev-parse", "--verify", f"{head}^{{commit}}")
    _git(repo, "merge-base", "--is-ancestor", base, head)
    # GitHub checks out a synthetic merge with the target base as parent one and
    # the PR branch as parent two. Its first-parent walk would see only the base.
    # Unwrap only that exact shape; fork-authored non-merge commits remain in base..head.
    head_parents = _git(repo, "rev-list", "--parents", "-n", "1", head).split()
    history_head = head_parents[2] if len(head_parents) == 3 and head_parents[1] == base else head
    candidates = _git(repo, "rev-list", "--first-parent", "--merges", f"{base}..{history_head}").splitlines()
    tips = []
    for merge in candidates:
        subject = _git(repo, "show", "-s", "--format=%s", merge)
        if not subject.startswith(OFFICIAL_MERGE_PREFIX):
            continue
        parts = _git(repo, "rev-list", "--parents", "-n", "1", merge).split()
        if len(parts) != 3 or parts[0] != merge:
            raise RuntimeError(f"claimed official merge {merge[:12]} is not a two-parent merge")
        tip = parts[2]
        if not verify(tip):
            raise RuntimeError(f"official commit provenance unverified for {merge[:12]}")
        tips.append(tip)
    upstream = set()
    for tip in tips:
        upstream.update(_git(repo, "rev-list", "--no-merges", f"{base}..{tip}").splitlines())
    fork = sorted({email for sha, email in _new_commits(repo, base, head) if sha not in upstream})
    return fork, tips


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python3 scripts/ci/official_import_authors.py BASE HEAD", file=sys.stderr)
        return 2
    try:
        emails, tips = fork_authors(Path.cwd(), sys.argv[1], sys.argv[2])
    except (OSError, RuntimeError) as exc:
        print(f"official import attribution: {exc}", file=sys.stderr)
        return 2
    print(f"official import attribution: verified {len(tips)} parent(s); checking {len(emails)} fork email(s)", file=sys.stderr)
    print("\n".join(emails))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
