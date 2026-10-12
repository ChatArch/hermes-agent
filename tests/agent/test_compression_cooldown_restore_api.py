"""Compression cooldown compensation through the SessionDB API."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from hermes_state import SessionDB


def test_exact_cooldown_restore_tolerates_a_concurrently_deleted_session(
    tmp_path: Path,
) -> None:
    """A deleted session has no cooldown state left to compensate (#106271)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "COOLDOWN_ROLLBACK_DELETED_SESSION"
    db.create_session(session_id, source="cli")
    snapshot = db.get_compression_failure_cooldown_row(session_id)
    assert snapshot["session_exists"] is True

    assert db.delete_session(session_id) is True
    db.restore_compression_failure_cooldown_row(session_id, snapshot)

    assert db.get_compression_failure_cooldown_row(session_id) == {
        "session_exists": False,
        "cooldown_until": None,
        "error": None,
    }


def test_exact_cooldown_restore_tolerates_deletion_before_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deletion after the compensating write is still an intentional no-op (#106271)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "COOLDOWN_ROLLBACK_DELETE_DURING_VERIFY"
    db.create_session(session_id, source="cli")
    snapshot = db.get_compression_failure_cooldown_row(session_id)
    real_execute_write = db._execute_write

    def _restore_then_delete(callback):
        monkeypatch.setattr(db, "_execute_write", real_execute_write)
        result = real_execute_write(callback)
        assert db.delete_session(session_id) is True
        return result

    monkeypatch.setattr(db, "_execute_write", _restore_then_delete)
    db.restore_compression_failure_cooldown_row(session_id, snapshot)

    assert db.get_compression_failure_cooldown_row(session_id)["session_exists"] is False


def test_exact_cooldown_restore_api_propagates_sqlite_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "RAW_COOLDOWN_WRITE_FAILURE"
    db.create_session(session_id, source="test")

    def _write_fails(_callback) -> None:
        raise sqlite3.OperationalError("forced low-level write failure")

    monkeypatch.setattr(db, "_execute_write", _write_fails)

    with pytest.raises(sqlite3.OperationalError, match="forced low-level write failure"):
        db.restore_compression_failure_cooldown_row(
            session_id,
            {
                "session_exists": True,
                "cooldown_until": time.time() + 10.0,
                "error": "must propagate",
            },
        )
