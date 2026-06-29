"""Regression tests for the Tests GitHub Actions workflow."""

from __future__ import annotations

import pathlib

import yaml


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"


def test_tests_workflow_yaml_is_valid():
    content = TESTS_WORKFLOW.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    assert isinstance(parsed, dict)
    assert "jobs" in parsed


def test_save_durations_keeps_slice_artifacts_separate_before_merging():
    """Per-slice artifacts all contain test_durations.json.

    Downloading them with merge-multiple=true flattens same-named files into one
    directory, which has broken the main-only save-durations job. Keep each
    artifact in its own directory, then glob recursively.
    """

    content = TESTS_WORKFLOW.read_text(encoding="utf-8")

    assert "merge-multiple: false" in content
    assert "glob.glob('durations/**/test_durations.json', recursive=True)" in content


def test_duration_cache_restore_uses_saved_cache_prefix():
    """Main saves duration caches with a run-id suffix.

    Restore must include a matching prefix or later test jobs will not find the
    most recent main-branch duration cache.
    """

    content = TESTS_WORKFLOW.read_text(encoding="utf-8")

    assert "key: test-durations-${{ github.run_id }}" in content
    assert "restore-keys:" in content
    assert "test-durations-" in content
