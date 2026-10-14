"""Bound the postmortem Git probe without hiding incomplete evidence."""
from types import SimpleNamespace
import subprocess

import pytest

from evals.postmortem.forensics import rework


def test_git_probe_keeps_stdout_and_bounded_original_options(monkeypatch):
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(stdout="observed\n", returncode=9)

    monkeypatch.setattr(rework.subprocess, "run", run)
    assert rework._git("/review", "show", "--format=", "abc") == "observed\n"
    assert seen == [(["git", "-C", "/review", "show", "--format=", "abc"], {
        "capture_output": True, "text": True, "encoding": "utf-8",
        "errors": "replace", "check": False, "timeout": 120,
    })]


def test_git_probe_timeout_propagates_instead_of_claiming_no_commits(monkeypatch):
    def timed_out(command, **kwargs):
        assert kwargs["timeout"] == 120
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output="partial evidence")

    monkeypatch.setattr(rework.subprocess, "run", timed_out)
    with pytest.raises(subprocess.TimeoutExpired):
        rework._git("/review", "log", "--no-merges", "base..head")
