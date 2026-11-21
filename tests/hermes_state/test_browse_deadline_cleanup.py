"""The browse deadline covers pre-DDL barriers and is removed before rollback."""
import sqlite3

import pytest


class _Connection(sqlite3.Connection):
    callback = None
    clock = None
    rollback_was_unarmed = None

    def set_progress_handler(self, callback, steps):
        self.callback = callback
        return super().set_progress_handler(callback, steps)

    def execute(self, statement, *args):
        if statement == "PRAGMA user_version" and self.clock is not None:
            for _ in range(30):
                self.clock[0] += 0.1
                if self.callback is not None and self.callback():
                    raise sqlite3.OperationalError("interrupted")
        if statement == "ROLLBACK":
            self.rollback_was_unarmed = self.callback is None
        return super().execute(statement, *args)


def test_browse_deadline_covers_pre_transaction_durability_barriers(tmp_path, monkeypatch):
    import hermes_cli.sqlite_safe_read as reads
    import hermes_state_reconcile as reconcile
    import hermes_state_repair as repair

    connection = sqlite3.connect(tmp_path / "state.db", factory=_Connection, isolation_level=None)
    clock = [0.0]
    connection.clock = clock
    budget = reconcile._Budget(0.3)
    budget._clock = lambda: clock[0]
    budget.deadline = 0.3
    monkeypatch.setattr(reads, "connect_tracked", lambda *a, **kw: connection)
    monkeypatch.setattr(repair, "_reapply_durability_barriers", lambda conn: conn.execute("PRAGMA user_version"))
    with pytest.raises(reconcile.BrowseBudgetExceeded):
        reconcile._apply_additive_schema(tmp_path / "state.db", budget)
    assert clock[0] <= 0.5, "pre-DDL SQL escaped the lock-holding browse budget"
    assert connection.callback is None


def test_browse_clears_expired_progress_handler_before_rollback(tmp_path, monkeypatch):
    import hermes_cli.sqlite_safe_read as reads
    import hermes_state_reconcile as reconcile
    import hermes_state_schema as schema

    connection = sqlite3.connect(tmp_path / "state.db", factory=_Connection, isolation_level=None)
    monkeypatch.setattr(reads, "connect_tracked", lambda *a, **kw: connection)
    budget = reconcile._Budget(3)

    def interrupted(cursor):
        cursor.execute("CREATE TABLE probe (value)")
        budget.expired = True
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(schema, "apply_declared_schema", interrupted)
    with pytest.raises(reconcile.BrowseBudgetExceeded):
        reconcile._apply_additive_schema(tmp_path / "state.db", budget)
    assert connection.rollback_was_unarmed is True
    assert connection.callback is None
    with sqlite3.connect(tmp_path / "state.db") as check:
        assert check.execute("SELECT name FROM sqlite_master WHERE name='probe'").fetchall() == []
