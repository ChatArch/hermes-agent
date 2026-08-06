from gateway.ssh_bindings import (
    get_destination_policy,
    get_ssh_binding,
    set_destination_enabled,
    set_ssh_binding,
    set_ssh_yolo_grant,
)


SESSION_KEY = "agent:main:feishu:group:oc_chat:omt_thread"


def test_destination_policy_defaults_local_on(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    policy = get_destination_policy(SESSION_KEY)

    assert policy.local_enabled is True
    assert policy.destination_enabled("local") is True


def test_destination_policy_can_disable_local_when_ssh_binding_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    set_ssh_binding(SESSION_KEY, alias="cubebot", source="user")

    result = set_destination_enabled(SESSION_KEY, "local", False)
    policy = get_destination_policy(SESSION_KEY)

    assert result.ok is True
    assert policy.local_enabled is False
    assert policy.destination_enabled("local") is False
    assert get_ssh_binding(SESSION_KEY).alias == "cubebot"


def test_destination_policy_refuses_to_disable_last_on_destination(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = set_destination_enabled(SESSION_KEY, "local", False)
    policy = get_destination_policy(SESSION_KEY)

    assert result.ok is False
    assert result.reason == "at_least_one_destination_required"
    assert "At least one execution destination must remain on" in result.message
    assert policy.local_enabled is True


def test_destination_policy_counts_yolo_grant_as_available_ssh_destination(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    set_ssh_yolo_grant(SESSION_KEY, enabled=True, aliases=["cubebot"])

    result = set_destination_enabled(SESSION_KEY, "local", False)
    policy = get_destination_policy(SESSION_KEY)

    assert result.ok is True
    assert policy.local_enabled is False
