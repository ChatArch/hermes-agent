"""SSH browser calls must never fall through to gateway execution."""
import json
from types import SimpleNamespace

import pytest

from tools import browser_use_cli as browser
from tools import terminal_tool, file_tools
from tools.registry import registry


class FakeSSH:
    cwd = "/remote/work"

    def __init__(self):
        self.calls = []
        self.transfers = []
        self.response = {"success": True, "exit_code": 0, "output": "remote output", "workspace": "/remote/browser/workspace"}
        self.transfer_error = None

    def execute(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return {"returncode": 0, "output": json.dumps(self.response)}

    def materialize_file(self, source, destination, **kwargs):
        self.transfers.append((source, kwargs))
        if self.transfer_error:
            raise self.transfer_error
        destination.write_bytes(b"remote image")
        return {"source_path": source}


@pytest.fixture
def remote(monkeypatch):
    env = FakeSSH()
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {"env_type": "local"})
    monkeypatch.setattr(terminal_tool, "resolve_task_overrides", lambda task: {"env_type": "ssh", "ssh_alias": "target"} if task == "ssh-task" else {})
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task: SimpleNamespace(env=env))
    monkeypatch.setattr(browser, "_blocked_url_in_code", lambda code: None)
    monkeypatch.setattr(browser, "_find_cli", lambda: ["gateway-cli"])
    monkeypatch.setattr(browser, "_base_subprocess_env", lambda: {})
    monkeypatch.setattr(browser, "_route_backend", lambda *a, **k: (_ for _ in ()).throw(AssertionError("gateway routing forbidden")))
    monkeypatch.setattr(browser, "_read_browser_cfg", lambda: {})
    for key in ("BU_CDP_URL", "BU_CDP_WS", "BROWSER_CDP_URL", "BROWSER_USE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return env


def dispatch(**args):
    result = registry.dispatch("browser_exec", {"code": "print('remote')", **args}, task_id="ssh-task")
    return json.loads(result) if isinstance(result, str) else result


def test_task_override_routes_through_ssh_not_gateway(remote):
    result = dispatch(session="named")
    assert result.get("output") == "remote output"
    assert remote.calls
    assert result["workspace"] == "/remote/browser/workspace"
    command, kwargs = remote.calls[0]
    assert "print('remote')" not in command
    assert json.loads(kwargs["stdin_data"])["code"].endswith("print('remote')")


def test_screenshot_materializes_from_bound_environment(remote, monkeypatch):
    remote.response["screenshot_path"] = "/remote/shot.png"
    monkeypatch.setattr(browser, "_native_screenshot_result", lambda result, path: {"_multimodal": True, "content": [], "text_summary": json.dumps(result)})
    result = dispatch()
    assert result.get("_multimodal") is True
    assert remote.transfers[0][0] == "/remote/shot.png"
    assert 0 < remote.transfers[0][1]["max_bytes"] <= 20 * 1024 * 1024
    assert 0 < remote.transfers[0][1]["timeout"] <= 30


def test_failed_transfer_never_uses_host_same_name(remote):
    remote.response["screenshot_path"] = "/remote/shot.png"
    remote.transfer_error = PermissionError("protected remote path")
    result = dispatch()
    assert "screenshot_error" in result
    assert remote.transfers


def test_missing_remote_cli_is_structured(remote, monkeypatch):
    from tools.browser_use_cli_remote import _REMOTE_RUNNER
    import contextlib
    import io
    import sys
    import shutil
    from pathlib import Path
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    monkeypatch.setattr(sys, "argv", ["runner"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    output = io.StringIO()
    with contextlib.redirect_stdout(output), pytest.raises(SystemExit):
        exec(_REMOTE_RUNNER, {})
    assert json.loads(output.getvalue())["error_type"] == "remote_cli_missing"


def test_real_remote_wrapper_with_fixture_cli(remote, tmp_path, monkeypatch):
    """Execute the dispatched Python wrapper; the fixture CLI never starts a browser."""
    import subprocess
    cli = tmp_path / ".local/bin/browser-use"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/usr/bin/env python3\nimport sys, os\ncode=sys.stdin.read()\nassert os.environ['BH_TMP_DIR'].startswith(os.getcwd() + os.sep)\nassert os.environ['BH_RUNTIME_DIR'] != os.environ['BH_TMP_DIR']\nprint('fixture CLI executed on target: ' + os.getcwd())\nprint('fixture stderr', file=sys.stderr)\n")
    cli.chmod(0o700)
    clean_env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "BU_CDP_URL": "http://127.0.0.1:9222"}
    def execute(command, **kwargs):
        remote.calls.append((command, kwargs))
        proc = subprocess.run(command, shell=True, input=kwargs.get("stdin_data"), text=True, capture_output=True, env=clean_env, timeout=kwargs["timeout"])
        return {"returncode": proc.returncode, "output": proc.stdout}
    monkeypatch.setattr(remote, "execute", execute)
    monkeypatch.setenv("GATEWAY_TEST_SECRET", "must-not-be-forwarded")
    result = dispatch(session="fixture")
    assert result["success"] is True
    assert result["output"].startswith("fixture CLI executed on target: ")
    assert result["workspace"].startswith(str(tmp_path))
    assert result["stderr"].strip() == "fixture stderr"
    assert "must-not-be-forwarded" not in remote.calls[0][0]


def test_local_cloud_provider_default_does_not_refuse_ssh(remote, monkeypatch):
    monkeypatch.setattr(browser, "_read_browser_cfg", lambda: {"cloud_provider": "local"})
    assert dispatch()["success"] is True


def test_gateway_endpoint_is_not_reinterpreted_on_target(remote, monkeypatch):
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")
    assert dispatch()["error_type"] == "remote_endpoint_configuration_required"
    assert not remote.calls


def test_real_profile_request_fails_closed_without_gateway(remote):
    result = dispatch(local=True)
    assert result.get("error_type") == "remote_real_profile_unsupported"
    assert not remote.calls
