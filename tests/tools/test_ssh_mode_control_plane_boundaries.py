"""Boundary tests for SSH Mode control-plane vs execution-plane behavior.

SSH Mode changes the effective terminal/file execution backend. It must not move
Hermes control-plane state, such as the local skills source-of-truth, onto the
SSH target machine.
"""

import json
from pathlib import Path


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
