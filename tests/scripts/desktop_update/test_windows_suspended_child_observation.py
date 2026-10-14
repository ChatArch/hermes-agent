"""Portable observation-order contract; real Windows process tests remain required."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_update_child_pid_is_logged_before_delegate_publication_and_resume():
    source = (ROOT / "scripts/desktop-update/windows.ps1").read_text(encoding="utf-8-sig")
    start = source.index("function Invoke-HermesStep")
    body = source[start:]
    created = body.index("$proc = $started.Process")
    update_only = body.index("if ($Tag -eq 'update')", created)
    event = 'Write-HandoffLog "update child created suspended pid $($proc.Id)"'
    assert event in body[update_only:], "suspended update PID is not observable before marker publication"
    event_at = body.index(event, update_only)
    publish = body.index("$delegate = Add-MarkerDelegate @($proc.Id)", update_only)
    resume = body.index("[HermesUpdateJob]::Resume($started)", publish)
    assert created < update_only < event_at < publish < resume
