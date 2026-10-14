"""Initial verified upstream adoption is reported; later PRs still block."""

from pathlib import Path
import subprocess

import pytest


def _verdict(code, *, repository="ChatArch/hermes-agent", head_ref="sync/official-20261010",
             base_ref="main", base_has_checker=False, official_ancestor=True):
    if not (Path(__file__).resolve().parents[2] / "scripts/ci/health_adoption.py").exists():
        return code
    from scripts.ci.health_adoption import adoption_verdict
    return adoption_verdict(code, repository=repository, head_ref=head_ref, base_ref=base_ref,
                            base_has_checker=base_has_checker, official_ancestor=official_ancestor)


def test_initial_verified_import_retains_report_without_blocking():
    assert _verdict(1) == 0


@pytest.mark.parametrize("code", [0, 2, 7, -15])
def test_measurement_and_process_errors_are_never_waived(code):
    assert _verdict(code) == code


@pytest.mark.parametrize("change", [
    {"repository": "Other/hermes-agent"}, {"head_ref": "feature/example"},
    {"base_ref": "other"}, {"base_has_checker": True}, {"official_ancestor": False},
])
def test_exception_is_only_the_initial_verified_import(change):
    assert _verdict(1, **change) == 1


def test_real_git_baseline_detects_first_adoption_and_restores_next_pr(tmp_path, monkeypatch):
    from scripts.ci import health_adoption as gate

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True, timeout=10).strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "README.md").write_text("fixture\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "baseline")
    baseline = git("rev-parse", "HEAD")
    monkeypatch.setattr(gate, "_OFFICIAL_PARENT", baseline)
    assert gate.initial_adoption_state(tmp_path, baseline) == (False, True)
    checker = tmp_path / "scripts/code_health/config.py"
    checker.parent.mkdir(parents=True)
    checker.write_text('ENFORCEMENT = "blocking"\n', encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "adopt")
    assert gate.initial_adoption_state(tmp_path, "HEAD") == (True, True)
    with pytest.raises(RuntimeError, match="baseline commit unavailable"):
        gate.initial_adoption_state(tmp_path, "not-a-revision")


@pytest.mark.parametrize("measurement_code,expected", [(0, 0), (1, 0), (2, 2), (7, 7)])
def test_entrypoint_retains_measurement_errors(monkeypatch, measurement_code, expected, capsys):
    from scripts.ci import health_adoption as gate

    monkeypatch.setenv("GITHUB_REPOSITORY", "ChatArch/hermes-agent")
    monkeypatch.setenv("GITHUB_HEAD_REF", "sync/official-20261010")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setattr(gate, "initial_adoption_state", lambda *_args: (False, True))
    monkeypatch.setattr(gate.subprocess, "run", lambda *args, **_kwargs: subprocess.CompletedProcess(args, measurement_code))
    assert gate.main(["--base", "HEAD^1"]) == expected
    assert ("::warning::" in capsys.readouterr().out) == (measurement_code == 1)
