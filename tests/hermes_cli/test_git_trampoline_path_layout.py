"""Portable Git roots on PATH need not live in Program Files or the legacy home."""

import pytest

from hermes_cli import update_cmd_git
from hermes_platform import resolver


def _path_git(monkeypatch, tmp_path, *, arch="mingw64", launcher_dir="cmd", probe=False):
    root = tmp_path / "pm-tools" / "portable-git"
    launcher = root / launcher_dir / "git.exe"
    core = root / arch / "libexec" / "git-core" / "git.exe"
    launcher.parent.mkdir(parents=True)
    launcher.touch()
    core.parent.mkdir(parents=True)
    core.touch()
    resolution = resolver.Resolution("path_executable", (
        resolver.Candidate(str(launcher), "PATH", True),
    ))
    monkeypatch.setattr(resolver, "locate_command", lambda _name: resolution)
    monkeypatch.setattr(update_cmd_git, "_portable_git_candidates", list)
    monkeypatch.setattr(update_cmd_git, "_probe_fork_bomb",
                        lambda argv: probe if argv == [str(core)] else None)
    return core


@pytest.mark.parametrize("arch", ["clangarm64", "mingw64", "mingw32"])
@pytest.mark.parametrize("launcher_dir", ["cmd", "bin"])
def test_locate_real_git_accepts_probed_core_beside_path_launcher(monkeypatch, tmp_path, arch, launcher_dir):
    core = _path_git(monkeypatch, tmp_path, arch=arch, launcher_dir=launcher_dir)

    assert update_cmd_git._locate_real_git() == core


@pytest.mark.parametrize("probe", [None, True])
def test_path_core_still_requires_a_successful_probe(monkeypatch, tmp_path, probe):
    _path_git(monkeypatch, tmp_path, probe=probe)

    assert update_cmd_git._locate_real_git() is None


def test_missing_path_command_keeps_existing_no_candidate_result(monkeypatch):
    monkeypatch.setattr(resolver, "locate_command", lambda _name: resolver.Resolution("missing"))
    monkeypatch.setattr(update_cmd_git, "_portable_git_candidates", list)
    monkeypatch.setattr(update_cmd_git, "_probe_fork_bomb", lambda _argv: None)

    assert update_cmd_git._locate_real_git() is None


def test_unrelated_path_layout_does_not_search_an_arbitrary_parent(monkeypatch, tmp_path):
    _path_git(monkeypatch, tmp_path, launcher_dir="unrelated")

    assert update_cmd_git._locate_real_git() is None
