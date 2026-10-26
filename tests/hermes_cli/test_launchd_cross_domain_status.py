"""A background CLI must recognize a gateway supervised in another launchd domain."""

import os
import subprocess

import pytest

from hermes_cli import gateway
import hermes_cli.gateway_launchd as launchd


pytestmark = pytest.mark.platforms("macos")


def _cross_domain_launchctl(monkeypatch, domain, pid, *, list_failure="missing"):
    label = "ai.hermes.gateway"
    target = f"{domain}/{os.getuid()}/{label}"

    def run(command, **kwargs):
        if command == ["launchctl", "list", label]:
            if list_failure == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if list_failure == "unavailable":
                raise OSError("caller domain unavailable")
        elif command == ["launchctl", "print", target]:
            output = f"pid = {pid}\n" if pid is not None else "state = not running\n"
            return subprocess.CompletedProcess(command, 0, stdout=output)
        else:
            assert command[:2] == ["launchctl", "print"]
        return subprocess.CompletedProcess(command, 1, stdout="")

    monkeypatch.setattr(launchd.subprocess, "run", run)


@pytest.mark.parametrize("domain", ["gui", "user"])
@pytest.mark.parametrize("list_failure", ["missing", "timeout", "unavailable"])
def test_supervision_falls_back_to_explicit_domain(monkeypatch, domain, list_failure):
    _cross_domain_launchctl(monkeypatch, domain, 12345, list_failure=list_failure)

    assert launchd._launchctl_supervised_pid("ai.hermes.gateway") == 12345


@pytest.mark.parametrize("pid", [12345, None])
def test_status_recognizes_cross_domain_service(monkeypatch, tmp_path, capsys, pid):
    _cross_domain_launchctl(monkeypatch, "gui", pid)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: tmp_path / "gateway.plist")
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: "ai.hermes.gateway")
    monkeypatch.setattr(gateway, "launchd_plist_is_current", lambda: True)
    monkeypatch.setattr(gateway, "_launchd_unsupported_marker_exists", lambda: False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda **kwargs: pid)

    launchd.launchd_status()

    output = capsys.readouterr().out
    assert "not loaded" not in output
    if pid is not None:
        assert f"supervised by launchd (PID {pid})" in output
        assert "detached" not in output.lower()
    else:
        assert "registered with launchd" in output
        assert "supervised by launchd" not in output
