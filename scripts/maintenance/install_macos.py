"""Install or upgrade the private macOS monthly retention scheduler.

Execute through with_managed_runtime.py, passing --launcher to both scripts.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile

from hermes_constants import get_hermes_home
from hermes_platform.host.facts import os_family

LABEL = "ai.hermes.session-retention"
FILES = ("session_retention.py", "run_retention_cycle.py", "with_managed_runtime.py", "install_macos.py")


def atomic_copy(path, content):
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def launchctl(*arguments, check=True):
    return subprocess.run(["/bin/launchctl", *arguments], check=check, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)


def install(launcher):
    import fcntl

    from hermes_cli.config import atomic_config_replace, read_user_config_raw

    home = get_hermes_home().resolve()
    agents = Path.home() / "Library/LaunchAgents"
    gateway = plistlib.loads((agents / "ai.hermes.gateway.plist").read_bytes())
    gateway_environment = gateway.get("EnvironmentVariables", {})
    configured = gateway_environment.get("HERMES_HOME")
    if gateway.get("Label") != "ai.hermes.gateway" or not configured or Path(configured).expanduser().resolve() != home:
        raise RuntimeError("Default GUI gateway plist does not match this Hermes home")
    if gateway_environment.get("HOME") and Path(gateway_environment["HOME"]).expanduser().resolve() != Path.home().resolve():
        raise RuntimeError("Default GUI gateway plist belongs to a different user home")
    domain = f"gui/{os.getuid()}"  # windows-footgun: ok - installer is macOS-only
    launchctl("print", f"{domain}/ai.hermes.gateway")
    target = home / "maintenance/session-retention"
    lock_directory = home / "backups/session-retention"
    for directory in (target.parent, target, lock_directory.parent, lock_directory):
        if directory.is_symlink():
            raise RuntimeError(f"Refusing symlinked maintenance directory: {directory}")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    with (lock_directory / ".cycle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Retention cycle is running; retry installation when idle") from error
        job = launchctl("print", f"{domain}/{LABEL}", check=False)
        if any(line.strip().startswith("pid = ") for line in job.stdout.splitlines()):
            raise RuntimeError("Retention launchd job is running; retry installation when idle")
        plist = agents / f"{LABEL}.plist"
        config = home / "config.yaml"
        paths = [*(target / name for name in FILES), plist, config]
        if any(path.is_symlink() for path in paths):
            raise RuntimeError("Refusing symlinked installation files")
        original = {path: path.read_bytes() if path.exists() else None for path in paths}
        old_config = read_user_config_raw(config)
        backup_root = home / "backups/retention-policy"
        if backup_root.is_symlink():
            raise RuntimeError("Refusing symlinked retention backup directory")
        backup_root.mkdir(exist_ok=True, mode=0o700)
        backup_root.chmod(0o700)
        backup = backup_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup.mkdir(parents=True, mode=0o700)
        for path, content in original.items():
            if content is not None:
                atomic_copy(backup / path.name, content)
        environment = dict(os.environ, HERMES_HOME=str(home), HERMES_DISABLE_LAZY_INSTALLS="1")
        loaded = job.returncode == 0
        try:
            if loaded:
                launchctl("bootout", f"{domain}/{LABEL}")
            for name in FILES:
                atomic_copy(target / name, (Path(__file__).parent / name).read_bytes())
            for key, value in (("auto_prune", "false"), ("auto_archive", "false"),
                               ("retention_days", "90"), ("vacuum_after_prune", "false")):
                subprocess.run([str(launcher), "config", "set", f"sessions.{key}", value],
                               env=environment, check=True, timeout=30)
            definition = {
                "Label": LABEL,
                "ProgramArguments": ["/usr/bin/python3", str(target / "with_managed_runtime.py"),
                                     "--launcher", str(launcher), str(target / "run_retention_cycle.py"),
                                     "--log-file", str(target / "cycle.log")],
                "EnvironmentVariables": {"HERMES_HOME": str(home), "HOME": str(Path.home())},
                "StartCalendarInterval": {"Hour": 5, "Minute": 30},
                "RunAtLoad": False, "KeepAlive": False, "ExitTimeOut": 240, "Umask": 0o077,
                "ProcessType": "Background", "Nice": 10, "LowPriorityIO": True,
                "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null",
            }
            atomic_copy(plist, plistlib.dumps(definition))
            launchctl("bootstrap", domain, str(plist))
        except BaseException:
            launchctl("bootout", f"{domain}/{LABEL}", check=False)
            for path, content in original.items():
                if path == config and content is not None:
                    atomic_config_replace(config, old_config)
                elif content is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_copy(path, content)
            if loaded:
                launchctl("bootstrap", domain, str(plist))
            raise
    print(json.dumps({"installed": str(plist), "backup": str(backup), "retention_days": 90}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", type=Path, required=True)
    args = parser.parse_args()
    launcher = args.launcher.expanduser().resolve()
    if os_family() != "darwin" or not launcher.is_file() or not os.access(launcher, os.X_OK):
        parser.error("A macOS host and executable Hermes launcher are required")
    os.umask(0o077)
    install(launcher)


if __name__ == "__main__":
    main()
