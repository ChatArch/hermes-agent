"""The real config-edit command keeps the editor and managed-mode contracts."""

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import config


def test_managed_config_edit_never_starts_editor(monkeypatch):
    notices = []
    monkeypatch.setattr(config, "is_managed", lambda: True)
    monkeypatch.setattr(config, "managed_error", notices.append)
    monkeypatch.setattr(config.subprocess, "run", lambda *_a, **_k: pytest.fail("managed config opened editor"))
    config.config_command(SimpleNamespace(config_command="edit"))
    assert notices == ["edit configuration"]


def test_editor_interrupt_remains_visible_to_caller(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("model: test\n", encoding="utf-8")
    monkeypatch.setattr(config, "is_managed", lambda: False)
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    monkeypatch.setenv("EDITOR", "chosen-editor")

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(config.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        config.config_command(SimpleNamespace(config_command="edit"))
    assert path.read_text(encoding="utf-8") == "model: test\n"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("editor_exit", [0, 3])
def test_cli_editor_user_exit_preserves_saved_file(monkeypatch, tmp_path, editor_exit):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(config, "is_managed", lambda: False)
    editor = tmp_path / "test-editor"
    editor.write_text(
        f"#!{sys.executable}\n"
        "import sys\nfrom pathlib import Path\n"
        "path = Path(sys.argv[1])\n"
        "path.write_text(path.read_text(encoding='utf-8') + '\\n# USER_SAVED_EDITOR_RECEIPT\\n', encoding='utf-8')\n"
        f"raise SystemExit({editor_exit})\n",
        encoding="utf-8",
    )
    editor.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(editor))
    config.config_command(SimpleNamespace(config_command="edit"))
    path = Path(config.get_config_path())
    assert path.is_file()
    assert "USER_SAVED_EDITOR_RECEIPT" in path.read_text(encoding="utf-8")
