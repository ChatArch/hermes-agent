"""Official merge attribution must never hide a fork-authored commit."""

import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.ci import official_import_authors as provenance
from scripts.ci.official_import_authors import fork_authors


def _git(repo: Path, *args: str, author: str = "fork@example.com") -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=20, check=False,
        env={**os.environ, "GIT_AUTHOR_NAME": "Test Author",
             "GIT_AUTHOR_EMAIL": author, "GIT_COMMITTER_NAME": "Test Committer",
             "GIT_COMMITTER_EMAIL": "reviewer@example.com"},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _commit(repo: Path, name: str, email: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", name, author=email)
    return _git(repo, "rev-parse", "HEAD")


def _history(tmp_path: Path, *, fork_email: str = "fork@example.com"):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "base.txt", "base@example.com")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "official")
    _commit(repo, "fork.txt", fork_email)
    _git(repo, "checkout", "-q", "official")
    upstream = _commit(repo, "official.txt", "official@example.com")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--no-ff", "-m", "Merge official Hermes main test-tip", "official")
    return repo, base, upstream


def test_verified_official_parent_excludes_only_its_exact_commits(tmp_path):
    repo, base, upstream = _history(tmp_path)
    requested = []
    emails, tips = fork_authors(repo, base, verify=lambda sha: requested.append(sha) or sha == upstream)
    assert requested == tips == [upstream]
    assert emails == ["fork@example.com"]


def test_fork_commit_using_official_authors_email_still_requires_mapping(tmp_path):
    repo, base, upstream = _history(tmp_path, fork_email="official@example.com")
    emails, tips = fork_authors(repo, base, verify=lambda sha: sha == upstream)
    assert tips == [upstream]
    assert emails == ["official@example.com"]


def test_claimed_official_merge_fails_closed_if_parent_cannot_be_verified(tmp_path):
    repo, base, _ = _history(tmp_path)
    with pytest.raises(RuntimeError, match="provenance unverified"):
        fork_authors(repo, base, verify=lambda sha: False)


def test_unrelated_merge_without_official_claim_cannot_shrink_author_range(tmp_path):
    repo, base, _ = _history(tmp_path)
    # Rewording an in-scope merge to be unrelated must require both authors.
    _git(repo, "commit", "--amend", "-qm", "Merge fork-only work")
    emails, tips = fork_authors(repo, base, verify=lambda sha: pytest.fail("unrelated merge verified"))
    assert tips == []
    assert emails == ["fork@example.com", "official@example.com"]


def test_unreviewed_parent_cannot_be_trusted_from_repository_presence(monkeypatch):
    sha = "a" * 40
    requests = []

    class Response(io.BytesIO):
        status = 200

    def fake_open(request, timeout):
        requests.append(request.full_url)
        payload = {"sha": sha} if len(requests) == 1 else {
            "status": "diverged", "base_commit": {"sha": "b" * 40}}
        return Response(json.dumps(payload).encode())

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(provenance.urllib.request, "urlopen", fake_open)
    assert provenance.verify_official_commit(sha) is False
    assert requests == []


def test_pinned_parent_must_still_exist_in_official_repository(monkeypatch):
    sha = "c57331677d7fc46298a7159cf3a650853f0966c5"

    class Response(io.BytesIO):
        status = 200

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        provenance.urllib.request, "urlopen",
        lambda request, timeout: Response(json.dumps({"sha": sha}).encode()),
    )
    assert provenance.verify_official_commit(sha) is True
