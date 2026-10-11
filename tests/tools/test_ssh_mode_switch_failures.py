"""Backend-switch failures must block divergent routes, not report success."""

import importlib
import json

import pytest

from gateway import session_context, ssh_bindings
from gateway.ssh_targets import SshTarget
from tools import ssh_mode_tool, terminal_tool

SESSION = "ssh-switch-failure-test-session"
TASK = "ssh-switch-failure-test-task"


@pytest.fixture
def bound_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    targets = [SshTarget(alias="prior", host="prior.invalid", user="test"),
               SshTarget(alias="candidate", host="candidate.invalid", user="test")]
    monkeypatch.setattr(ssh_mode_tool, "load_ssh_targets", lambda: targets)
    monkeypatch.setattr(ssh_bindings, "load_ssh_targets", lambda: targets)
    monkeypatch.setattr(ssh_mode_tool, "validate_ssh_target_for_runtime", lambda target: None)
    tokens = session_context.set_session_vars(platform="feishu", thread_id="test-thread",
                                             session_key=SESSION, session_id=TASK)
    binding = ssh_bindings.set_ssh_binding(SESSION, alias="prior")
    old = ssh_bindings.binding_to_task_overrides(binding, targets[0])
    terminal_tool.register_task_env_overrides(TASK, old)
    terminal_tool.register_task_env_overrides(SESSION, old)
    ssh_bindings.set_backend_auto_enabled(SESSION, "candidate", True)
    original_register = terminal_tool.register_task_env_overrides
    original_clear = terminal_tool.clear_task_env_overrides
    try:
        yield original_register, original_clear
    finally:
        original_clear(TASK)
        original_clear(SESSION)
        session_context.clear_session_vars(tokens)


def _call(backend, **args):
    return json.loads(ssh_mode_tool.ssh_mode_tool({"action": "use", "backend": backend, **args}, task_id=TASK))


def _assert_blocked():
    for key in (TASK, SESSION):
        with pytest.raises(RuntimeError, match="blocked|incomplete"):
            terminal_tool.resolve_task_overrides(key)


def test_second_registration_failure_cannot_report_success(bound_session, monkeypatch):
    register, _ = bound_session

    def fail_session_key(task_id, overrides):
        if task_id == SESSION:
            raise RuntimeError("injected second registration failure")
        return register(task_id, overrides)

    monkeypatch.setattr(terminal_tool, "register_task_env_overrides", fail_session_key)
    result = _call("candidate")
    assert result.get("ok") is False
    _assert_blocked()


def test_second_clear_failure_cannot_report_local(bound_session, monkeypatch):
    _, clear = bound_session

    def fail_session_key(task_id):
        if task_id == SESSION:
            raise RuntimeError("injected second clearing failure")
        return clear(task_id)

    monkeypatch.setattr(terminal_tool, "clear_task_env_overrides", fail_session_key)
    result = _call("local")
    assert result.get("ok") is False
    _assert_blocked()


def test_failed_switch_survives_state_module_reload(bound_session, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("registration unavailable")

    monkeypatch.setattr(terminal_tool, "register_task_env_overrides", fail)
    assert _call("candidate").get("ok") is False
    from gateway import ssh_switch_state
    importlib.reload(ssh_switch_state)
    _assert_blocked()


def test_explicit_retry_recovers_both_routes(bound_session, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("registration unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(terminal_tool, "register_task_env_overrides", fail)
        assert _call("candidate").get("ok") is False
        _assert_blocked()
    assert _call("candidate").get("ok") is True
    assert terminal_tool.resolve_task_overrides(TASK)["ssh_alias"] == "candidate"
    assert terminal_tool.resolve_task_overrides(SESSION)["ssh_alias"] == "candidate"


def test_local_selection_overrides_a_global_ssh_default(bound_session, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "default.invalid")
    assert _call("local").get("ok") is True
    assert terminal_tool.resolve_task_overrides(TASK).get("env_type") == "local"
    assert ssh_bindings.get_ssh_binding(SESSION) is None


def test_late_creator_cannot_publish_the_old_route(bound_session, monkeypatch):
    from types import SimpleNamespace
    from tools import terminal_tool_lifecycle

    assert _call("candidate").get("ok") is True
    cache_key = terminal_tool._resolve_container_task_id(TASK)

    def late_environment(*args, **kwargs):
        assert _call("local").get("ok") is True
        return SimpleNamespace(cwd="/safe", cleanup=lambda: None)

    monkeypatch.setattr(terminal_tool_lifecycle, "_create_configured_env", late_environment)
    with pytest.raises(RuntimeError, match="revision|blocked"):
        terminal_tool_lifecycle.ensure_task_env(TASK)
    assert cache_key not in terminal_tool._active_environments


def test_managed_route_never_returns_an_unstamped_cached_env(bound_session, monkeypatch):
    from types import SimpleNamespace
    from tools import terminal_tool_lifecycle

    assert _call("candidate").get("ok") is True
    cache_key = terminal_tool._resolve_container_task_id(TASK)
    old = SimpleNamespace(cwd="/old")
    new = SimpleNamespace(cwd="/new", cleanup=lambda: None)
    with terminal_tool._env_lock:
        terminal_tool._active_environments[cache_key] = old
    monkeypatch.setattr(terminal_tool_lifecycle, "_create_configured_env", lambda *a, **k: new)
    assert terminal_tool_lifecycle.ensure_task_env(TASK) is new
    assert terminal_tool._active_environments[cache_key] is new


@pytest.mark.parametrize("consumer", ["file", "code"])
def test_other_creators_cannot_publish_an_old_route(bound_session, monkeypatch, consumer):
    from types import SimpleNamespace
    from tools import file_tools, code_execution_tool, terminal_tool_backends

    assert _call("candidate").get("ok") is True
    key = terminal_tool._resolve_container_task_id(TASK)

    def changed_while_creating(*args, **kwargs):
        assert _call("local").get("ok") is True
        env = SimpleNamespace(cwd="/safe", cleanup=lambda: None)
        return ("ssh", env) if consumer == "file" else env

    if consumer == "file":
        monkeypatch.setattr(file_tools, "_create_terminal_env_for_file_ops", changed_while_creating)
        create = lambda: file_tools._get_file_ops(TASK)
    else:
        monkeypatch.setattr(terminal_tool_backends, "_create_environment", changed_while_creating)
        create = lambda: code_execution_tool._get_or_create_env(TASK)
    try:
        with pytest.raises(RuntimeError, match="revision|blocked"):
            create()
        assert key not in terminal_tool._active_environments
    finally:
        file_tools.clear_file_ops_cache(key)


@pytest.mark.parametrize("owner", ["background", "foreground"])
def test_switch_does_not_cleanup_an_environment_still_owned_by_running_work(bound_session, monkeypatch, owner):
    from types import SimpleNamespace
    from tools.environments import base
    from tools.process_registry import process_registry
    from gateway.ssh_switch_state import routing_snapshot

    assert _call("prior").get("ok") is True
    cleanup_calls = []
    env = SimpleNamespace(cwd="/old", cleanup=lambda: cleanup_calls.append("cleanup"),
                          _hermes_ssh_snapshot=routing_snapshot(TASK))
    key = terminal_tool._resolve_container_task_id(TASK)
    with terminal_tool._env_lock:
        terminal_tool._active_environments[key] = env
    if owner == "background":
        monkeypatch.setattr(process_registry, "_running", {"owned-process": SimpleNamespace(env_ref=env, exited=False)})
    else:
        monkeypatch.setattr(base, "_live_foreground", {123: (env, object())})
    assert _call("candidate").get("ok") is True
    assert cleanup_calls == []
    from gateway.ssh_switch_env import reap_retired
    reap_retired(lifetime_seconds=0)
    assert cleanup_calls == []
    if owner == "background":
        process_registry._running.clear()
    else:
        base._live_foreground.clear()
    reap_retired(lifetime_seconds=0)
    assert cleanup_calls == ["cleanup"]


def test_reselecting_ready_backend_keeps_the_live_environment(bound_session, monkeypatch):
    from types import SimpleNamespace
    from tools import terminal_tool_lifecycle

    created = []
    def create(*args, **kwargs):
        env = SimpleNamespace(cwd="/stable", cleanup=lambda: None)
        created.append(env)
        return env

    monkeypatch.setattr(terminal_tool_lifecycle, "_create_configured_env", create)
    assert _call("prior").get("ok") is True
    first = terminal_tool_lifecycle.ensure_task_env(TASK)
    assert _call("prior").get("ok") is True
    assert terminal_tool_lifecycle.ensure_task_env(TASK) is first
    assert created == [first]


def test_status_reports_a_blocked_switch_without_raising(bound_session, monkeypatch):
    monkeypatch.setattr(terminal_tool, "register_task_env_overrides", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unavailable")))
    assert _call("candidate").get("ok") is False
    result = json.loads(ssh_mode_tool.ssh_mode_tool({"action": "status"}, task_id=TASK))
    assert result.get("ok") is False
    assert result.get("execution_blocked") is True


def test_abandoned_persisted_pending_can_only_recover_by_explicit_use(bound_session):
    from gateway.ssh_switch_state import begin_switch
    begin_switch(SESSION, "candidate")
    for key in (TASK, SESSION):
        with pytest.raises(RuntimeError, match="blocked"):
            terminal_tool.resolve_task_overrides(key)
    assert _call("candidate").get("ok") is True
    for key in (TASK, SESSION):
        assert terminal_tool.resolve_task_overrides(key)["ssh_host"] == "candidate.invalid"


def test_existing_empty_store_is_unknown_not_a_local_default(bound_session):
    ssh_bindings.default_ssh_bindings_path().write_text("")
    result = json.loads(ssh_mode_tool.ssh_mode_tool({"action": "status"}, task_id=TASK))
    assert result.get("ok") is False
    assert result.get("execution_blocked") is True


def test_successful_local_status_fields_agree(bound_session):
    assert _call("local").get("ok") is True
    result = json.loads(ssh_mode_tool.ssh_mode_tool({"action": "status"}, task_id=TASK))
    assert result["ok"] is True
    assert result["backend"] == result["current_backend"] == "local"
    assert result["binding"] is None


@pytest.mark.parametrize("first", ["file", "code", "ensure"])
@pytest.mark.parametrize("cwd_case", ["new_backend", "continued_session"])
def test_all_cold_consumers_agree_on_effective_cwd(bound_session, monkeypatch, first, cwd_case):
    from types import SimpleNamespace
    from tools import file_tools, code_execution_tool, terminal_tool_lifecycle, terminal_tool_backends

    config = {**terminal_tool._get_env_config(), "cwd": "/global"}
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: dict(config))
    terminal_tool.record_session_cwd(TASK, "/old")
    assert _call("candidate")["ok"] is True
    expected = "/global"
    if cwd_case == "continued_session":
        terminal_tool.record_session_cwd(TASK, "/continued")
        expected = "/continued"
    created = []
    def create(*args, **kwargs):
        env = SimpleNamespace(cwd=kwargs["cwd"], cleanup=lambda: None)
        created.append(env)
        return env
    monkeypatch.setattr(terminal_tool, "_create_configured_env", create)
    monkeypatch.setattr(terminal_tool_lifecycle, "_create_configured_env", create)
    monkeypatch.setattr(terminal_tool_backends, "_create_environment", create)
    consumers = {"file": lambda: file_tools._get_file_ops(TASK),
                 "code": lambda: code_execution_tool._get_or_create_env(TASK),
                 "ensure": lambda: terminal_tool_lifecycle.ensure_task_env(TASK)}
    key = terminal_tool._resolve_container_task_id(TASK)
    try:
        consumers[first]()
        assert created[0].cwd == expected
        for consume in consumers.values():
            consume()
        assert len(created) == 1
        assert terminal_tool._active_environments[key].cwd == expected
    finally:
        file_tools.clear_file_ops_cache(key)


@pytest.mark.parametrize("consumer", ["file", "code", "ensure"])
def test_cwd_change_during_creation_cannot_publish_old_config(bound_session, monkeypatch, consumer):
    from types import SimpleNamespace
    from tools import file_tools, code_execution_tool, terminal_tool_lifecycle, terminal_tool_backends
    assert _call("candidate")["ok"] is True
    def create(*args, **kwargs):
        terminal_tool.record_session_cwd(TASK, "/changed-during-creation")
        return SimpleNamespace(cwd=kwargs["cwd"], cleanup=lambda: None)
    monkeypatch.setattr(terminal_tool, "_create_configured_env", create)
    monkeypatch.setattr(terminal_tool_lifecycle, "_create_configured_env", create)
    monkeypatch.setattr(terminal_tool_backends, "_create_environment", create)
    creators = {"file": lambda: file_tools._get_file_ops(TASK),
                "code": lambda: code_execution_tool._get_or_create_env(TASK),
                "ensure": lambda: terminal_tool_lifecycle.ensure_task_env(TASK)}
    key = terminal_tool._resolve_container_task_id(TASK)
    try:
        with pytest.raises(RuntimeError, match="cwd|CWD|configur"):
            creators[consumer]()
        assert key not in terminal_tool._active_environments
    finally:
        file_tools.clear_file_ops_cache(key)


@pytest.mark.parametrize("consumer", ["terminal", "file", "code"])
@pytest.mark.parametrize("requested", ["candidate", "local"])
def test_partial_apply_blocks_real_consumer_entrypoints(bound_session, monkeypatch, tmp_path, consumer, requested):
    from tools import file_tools, code_execution_tool
    from gateway.ssh_switch_store import SshStateUnavailable
    original = terminal_tool.clear_task_env_overrides if requested == "local" else terminal_tool.register_task_env_overrides
    method = "clear_task_env_overrides" if requested == "local" else "register_task_env_overrides"
    def fail_second(key, *args, **kwargs):
        if key == SESSION:
            raise RuntimeError("second projection failed")
        return original(key, *args, **kwargs)
    monkeypatch.setattr(terminal_tool, method, fail_second)
    assert _call(requested)["ok"] is False
    executed = []
    def must_not_execute(*args, **kwargs):
        executed.append(True)
        raise AssertionError("execution must not be reached")
    monkeypatch.setattr(terminal_tool, "_acquire_env", must_not_execute)
    monkeypatch.setattr(code_execution_tool, "SANDBOX_AVAILABLE", True)
    monkeypatch.setattr(code_execution_tool, "_execute_remote", must_not_execute)
    canary = tmp_path / "must-not-read.txt"
    canary.write_text("HOST-FALLBACK-CANARY")
    calls = {"terminal": lambda: terminal_tool.terminal_tool("printf safe-probe", task_id=TASK),
             "file": lambda: file_tools.read_file_tool(str(canary), task_id=TASK),
             "code": lambda: code_execution_tool.execute_code("print('safe-probe')", task_id=TASK)}
    try:
        result = calls[consumer]()
    except SshStateUnavailable as exc:
        assert "blocked" in str(exc)
    else:
        response = json.loads(result)
        assert "blocked" in str(response).lower()
        assert "HOST-FALLBACK-CANARY" not in str(response)
    assert executed == []


def _finish_managed_command(plan, execute):
    from types import SimpleNamespace
    env = SimpleNamespace(cwd=plan.cwd, execute=execute)
    return terminal_tool._run_foreground("cd /after", env, plan, task_id=TASK,
        session_id=TASK, session_key=SESSION, workdir=None, approval_note=None,
        clear_interrupt=False, metered=False)


def test_terminal_cd_and_raw_consumers_share_one_owner_cwd(bound_session):
    from gateway.ssh_switch_state import runtime_config_snapshot
    assert _call("candidate", cwd="/selected")["ok"] is True
    plan = terminal_tool._plan_execution("cd /after", task_id=TASK, timeout=10,
                                        background=False, _host_local=False)
    _finish_managed_command(plan, lambda *a, **k: {"output": "done", "returncode": 0,
                                                  "cwd_observed": True, "cwd": "/after"})
    for key in (TASK, SESSION):
        overrides = terminal_tool.resolve_task_overrides(key)
        config, snapshot = runtime_config_snapshot(key, overrides, terminal_tool._get_env_config())
        assert config["cwd"] == snapshot[5] == "/after"


def test_terminal_plan_uses_dynamic_cwd_even_with_explicit_selection_cwd(bound_session):
    assert _call("candidate", cwd="/selected")["ok"] is True
    terminal_tool.record_session_cwd(SESSION, "/after")
    terminal_tool.record_session_cwd(TASK, "/after")
    plan = terminal_tool._plan_execution("pwd", task_id=TASK, timeout=10,
                                        background=False, _host_local=False)
    assert plan.cwd == plan.config["cwd"] == plan.ssh_route_snapshot[5] == "/after"


def test_old_foreground_result_cannot_write_cwd_into_new_revision(bound_session):
    assert _call("prior", cwd="/prior")["ok"] is True
    plan = terminal_tool._plan_execution("cd /prior-finished", task_id=TASK, timeout=10,
                                        background=False, _host_local=False)
    def late_result(*args, **kwargs):
        assert _call("candidate", cwd="/candidate")["ok"] is True
        return {"output": "original-output", "returncode": 7, "cwd_observed": True, "cwd": "/prior-finished"}
    result = json.loads(_finish_managed_command(plan, late_result))
    assert result["exit_code"] == 7
    assert result["output"] == "original-output"
    assert terminal_tool.get_session_cwd(SESSION) == "/candidate"
    assert terminal_tool.get_session_cwd(TASK) == "/candidate"


@pytest.mark.parametrize("lock_fault", ["directory", "permission"])
def test_completed_result_survives_lock_io_failure(bound_session, monkeypatch, lock_fault):
    from pathlib import Path
    from gateway import ssh_switch_store
    assert _call("candidate", cwd="/candidate")["ok"] is True
    plan = terminal_tool._plan_execution("cd /completed", task_id=TASK, timeout=10,
                                        background=False, _host_local=False)
    lock = Path(str(ssh_bindings.default_ssh_bindings_path()) + ".lock")
    backup = lock.with_name(lock.name + ".test-backup")
    def completed(*args, **kwargs):
        if lock_fault == "directory":
            lock.rename(backup)
            lock.mkdir()
        else:
            original_open = ssh_switch_store.os.open
            def denied(path, *args, **kwargs):
                if str(path) == str(lock):
                    raise PermissionError("test writeback lock denied")
                return original_open(path, *args, **kwargs)
            monkeypatch.setattr(ssh_switch_store.os, "open", denied)
        return {"output": "completed-original-output", "returncode": 7,
                "cwd_observed": True, "cwd": "/completed"}
    try:
        result = json.loads(_finish_managed_command(plan, completed))
        assert result["exit_code"] == 7
        assert result["output"] == "completed-original-output"
        assert terminal_tool.get_session_cwd(SESSION) == "/candidate"
        assert terminal_tool.get_session_cwd(TASK) == "/candidate"
    finally:
        if lock_fault == "directory" and lock.is_dir():
            lock.rmdir()
            backup.rename(lock)
