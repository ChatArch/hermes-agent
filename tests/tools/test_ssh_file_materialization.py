import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tools.environments.ssh import SSHEnvironment


def _environment(tmp_path):
    env = SSHEnvironment.__new__(SSHEnvironment)
    env.host = "remote.example"
    env.user = "alice"
    env.port = 2222
    env.key_path = "/keys/alice"
    env.identities_only = True
    env.known_hosts_path = tmp_path / "known_hosts"
    env.host_key_policy = "yes"
    env.control_socket = tmp_path / "control.sock"
    env._remote_home = "/home/alice"
    env.cwd = "/home/alice/work"
    return env


def test_materialize_source_expands_remote_home_and_rejects_relative_paths(tmp_path):
    env = _environment(tmp_path)

    assert env._normalize_materialize_source("~/shots/page.png") == "/home/alice/shots/page.png"
    assert env._normalize_materialize_source("/srv/shots/page.png") == "/srv/shots/page.png"

    with pytest.raises(ValueError, match="absolute"):
        env._normalize_materialize_source("shots/page.png")


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "//etc/passwd",
        "/proc/self/environ",
        "/run/secrets/service-token",
        "/home/alice/.ssh/id_ed25519",
        "/home/alice/.aws/credentials",
        "/home/alice/.hermes/auth.json",
        "/home/alice/.hermes/mcp-tokens/server.json",
    ],
)
def test_materialize_file_denies_remote_sensitive_paths(tmp_path, path):
    env = _environment(tmp_path)
    env._inspect_materialize_source = MagicMock()

    with pytest.raises(PermissionError, match="protected"):
        env.materialize_file(path, tmp_path / "out.bin", max_bytes=1024, timeout=5)

    env._inspect_materialize_source.assert_not_called()


def test_materialize_file_rechecks_canonical_sensitive_path(tmp_path, monkeypatch):
    env = _environment(tmp_path)
    env._inspect_materialize_source = MagicMock(
        return_value={
            "source_path": "/etc/passwd",
            "size": 8,
            "mtime": 100.0,
            "device": 1,
            "inode": 2,
        }
    )
    run = MagicMock()
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(PermissionError, match="Resolved.*protected"):
        env.materialize_file(
            "/home/alice/work/link.png",
            tmp_path / "out.png",
            max_bytes=1024,
            timeout=5,
        )

    run.assert_not_called()


def test_materialize_file_allows_remote_hermes_media_cache(tmp_path, monkeypatch):
    env = _environment(tmp_path)
    source = "/home/alice/.hermes/cache/images/page.png"
    env._inspect_materialize_source = MagicMock(
        return_value={"source_path": source, "size": 8, "mtime": 100.0, "device": 1, "inode": 2}
    )

    def fake_run(cmd, **kwargs):
        kwargs["stdout"].write(b"12345678")
        return subprocess.CompletedProcess(cmd, 0, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    destination = tmp_path / "page.png"

    result = env.materialize_file(source, destination, max_bytes=8, timeout=5)

    assert destination.read_bytes() == b"12345678"
    assert result["path"] == str(destination)


def test_materialize_file_rejects_oversized_remote_file_before_transfer(tmp_path, monkeypatch):
    env = _environment(tmp_path)
    env._inspect_materialize_source = MagicMock(
        return_value={"source_path": "/home/alice/work/big.png", "size": 9, "mtime": 100.0, "device": 1, "inode": 2}
    )
    run = MagicMock()
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(ValueError, match="too large"):
        env.materialize_file(
            "/home/alice/work/big.png",
            tmp_path / "big.png",
            max_bytes=8,
            timeout=5,
        )

    run.assert_not_called()


def test_materialize_file_streams_through_existing_ssh_options_atomically(tmp_path, monkeypatch):
    env = _environment(tmp_path)
    source = "/home/alice/work/page.png"
    env._inspect_materialize_source = MagicMock(
        return_value={"source_path": source, "size": 8, "mtime": 100.0, "device": 1, "inode": 2}
    )
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs["timeout"]
        kwargs["stdout"].write(b"12345678")
        return subprocess.CompletedProcess(cmd, 0, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    destination = tmp_path / "page.png"

    result = env.materialize_file(source, destination, max_bytes=8, timeout=17)

    assert destination.read_bytes() == b"12345678"
    assert result == {
        "source_path": source,
        "path": str(destination),
        "size": 8,
        "mtime": 100.0,
    }
    command = seen["cmd"]
    assert command[0] == "ssh"
    assert f"ControlPath={env.control_socket}" in command
    assert f"UserKnownHostsFile={env.known_hosts_path}" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "-p" in command and "2222" in command
    assert "-i" in command and "/keys/alice" in command
    assert command[-2] == "alice@remote.example"
    assert "python3 -c" in command[-1]
    assert command[-1].endswith(" 8 1 2 8")
    assert seen["timeout"] == 17
    assert not list(tmp_path.glob("*.part"))


def test_materialize_file_removes_partial_output_on_transfer_failure(tmp_path, monkeypatch):
    env = _environment(tmp_path)
    source = "/home/alice/work/page.png"
    env._inspect_materialize_source = MagicMock(
        return_value={"source_path": source, "size": 8, "mtime": 100.0, "device": 1, "inode": 2}
    )

    def fake_run(cmd, **kwargs):
        kwargs["stdout"].write(b"partial")
        return subprocess.CompletedProcess(cmd, 1, stderr=b"connection lost")

    monkeypatch.setattr(subprocess, "run", fake_run)
    destination = tmp_path / "page.png"

    with pytest.raises(RuntimeError, match="connection lost"):
        env.materialize_file(source, destination, max_bytes=8, timeout=5)

    assert not destination.exists()
    assert not list(tmp_path.glob("*.part"))


def test_materialize_file_rejects_growth_past_stream_cap(tmp_path, monkeypatch):
    env = _environment(tmp_path)
    source = "/home/alice/work/page.png"
    env._inspect_materialize_source = MagicMock(
        return_value={"source_path": source, "size": 8, "mtime": 100.0, "device": 1, "inode": 2}
    )

    def fake_run(cmd, **kwargs):
        kwargs["stdout"].write(b"123456789")
        return subprocess.CompletedProcess(cmd, 0, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    destination = tmp_path / "page.png"

    with pytest.raises(ValueError, match="grew beyond"):
        env.materialize_file(source, destination, max_bytes=8, timeout=5)

    assert not destination.exists()


def test_materialize_file_honors_recency_requirement(tmp_path):
    env = _environment(tmp_path)
    source = "/home/alice/work/old.png"
    env._inspect_materialize_source = MagicMock(
        return_value={
            "source_path": source,
            "size": 8,
            "mtime": 1.0,
            "device": 1,
            "inode": 2,
        }
    )

    with pytest.raises(PermissionError, match="recent"):
        env.materialize_file(
            source,
            tmp_path / "old.png",
            max_bytes=8,
            timeout=5,
            require_recent_seconds=60,
        )


def test_materialize_file_shares_one_timeout_across_metadata_and_transfer(
    tmp_path, monkeypatch,
):
    import tools.environments.ssh as ssh_module

    env = _environment(tmp_path)
    source = "/home/alice/work/page.png"
    env._inspect_materialize_source = MagicMock(
        return_value={
            "source_path": source,
            "size": 8,
            "mtime": 100.0,
            "device": 1,
            "inode": 2,
        }
    )
    ticks = iter([0.0, 1.0, 6.0])
    monkeypatch.setattr(
        ssh_module,
        "_monotonic",
        lambda: next(ticks),
        raising=False,
    )
    run = MagicMock(side_effect=AssertionError("transfer should not start"))
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(RuntimeError, match="timed out"):
        env.materialize_file(
            source,
            tmp_path / "page.png",
            max_bytes=8,
            timeout=5,
        )

    env._inspect_materialize_source.assert_called_once_with(source, 4)
    run.assert_not_called()
