"""Public prompt fixtures must not capture an ancestor workspace's private context."""
from tests.ci.test_prompt_surface import prompt_surface


def test_render_does_not_capture_an_ancestor_workspace(tmp_path, monkeypatch):
    ancestor = tmp_path / "unrelated-workspace"
    ancestor.mkdir()
    (ancestor / "AGENTS.md").write_text("# Unrelated private workspace\n", encoding="utf-8")
    scratch = ancestor / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(prompt_surface.tempfile, "tempdir", str(scratch))
    rendered = tmp_path / "rendered"

    assert prompt_surface.render(rendered) == 0

    for prompt in (rendered / "prompts").glob("*.txt"):
        assert str(ancestor) not in prompt.read_text(encoding="utf-8"), prompt.name
