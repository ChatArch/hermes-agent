"""The launchd marker fixture must never signal its synthetic gateway PID."""

import pytest

pytest.importorskip("pwd")
pytest.importorskip("grp")

from hermes_cli import gateway as gateway_cli
from tests.hermes_cli import test_gateway_service as gateway_tests


def test_kickstart_marker_fixture_does_not_signal_host_process(tmp_path, monkeypatch):
    signals = []

    def forbidden_signal(pid, sig):
        signals.append((pid, sig))
        raise AssertionError("service signal escaped the mocked launchd fixture")

    monkeypatch.setattr(gateway_cli.os, "kill", forbidden_signal)
    gateway_tests.TestLaunchdServiceRecovery().test_launchd_restart_kickstart_path_writes_planned_restart_marker(tmp_path, monkeypatch)
    assert signals == []
