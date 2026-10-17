"""Passive Git-for-Windows sibling lookup for relocated PortableGit installs."""

from pathlib import Path


def windows_git_core_candidates() -> list[Path]:
    """Locate git-core siblings of the PATH launcher, without executing that launcher."""
    from hermes_platform.resolver import locate_command

    command = locate_command("git").command
    if not command:
        return []
    launcher = Path(command[0])
    if launcher.name.casefold() != "git.exe" or launcher.parent.name.casefold() not in {"cmd", "bin"}:
        return []
    root = launcher.parent.parent
    return [root / arch / "libexec" / "git-core" / "git.exe"
            for arch in ("clangarm64", "mingw64", "mingw32")]
