"""Named browser isolation must succeed before any user action runs."""
import sys
import tempfile
from types import ModuleType, SimpleNamespace

import pytest

from tools.browser_use_cli import _OWN_TAB_PREAMBLE


@pytest.fixture
def session(monkeypatch, tmp_path):
    pid = tmp_path / "ipc" / "daemon.pid"
    pid.parent.mkdir()
    pid.write_text("42")
    package = ModuleType("browser_harness")
    package._ipc = SimpleNamespace(pid_path=lambda name: pid)
    monkeypatch.setitem(sys.modules, "browser_harness", package)
    monkeypatch.setenv("BU_NAME", "isolation-test")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return pid


@pytest.mark.parametrize("fault", ["create", "empty-target", "switch", "wrong-target", "missing-pid"])
def test_isolation_failure_aborts_user_code_without_success_marker(session, fault):
    pid = session
    actions = []
    current = {"targetId": "shared-tab"}
    if fault == "missing-pid":
        pid.unlink()

    def cdp(method, **params):
        if fault == "create":
            raise RuntimeError("create failed")
        return {} if fault == "empty-target" else {"targetId": "owned-tab"}

    def switch_tab(target):
        if fault == "switch":
            raise RuntimeError("switch failed")
        if fault != "wrong-target":
            current["targetId"] = target
        return "attached-session"

    namespace = {"cdp": cdp, "switch_tab": switch_tab, "current_tab": lambda: current,
                 "actions": actions}
    with pytest.raises(RuntimeError, match="isolation"):
        exec(_OWN_TAB_PREAMBLE + "\nactions.append('user action')", namespace)
    assert actions == []
    assert list(pid.parent.iterdir()) == ([pid] if pid.exists() else [])


def test_successful_isolation_is_reused_only_after_verified_switch(session):
    current = {"targetId": "shared-tab"}
    calls = []
    actions = []

    def cdp(method, **params):
        calls.append(method)
        return {"targetId": "owned-tab"}

    def switch_tab(target):
        current["targetId"] = target
        return "attached-session"

    namespace = {"cdp": cdp, "switch_tab": switch_tab, "current_tab": lambda: current,
                 "actions": actions}
    for _ in range(2):
        exec(_OWN_TAB_PREAMBLE + "\nactions.append('user action')", namespace)
    assert actions == ["user action", "user action"]
    assert calls == ["Target.createTarget"]
    assert current["targetId"] == "owned-tab"
