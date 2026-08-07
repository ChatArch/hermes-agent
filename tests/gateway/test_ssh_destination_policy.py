from gateway.ssh_bindings import (
    get_backend_auto_policy,
    get_ssh_binding,
    list_backend_auto_policies,
    set_backend_auto_enabled,
    set_ssh_binding,
)


SESSION_KEY = "agent:main:feishu:group:oc_chat:omt_thread"


def test_backend_policy_defaults_local_on_and_ssh_targets_off(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    local = get_backend_auto_policy(SESSION_KEY, "local")
    remote = get_backend_auto_policy(SESSION_KEY, "cubebot")

    assert local.enabled is True
    assert local.local_enabled is True
    assert remote.enabled is False
    assert list_backend_auto_policies(SESSION_KEY, ["local", "cubebot"]) == {
        "local": True,
        "cubebot": False,
    }


def test_backend_policy_can_turn_every_auto_switch_backend_off_while_current_binding_remains(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    set_ssh_binding(SESSION_KEY, alias="cubebot", source="user")

    local_result = set_backend_auto_enabled(SESSION_KEY, "local", False)
    remote_result = set_backend_auto_enabled(SESSION_KEY, "cubebot", False)

    assert local_result.ok is True
    assert remote_result.ok is True
    assert get_backend_auto_policy(SESSION_KEY, "local").enabled is False
    assert get_backend_auto_policy(SESSION_KEY, "cubebot").enabled is False
    assert get_ssh_binding(SESSION_KEY).alias == "cubebot"


def test_backend_policy_on_off_applies_to_local_and_ssh_backends_symmetrically(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert set_backend_auto_enabled(SESSION_KEY, "local", False).ok is True
    assert set_backend_auto_enabled(SESSION_KEY, "cubebot", True).ok is True
    assert set_backend_auto_enabled(SESSION_KEY, "cubebot", False).ok is True
    assert set_backend_auto_enabled(SESSION_KEY, "local", True).ok is True

    assert list_backend_auto_policies(SESSION_KEY, ["local", "cubebot"]) == {
        "local": True,
        "cubebot": False,
    }
