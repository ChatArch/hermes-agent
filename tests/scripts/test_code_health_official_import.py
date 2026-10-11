"""Official import accounting only changes unchanged structural debt, not safety gates."""

import pytest

from scripts.code_health.model import Finding
from scripts.code_health.official_import import reconcile_official_import
from scripts.code_health import cli
from scripts.ci import official_import_authors
from tests.scripts.test_official_import_authors import _git, _history


def _finding(rule, path="source.py", scope="fn", line=8):
    return Finding(path, rule, scope, line, "new violation")


def test_only_verified_byte_identical_official_structure_is_classified_as_import_debt():
    from_fork = [_finding("FILE_LINES", scope="<module>", line=1),
                 _finding("CC"), _finding("F821"), _finding("HX006")]
    result = reconcile_official_import(from_fork, [], same_blob=lambda path: path == "source.py", tip="a" * 40)
    assert [f.rule for f in result if f.blocking] == ["F821", "HX006"]
    assert [f.rule for f in result if not f.blocking] == ["FILE_LINES", "CC"]
    assert all("official import" in f.detail for f in result if not f.blocking)


def test_conflict_rewrite_or_unverified_blob_never_gets_structural_credit():
    finding = _finding("FILE_LINES", scope="<module>", line=1)
    result = reconcile_official_import([finding], [], same_blob=lambda path: False, tip="a" * 40)
    assert result[0].blocking is True


def test_second_parent_findings_stay_blocking_even_when_base_file_was_identical():
    old = _finding("CC", path="fork.py")
    upstream = [_finding("F821", path="fork.py"), _finding("FILE_LINES", path="fork.py", scope="<module>")]
    result = reconcile_official_import([old], upstream, same_blob=lambda path: False, tip="a" * 40)
    assert [f.rule for f in result if f.blocking] == ["CC", "F821", "FILE_LINES"]


def test_duplicate_both_parent_findings_remain_single_blocking_result():
    old = _finding("HX006")
    from_upstream = _finding("HX006")
    result = reconcile_official_import([old], [from_upstream], same_blob=lambda path: False, tip="a" * 40)
    assert len(result) == 1 and result[0].blocking


def test_health_provenance_requires_real_merge_commit_not_only_subject(monkeypatch, tmp_path):
    repo, base, upstream = _history(tmp_path)
    observed = []
    monkeypatch.setattr(official_import_authors, "fork_authors", lambda path, root, head: (
        observed.append((path, root, head)) or ["fork@example.com"], [upstream],
    ))
    head = _git(repo, "rev-parse", "HEAD")
    assert cli._verified_official_tip(repo, base, head) == upstream
    assert observed == [(repo, base, head)]
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    assert cli._verified_official_tip(repo, base, tree) == upstream
    (repo / "dirty.txt").write_text("not part of the reviewed tree", encoding="utf-8")
    assert cli._verified_official_tip(repo, base, tree) == upstream
    (repo / "dirty.txt").unlink()
    _git(repo, "commit", "--amend", "-qm", "Merge fork-only work")
    assert cli._verified_official_tip(repo, base, _git(repo, "rev-parse", "HEAD")) is None
    assert len(observed) == 3


def test_only_the_actual_judged_file_bytes_receive_official_credit(tmp_path):
    repo, _base, upstream = _history(tmp_path)
    path = "official.txt"
    assert cli._matches_official_blob(repo, upstream, None, path)
    (repo / path).write_text("uncommitted fork edit", encoding="utf-8")
    assert not cli._matches_official_blob(repo, upstream, None, path)
    # An explicit committed head judges Git objects, not the dirty worktree.
    assert cli._matches_official_blob(repo, upstream, _git(repo, "rev-parse", "HEAD"), path)


def test_unavailable_provenance_fails_before_expensive_health_measurement(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_verified_official_tip", lambda *args: (_ for _ in ()).throw(
        RuntimeError("official provenance unavailable")))
    monkeypatch.setattr(cli.gitio, "changed_files", lambda *args: pytest.fail("measured before verification"))
    with pytest.raises(RuntimeError, match="official provenance unavailable"):
        cli.run(tmp_path, "missing-base", None, as_json=True)


def test_health_finds_verified_import_beneath_github_synthetic_merge(monkeypatch, tmp_path):
    repo, base, upstream = _history(tmp_path)
    pr_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "synthetic", base)
    _git(repo, "merge", "--no-ff", "-m", "Merge PR branch into main", pr_head)
    synthetic = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(official_import_authors, "fork_authors", lambda path, root, head: (
        ["fork@example.com"], [upstream],
    ))
    assert cli._verified_official_tip(repo, base, synthetic) == upstream
