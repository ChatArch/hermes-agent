"""Resource snapshots must not hang while querying local processes."""
import os
import subprocess
from types import SimpleNamespace

import pytest

from evals import fanout_resource_bench as bench


def test_snapshot_bounds_both_process_probes_without_changing_metrics(monkeypatch, tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return SimpleNamespace(stdout=(
                f"ESTAB pid={os.getpid()}, fd=2\n"
                "LISTEN pid=999999, fd=1\n"
                f"ESTAB pid={os.getpid()}, fd=3 pid={os.getpid()}, fd=4\n"
            ))
        return SimpleNamespace(stdout="pyright-langserver\nhermes_kernel_runner\n")

    monkeypatch.setattr(bench.subprocess, "run", run)
    monkeypatch.setattr(bench, "_count_live", lambda _name: 0)
    data = bench._snap(os.getpid(), str(tmp_path / "absent.db"))
    assert data["tcp"] == 2 and data["pyright"] == 1 and data["kernels"] == 1
    assert data["db_mb"] == 0
    assert len(calls) == 2
    assert [kwargs["timeout"] for _, kwargs in calls] == [5, 5]
    assert calls[0][0] == ["ss", "-tanp"]
    assert calls[0][1].get("shell") is not True
    assert calls[1][0][:3] == ["ps", "-o", "args="]


@pytest.mark.parametrize("timed_phase", [0, 1])
def test_snapshot_probe_timeout_is_not_reported_as_zero(monkeypatch, tmp_path, timed_phase):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 5
        if len(calls) - 1 == timed_phase:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output="partial")
        return SimpleNamespace(stdout="1\n")

    monkeypatch.setattr(bench.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        bench._snap(os.getpid(), str(tmp_path / "absent.db"))
    assert len(calls) == timed_phase + 1


def test_missing_ss_preserves_zero_socket_count_and_still_reads_children(monkeypatch, tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command == ["ss", "-tanp"]:
            raise FileNotFoundError("ss")
        return SimpleNamespace(stdout="pyright-langserver\n")

    monkeypatch.setattr(bench.subprocess, "run", run)
    monkeypatch.setattr(bench, "_count_live", lambda _name: 0)
    result = bench._snap(os.getpid(), str(tmp_path / "absent.db"))
    assert result["tcp"] == 0 and result["pyright"] == 1
    assert calls == [["ss", "-tanp"], ["ps", "-o", "args=", "--ppid", str(os.getpid())]]
