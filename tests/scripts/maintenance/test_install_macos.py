"""Install and roll back a scheduler in an isolated home without contacting launchd."""
import fcntl
from pathlib import Path
import plistlib
import subprocess

import pytest

from scripts.maintenance import install_macos as installer

pytestmark = pytest.mark.platforms("macos")


@pytest.fixture
def installation(tmp_path, monkeypatch):
    from hermes_cli.config import atomic_config_write, read_user_config_raw

    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(installer, "get_hermes_home", lambda: home)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(installer, "__file__", str(source / "install_macos.py"))
    target = home / "maintenance/session-retention"
    target.mkdir(parents=True)
    for name in installer.FILES:
        (source / name).write_bytes(b"new script")
        (target / name).write_bytes(b"old script")
    agents = tmp_path / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "ai.hermes.gateway.plist").write_bytes(plistlib.dumps({
        "Label": "ai.hermes.gateway", "EnvironmentVariables": {"HERMES_HOME": str(home)}}))
    plist = agents / f"{installer.LABEL}.plist"
    original_plist = plistlib.dumps({"Label": installer.LABEL, "old": True})
    plist.write_bytes(original_plist)
    config = home / "config.yaml"
    config.write_text("# operator settings\nsessions:\n  retention_days: 120\n  auto_archive: true\n")
    launcher = tmp_path / "hermes-launcher"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o700)
    state = {"running": False, "loaded": True, "fail_bootstrap": False, "calls": [], "config_calls": []}

    def launchctl(*arguments, check=True):
        state["calls"].append(arguments)
        if arguments[0] == "print":
            stdout = "pid = 123\n" if state["running"] and arguments[1].endswith(installer.LABEL) else ""
            return subprocess.CompletedProcess(arguments, 0, stdout=stdout)
        if arguments[0] == "bootstrap" and state["fail_bootstrap"]:
            state["fail_bootstrap"] = False
            raise subprocess.CalledProcessError(5, arguments)
        state["loaded"] = arguments[0] == "bootstrap"
        return subprocess.CompletedProcess(arguments, 0, stdout="")

    def configure(command, **kwargs):
        state["config_calls"].append(command)
        assert command[:3] == [str(launcher), "config", "set"]
        assert kwargs["env"]["HERMES_HOME"] == str(home)
        value = {"false": False, "90": 90}[command[4]]
        data = read_user_config_raw(config)
        data["sessions"][command[3].split(".")[1]] = value
        atomic_config_write(config, data)

    monkeypatch.setattr(installer, "launchctl", launchctl)
    monkeypatch.setattr(installer.subprocess, "run", configure)
    return home, target, plist, launcher, state, original_plist


def test_install_creates_private_daily_job_with_monthly_runner(installation):
    home, target, plist, launcher, state, _ = installation
    installer.install(launcher)

    job = plistlib.loads(plist.read_bytes())
    assert job["StartCalendarInterval"] == {"Hour": 5, "Minute": 30}
    assert job["ProgramArguments"] == [
        "/usr/bin/python3", str(target / "with_managed_runtime.py"), "--launcher", str(launcher),
        str(target / "run_retention_cycle.py"), "--log-file", str(target / "cycle.log")]
    assert job["ExitTimeOut"] == 240 and job["Nice"] > 0 and job["Umask"] == 0o077
    assert job["StandardOutPath"] == job["StandardErrorPath"] == "/dev/null"
    assert state["loaded"] and len(state["config_calls"]) == 4
    assert target.stat().st_mode & 0o777 == 0o700
    for name in installer.FILES:
        assert (target / name).read_bytes() == b"new script"
        assert (target / name).stat().st_mode & 0o777 == 0o600
    backup, = (home / "backups/retention-policy").iterdir()
    assert backup.parent.stat().st_mode & 0o777 == 0o700
    assert (backup / "session_retention.py").read_bytes() == b"old script"
    assert (backup / "config.yaml").read_text().startswith("# operator settings")


def test_failed_upgrade_restores_files_config_and_previous_job(installation):
    from hermes_cli.config import read_user_config_raw

    home, target, plist, launcher, state, original_plist = installation
    state["fail_bootstrap"] = True
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(launcher)

    assert state["loaded"]
    assert plist.read_bytes() == original_plist
    assert all((target / name).read_bytes() == b"old script" for name in installer.FILES)
    assert read_user_config_raw(home / "config.yaml")["sessions"] == {"retention_days": 120, "auto_archive": True}


@pytest.mark.parametrize("busy", ["job", "lock"])
def test_running_cycle_refuses_upgrade_before_config_or_bootout(installation, busy):
    home, target, _, launcher, state, _ = installation
    lock_directory = home / "backups/session-retention"
    lock_directory.mkdir(parents=True)
    with (lock_directory / ".cycle.lock").open("a") as lock:
        if busy == "lock":
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            state["running"] = True
        with pytest.raises(RuntimeError, match="running"):
            installer.install(launcher)
    assert not state["config_calls"]
    assert all(call[0] != "bootout" for call in state["calls"])
    assert (target / "session_retention.py").read_bytes() == b"old script"
