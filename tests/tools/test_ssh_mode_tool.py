import json

from gateway.session_context import clear_session_vars, set_session_vars
from gateway.ssh_bindings import get_ssh_binding, set_ssh_binding, set_ssh_yolo_grant
from gateway.ssh_targets import SshTarget


def _call(args):
    from tools.ssh_mode_tool import ssh_mode_tool

    return json.loads(ssh_mode_tool(args, task_id="session-1"))


def test_ssh_mode_status_and_list_are_read_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.ssh_mode_tool as ssh_mode_tool

    monkeypatch.setattr(
        ssh_mode_tool,
        "load_ssh_targets",
        lambda: [SshTarget(alias="cubebot", host="127.0.0.1", user="cubebot", identity_file="/secret/key")],
    )
    tokens = set_session_vars(
        platform="feishu",
        chat_id="oc_chat",
        thread_id="omt_thread",
        session_key="agent:main:feishu:group:oc_chat:omt_thread",
        session_id="session-1",
    )
    try:
        status = _call({"action": "status"})
        targets = _call({"action": "list_targets"})
    finally:
        clear_session_vars(tokens)

    assert status["backend"] == "local"
    assert status["yolo"]["enabled"] is False
    assert targets["targets"][0]["alias"] == "cubebot"
    assert targets["targets"][0]["identity"] == "[REDACTED_PATH]"
    assert "/secret/key" not in json.dumps(targets)


def test_ssh_mode_request_use_requires_yolo_in_thread(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.ssh_mode_tool as ssh_mode_tool

    monkeypatch.setattr(
        ssh_mode_tool,
        "load_ssh_targets",
        lambda: [SshTarget(alias="cubebot", host="127.0.0.1", user="cubebot")],
    )
    session_key = "agent:main:feishu:group:oc_chat:omt_thread"
    tokens = set_session_vars(
        platform="feishu",
        chat_id="oc_chat",
        thread_id="omt_thread",
        session_key=session_key,
        session_id="session-1",
    )
    try:
        result = _call({"action": "request_use", "alias": "cubebot", "reason": "test"})
    finally:
        clear_session_vars(tokens)

    assert result["ok"] is False
    assert result["approval_required"] is True
    assert get_ssh_binding(session_key) is None


def test_ssh_mode_request_use_switches_when_yolo_allows(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.ssh_mode_tool as ssh_mode_tool
    from tools.terminal_tool import clear_task_env_overrides, resolve_task_overrides

    monkeypatch.setattr(
        ssh_mode_tool,
        "load_ssh_targets",
        lambda: [
            SshTarget(
                alias="cubebot",
                host="127.0.0.1",
                user="cubebot",
                cwd="/home/cubebot/Playground",
            )
        ],
    )
    session_key = "agent:main:feishu:group:oc_chat:omt_thread"
    set_ssh_yolo_grant(session_key, enabled=True, aliases=["cubebot"])
    clear_task_env_overrides("session-1")
    tokens = set_session_vars(
        platform="feishu",
        chat_id="oc_chat",
        thread_id="omt_thread",
        session_key=session_key,
        session_id="session-1",
    )
    try:
        result = _call({"action": "request_use", "alias": "cubebot", "reason": "test"})
        overrides = resolve_task_overrides("session-1")
    finally:
        clear_session_vars(tokens)
        clear_task_env_overrides("session-1")

    binding = get_ssh_binding(session_key)
    assert result["ok"] is True
    assert result["backend"] == "ssh"
    assert binding is not None
    assert binding.source == "agent-yolo"
    assert overrides["env_type"] == "ssh"
    assert overrides["ssh_host"] == "127.0.0.1"


def test_ssh_mode_request_local_only_clears_agent_bindings(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session_key = "agent:main:feishu:group:oc_chat:omt_thread"
    tokens = set_session_vars(
        platform="feishu",
        chat_id="oc_chat",
        thread_id="omt_thread",
        session_key=session_key,
        session_id="session-1",
    )
    try:
        set_ssh_binding(session_key, alias="cubebot", source="user")
        protected = _call({"action": "request_local", "reason": "done"})
        set_ssh_binding(session_key, alias="cubebot", source="agent-yolo")
        cleared = _call({"action": "request_local", "reason": "done"})
    finally:
        clear_session_vars(tokens)

    assert protected["protected"] is True
    assert cleared["changed"] is True
    assert get_ssh_binding(session_key) is None


def test_ssh_mode_request_use_rejects_feishu_mainthread(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = set_session_vars(
        platform="feishu",
        chat_id="oc_chat",
        thread_id="",
        session_key="agent:main:feishu:group:oc_chat",
        session_id="session-1",
    )
    try:
        result = _call({"action": "request_use", "alias": "cubebot"})
    finally:
        clear_session_vars(tokens)

    assert result["ok"] is False
    assert result["approval_required"] is True
    assert "MainThread" in result["reason"]
