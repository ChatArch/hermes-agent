import json

from gateway.session_context import clear_session_vars, set_session_vars
from gateway.ssh_bindings import (
    get_backend_auto_policy,
    get_ssh_binding,
    set_backend_auto_enabled,
    set_ssh_binding,
)
from gateway.ssh_targets import SshTarget


SESSION_KEY = "agent:main:feishu:group:oc_chat:omt_thread"


def _call(args):
    from tools.ssh_mode_tool import ssh_mode_tool

    return json.loads(ssh_mode_tool(args, task_id="session-1"))


def _session_vars(session_key=SESSION_KEY, *, thread_id="omt_thread", platform="feishu"):
    return set_session_vars(
        platform=platform,
        chat_id="oc_chat" if platform == "feishu" else "C123",
        thread_id=thread_id,
        session_key=session_key,
        session_id="session-1",
    )


def test_ssh_mode_schema_is_flat_backend_interface():
    import toolsets
    from tools.ssh_mode_tool import SSH_MODE_SCHEMA

    actions = SSH_MODE_SCHEMA["parameters"]["properties"]["action"]["enum"]
    assert actions == ["status", "list", "test", "use", "on", "off"]
    assert "backend" in SSH_MODE_SCHEMA["parameters"]["properties"]
    schema_text = json.dumps(SSH_MODE_SCHEMA, ensure_ascii=False)
    assert "request_local" not in schema_text
    assert "request_use" not in schema_text
    assert "yolo" not in schema_text.lower()
    assert "ssh_mode" in toolsets._HERMES_CORE_TOOLS
    assert "ssh_mode" in toolsets.TOOLSETS["terminal"]["tools"]


def test_ssh_mode_status_and_list_show_local_and_targets_as_peer_backends(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.ssh_mode_tool as ssh_mode_tool

    monkeypatch.setattr(
        ssh_mode_tool,
        "load_ssh_targets",
        lambda: [SshTarget(alias="cubebot", host="127.0.0.1", user="cubebot", identity_file="/secret/key")],
    )
    tokens = _session_vars()
    try:
        status = _call({"action": "status"})
        listed = _call({"action": "list"})
    finally:
        clear_session_vars(tokens)

    assert status["backend"] == "local"
    assert status["current_backend"] == "local"
    assert status["auto_switch"] == {"local": True, "cubebot": False}
    assert [item["backend"] for item in status["backends"]] == ["local", "cubebot"]
    assert status["backends"][0]["current"] is True
    assert status["backends"][1]["auto_switch"] is False
    assert listed["backends"][1]["identity"] == "[REDACTED_PATH]"
    assert "/secret/key" not in json.dumps(listed)


def test_ssh_mode_use_off_ssh_backend_requests_approval_and_does_not_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.ssh_mode_tool as ssh_mode_tool

    monkeypatch.setattr(
        ssh_mode_tool,
        "load_ssh_targets",
        lambda: [SshTarget(alias="cubebot", host="127.0.0.1", user="cubebot")],
    )
    tokens = _session_vars()
    try:
        result = _call({"action": "use", "backend": "cubebot", "reason": "debug"})
    finally:
        clear_session_vars(tokens)

    assert result["ok"] is False
    assert result["approval_required"] is True
    assert result["backend"] == "cubebot"
    assert get_ssh_binding(SESSION_KEY) is None
    assert get_backend_auto_policy(SESSION_KEY, "cubebot").enabled is False


def test_ssh_mode_allow_current_enables_each_requested_backend_additively(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.ssh_mode_tool as ssh_mode_tool
    from tools.terminal_tool import clear_task_env_overrides, resolve_task_overrides

    monkeypatch.setattr(
        ssh_mode_tool,
        "load_ssh_targets",
        lambda: [
            SshTarget(alias="cubebot", host="127.0.0.1", user="cubebot"),
            SshTarget(alias="builder", host="127.0.0.2", user="builder"),
        ],
    )
    choices = iter(["allow_current", "allow_current"])
    requests = []

    def _notify(data):
        requests.append(data["backend"])
        ssh_mode_tool.resolve_gateway_ssh_grant(SESSION_KEY, next(choices))

    ssh_mode_tool.register_gateway_ssh_grant_notify(SESSION_KEY, _notify)
    tokens = _session_vars()
    try:
        first = _call({"action": "use", "backend": "cubebot"})
        second = _call({"action": "use", "backend": "builder"})
        task_overrides = resolve_task_overrides("session-1")
        session_overrides = resolve_task_overrides(SESSION_KEY)
    finally:
        ssh_mode_tool.unregister_gateway_ssh_grant_notify(SESSION_KEY)
        clear_session_vars(tokens)
        clear_task_env_overrides("session-1")
        clear_task_env_overrides(SESSION_KEY)

    assert requests == ["cubebot", "builder"]
    assert first["ok"] is True
    assert first["current_backend"] == "cubebot"
    assert second["ok"] is True
    assert second["current_backend"] == "builder"
    assert task_overrides["env_type"] == "ssh"
    assert task_overrides["ssh_alias"] == "builder"
    assert session_overrides["env_type"] == "ssh"
    assert session_overrides["ssh_alias"] == "builder"
    binding = get_ssh_binding(SESSION_KEY)
    assert binding is not None
    assert binding.alias == "builder"
    assert get_backend_auto_policy(SESSION_KEY, "cubebot").enabled is True
    assert get_backend_auto_policy(SESSION_KEY, "builder").enabled is True


def test_ssh_mode_allow_all_enables_every_backend_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.ssh_mode_tool as ssh_mode_tool
    from gateway.ssh_bindings import list_backend_auto_policies
    from tools.terminal_tool import clear_task_env_overrides, resolve_task_overrides

    monkeypatch.setattr(
        ssh_mode_tool,
        "load_ssh_targets",
        lambda: [
            SshTarget(alias="cubebot", host="127.0.0.1", user="cubebot"),
            SshTarget(alias="builder", host="127.0.0.2", user="builder"),
        ],
    )
    set_backend_auto_enabled(SESSION_KEY, "local", False)

    def _notify(_data):
        ssh_mode_tool.resolve_gateway_ssh_grant(SESSION_KEY, "allow_all")

    ssh_mode_tool.register_gateway_ssh_grant_notify(SESSION_KEY, _notify)
    tokens = _session_vars()
    try:
        result = _call({"action": "use", "backend": "cubebot"})
        task_overrides = resolve_task_overrides("session-1")
        session_overrides = resolve_task_overrides(SESSION_KEY)
    finally:
        ssh_mode_tool.unregister_gateway_ssh_grant_notify(SESSION_KEY)
        clear_session_vars(tokens)
        clear_task_env_overrides("session-1")
        clear_task_env_overrides(SESSION_KEY)

    assert result["ok"] is True
    assert result["current_backend"] == "cubebot"
    assert task_overrides["env_type"] == "ssh"
    assert task_overrides["ssh_alias"] == "cubebot"
    assert session_overrides["env_type"] == "ssh"
    assert session_overrides["ssh_alias"] == "cubebot"
    assert list_backend_auto_policies(SESSION_KEY, ["local", "cubebot", "builder"]) == {
        "local": True,
        "cubebot": True,
        "builder": True,
    }


def test_ssh_mode_use_local_respects_local_off_and_keeps_current_ssh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    set_ssh_binding(SESSION_KEY, alias="cubebot", source="agent-auto")
    set_backend_auto_enabled(SESSION_KEY, "local", False)
    tokens = _session_vars()
    try:
        result = _call({"action": "use", "backend": "local", "reason": "done"})
    finally:
        clear_session_vars(tokens)

    assert result["ok"] is False
    assert result["approval_required"] is True
    assert result["backend"] == "local"
    assert get_ssh_binding(SESSION_KEY).alias == "cubebot"
    assert get_backend_auto_policy(SESSION_KEY, "local").enabled is False


def test_ssh_mode_use_local_when_local_on_clears_current_ssh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    set_ssh_binding(SESSION_KEY, alias="cubebot", source="agent-auto")
    tokens = _session_vars()
    try:
        result = _call({"action": "use", "backend": "local", "reason": "done"})
    finally:
        clear_session_vars(tokens)

    assert result["ok"] is True
    assert result["backend"] == "local"
    assert result["changed"] is True
    assert get_ssh_binding(SESSION_KEY) is None


def test_ssh_mode_model_cannot_use_on_off_to_grant_itself_backend_permission(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = _session_vars()
    try:
        result = _call({"action": "on", "backend": "cubebot"})
    finally:
        clear_session_vars(tokens)

    assert result["ok"] is False
    assert result["approval_required"] is True
    assert get_backend_auto_policy(SESSION_KEY, "cubebot").enabled is False


def test_ssh_mode_use_rejects_feishu_parent_chat_for_ssh_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.ssh_mode_tool as ssh_mode_tool

    monkeypatch.setattr(
        ssh_mode_tool,
        "load_ssh_targets",
        lambda: [SshTarget(alias="cubebot", host="127.0.0.1", user="cubebot")],
    )
    tokens = _session_vars(session_key="agent:main:feishu:group:oc_chat", thread_id="")
    try:
        result = _call({"action": "use", "backend": "cubebot"})
    finally:
        clear_session_vars(tokens)

    assert result["ok"] is False
    assert result["approval_required"] is True
    assert "/ssh use <backend>" in result["reason"]
