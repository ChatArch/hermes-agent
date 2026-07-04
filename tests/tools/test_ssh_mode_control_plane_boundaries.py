"""Boundary tests for SSH Mode control-plane vs execution-plane behavior.

SSH Mode changes the effective terminal/file execution backend. It must not move
Hermes control-plane state, such as the local skills source-of-truth, onto the
SSH target machine.
"""

import json
from pathlib import Path

import pytest


def _skill_content(description="Boundary probe"):
    return (
        "---\n"
        "name: ssh-mode-boundary-probe\n"
        f"description: {description}\n"
        "---\n\n"
        "# SSH Mode Boundary Probe\n\n"
        "This skill is created by a test to verify local control-plane routing.\n"
    )


def _isolate_skill_home(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    skills_dir = hermes_home / "skills"
    skills_dir.mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from tools import skill_manager_tool
    from tools import skills_tool

    monkeypatch.setattr(skill_manager_tool, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(skill_manager_tool, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(skills_tool, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", skills_dir)

    return hermes_home, skills_dir


def _configure_system_ssh_backend(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "example.internal")
    monkeypatch.setenv("TERMINAL_SSH_USER", "rex")
    monkeypatch.setenv("TERMINAL_SSH_KEY", "/redacted/key")
    monkeypatch.setenv("TERMINAL_CWD", "/home/rex/work")


def _register_session_ssh_backend(task_id):
    from tools import terminal_tool

    terminal_tool.register_task_env_overrides(
        task_id,
        {
            "env_type": "ssh",
            "ssh_host": "example.internal",
            "ssh_user": "rex",
            "ssh_key": "/redacted/key",
            "cwd": "/home/rex/work",
        },
    )
    return terminal_tool


def test_skill_manage_system_ssh_backend_uses_local_skills_source(monkeypatch, tmp_path):
    """System-level SSH must not redirect skill creation to the SSH target path."""
    from tools import skill_manager_tool

    _, skills_dir = _isolate_skill_home(monkeypatch, tmp_path)
    _configure_system_ssh_backend(monkeypatch)

    result = json.loads(
        skill_manager_tool.skill_manage(
            action="create",
            name="ssh-mode-boundary-probe",
            category="tmp",
            content=_skill_content("System SSH boundary probe"),
        )
    )

    local_skill = skills_dir / "tmp" / "ssh-mode-boundary-probe" / "SKILL.md"
    assert result["success"] is True
    assert Path(result["skill_md"]) == local_skill
    assert local_skill.exists()
    assert str(result["skill_md"]).startswith(str(skills_dir))
    assert not str(result["skill_md"]).startswith("/home/rex/.hermes/skills")


def test_skill_manage_session_ssh_override_uses_local_skills_source(monkeypatch, tmp_path):
    """Session-scoped SSH Mode must not move skill source-of-truth to remote."""
    from tools import skill_manager_tool

    task_id = "ssh-mode-boundary-skill-manage"
    _, skills_dir = _isolate_skill_home(monkeypatch, tmp_path)
    terminal_tool = _register_session_ssh_backend(task_id)

    try:
        result = json.loads(
            skill_manager_tool.skill_manage(
                action="create",
                name="ssh-mode-boundary-probe",
                category="tmp",
                content=_skill_content("Session SSH boundary probe"),
            )
        )
    finally:
        terminal_tool.clear_task_env_overrides(task_id)

    local_skill = skills_dir / "tmp" / "ssh-mode-boundary-probe" / "SKILL.md"
    assert result["success"] is True
    assert Path(result["skill_md"]) == local_skill
    assert local_skill.exists()
    assert str(result["skill_md"]).startswith(str(skills_dir))
    assert not str(result["skill_md"]).startswith("/home/rex/.hermes/skills")



def test_skills_list_and_view_system_ssh_backend_read_local_source(monkeypatch, tmp_path):
    """Skill discovery/view are local control-plane reads, not SSH target reads."""
    from tools import skills_tool

    _, skills_dir = _isolate_skill_home(monkeypatch, tmp_path)
    _configure_system_ssh_backend(monkeypatch)

    skill_dir = skills_dir / "tmp" / "local-only-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: local-only-skill\n"
        "description: Local skill source of truth\n"
        "---\n\n"
        "# Local Only\n\n"
        "This content must come from the local Hermes profile.\n",
        encoding="utf-8",
    )

    listed = json.loads(skills_tool.skills_list())
    viewed = json.loads(skills_tool.skill_view("local-only-skill"))

    assert listed["success"] is True
    assert any(skill["name"] == "local-only-skill" for skill in listed["skills"])
    assert viewed["success"] is True
    assert viewed["skill_dir"] == str(skill_dir)
    assert "local Hermes profile" in viewed["content"]
    assert not viewed["skill_dir"].startswith("/home/rex/.hermes/skills")


def test_memory_system_ssh_backend_writes_local_memory_store(monkeypatch, tmp_path):
    """Memory is local profile state even when the effective terminal backend is SSH."""
    from tools import memory_tool

    hermes_home = tmp_path / "hermes-home"
    local_memory_dir = hermes_home / "memories"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(memory_tool, "get_memory_dir", lambda: local_memory_dir)
    _configure_system_ssh_backend(monkeypatch)

    store = memory_tool.MemoryStore(memory_char_limit=500, user_char_limit=500)
    store.load_from_disk()

    result = json.loads(
        memory_tool.memory_tool(
            action="add",
            target="memory",
            content="Local memory boundary probe",
            store=store,
        )
    )

    memory_file = local_memory_dir / "MEMORY.md"
    remote_like_memory = Path("/home/rex/.hermes/memories/MEMORY.md")
    assert result["success"] is True
    assert memory_file.exists()
    assert "Local memory boundary probe" in memory_file.read_text(encoding="utf-8")
    assert str(memory_file).startswith(str(hermes_home))
    assert str(memory_file) != str(remote_like_memory)


def test_memory_session_ssh_override_writes_local_memory_store(monkeypatch, tmp_path):
    """Session SSH overrides must not move memory writes to the SSH target."""
    from tools import memory_tool

    task_id = "ssh-mode-boundary-memory"
    hermes_home = tmp_path / "hermes-home"
    local_memory_dir = hermes_home / "memories"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(memory_tool, "get_memory_dir", lambda: local_memory_dir)
    terminal_tool = _register_session_ssh_backend(task_id)

    try:
        store = memory_tool.MemoryStore(memory_char_limit=500, user_char_limit=500)
        store.load_from_disk()
        result = json.loads(
            memory_tool.memory_tool(
                action="add",
                target="user",
                content="Local user profile boundary probe",
                store=store,
            )
        )
    finally:
        terminal_tool.clear_task_env_overrides(task_id)

    user_file = local_memory_dir / "USER.md"
    assert result["success"] is True
    assert user_file.exists()
    assert "Local user profile boundary probe" in user_file.read_text(encoding="utf-8")
    assert str(user_file).startswith(str(hermes_home))
    assert not str(user_file).startswith("/home/rex/.hermes/memories")



def test_todo_session_ssh_override_stays_in_local_store():
    """Todo is local in-memory agent/session state, not SSH target state."""
    from tools import todo_tool

    task_id = "ssh-mode-boundary-todo"
    terminal_tool = _register_session_ssh_backend(task_id)
    store = todo_tool.TodoStore()

    try:
        result = json.loads(
            todo_tool.todo_tool(
                todos=[{"id": "a", "content": "local todo", "status": "in_progress"}],
                store=store,
            )
        )
    finally:
        terminal_tool.clear_task_env_overrides(task_id)

    assert result["todos"] == [{"id": "a", "content": "local todo", "status": "in_progress"}]
    assert store.read() == result["todos"]


def test_cronjob_system_ssh_backend_writes_local_scheduler_store(monkeypatch, tmp_path):
    """Cron metadata belongs to the local Hermes service, never the SSH target."""
    from cron import jobs as cron_jobs
    from tools import cronjob_tools
    from tools.environments import ssh as ssh_env

    hermes_home = tmp_path / "hermes-home"
    cron_dir = hermes_home / "cron"
    jobs_file = cron_dir / "jobs.json"
    output_dir = cron_dir / "output"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr(cron_jobs, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(cron_jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(
        ssh_env.SSHEnvironment,
        "__init__",
        lambda self, *a, **k: pytest.fail("cronjob must not initialize an SSH backend"),
    )
    _configure_system_ssh_backend(monkeypatch)

    result = json.loads(
        cronjob_tools.cronjob(
            action="create",
            schedule="every 1h",
            prompt="Record a local scheduler boundary probe",
            name="local-cron-boundary",
        )
    )
    listed = json.loads(cronjob_tools.cronjob(action="list"))

    assert result["success"] is True
    assert jobs_file.exists()
    assert str(jobs_file).startswith(str(hermes_home))
    assert not str(jobs_file).startswith("/home/rex/.hermes/cron")
    assert listed["success"] is True
    assert listed["count"] == 1
    assert listed["jobs"][0]["name"] == "local-cron-boundary"


class _LocalSessionDB:
    def __init__(self):
        self.list_calls = []

    def list_sessions_rich(self, **kwargs):
        self.list_calls.append(kwargs)
        return [
            {
                "id": "local-session-1",
                "title": "Local session",
                "source": "feishu",
                "started_at": 1,
                "last_active": 2,
                "message_count": 3,
                "preview": "local service DB preview",
            }
        ]


def test_session_search_system_ssh_backend_reads_local_session_db(monkeypatch):
    """session_search is local service recall, not a remote SSH database query."""
    from tools import session_search_tool
    from tools.environments import ssh as ssh_env

    db = _LocalSessionDB()
    monkeypatch.setattr(
        ssh_env.SSHEnvironment,
        "__init__",
        lambda self, *a, **k: pytest.fail("session_search must not initialize an SSH backend"),
    )
    _configure_system_ssh_backend(monkeypatch)

    result = json.loads(session_search_tool.session_search(db=db, limit=1))

    assert result["success"] is True
    assert result["mode"] == "browse"
    assert result["results"][0]["session_id"] == "local-session-1"
    assert result["results"][0]["preview"] == "local service DB preview"
    assert db.list_calls


def test_control_plane_tools_ignore_gateway_session_ssh_context(monkeypatch, tmp_path):
    """Gateway session SSH binding must not pull Hermes control-plane tools onto SSH."""
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import memory_tool, session_search_tool, skill_manager_tool, todo_tool
    from tools.environments import ssh as ssh_env

    session_key = "agent:main:feishu:chat:thread"
    terminal_tool = _register_session_ssh_backend(session_key)
    _, skills_dir = _isolate_skill_home(monkeypatch, tmp_path)
    hermes_home = tmp_path / "hermes-home"
    local_memory_dir = hermes_home / "memories"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(memory_tool, "get_memory_dir", lambda: local_memory_dir)
    monkeypatch.setattr(
        ssh_env.SSHEnvironment,
        "__init__",
        lambda self, *a, **k: pytest.fail("control-plane tools must not initialize an SSH backend"),
    )

    tokens = set_session_vars(
        platform="feishu",
        chat_id="chat",
        thread_id="thread",
        session_key=session_key,
    )
    try:
        skill_result = json.loads(
            skill_manager_tool.skill_manage(
                action="create",
                name="ssh-mode-boundary-probe",
                category="tmp",
                content=_skill_content("Gateway session SSH boundary probe"),
            )
        )
        store = memory_tool.MemoryStore(memory_char_limit=500, user_char_limit=500)
        store.load_from_disk()
        memory_result = json.loads(
            memory_tool.memory_tool(
                action="add",
                target="memory",
                content="Gateway session local memory probe",
                store=store,
            )
        )
        todo_store = todo_tool.TodoStore()
        todo_result = json.loads(
            todo_tool.todo_tool(
                todos=[{"id": "local", "content": "gateway local todo", "status": "in_progress"}],
                store=todo_store,
            )
        )
        db = _LocalSessionDB()
        search_result = json.loads(session_search_tool.session_search(db=db, limit=1))
    finally:
        clear_session_vars(tokens)
        terminal_tool.clear_task_env_overrides(session_key)

    local_skill = skills_dir / "tmp" / "ssh-mode-boundary-probe" / "SKILL.md"
    assert skill_result["success"] is True
    assert Path(skill_result["skill_md"]) == local_skill
    assert memory_result["success"] is True
    assert (local_memory_dir / "MEMORY.md").exists()
    assert todo_result["todos"] == [{"id": "local", "content": "gateway local todo", "status": "in_progress"}]
    assert search_result["results"][0]["session_id"] == "local-session-1"
