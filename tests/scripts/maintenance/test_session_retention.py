"""Retention deletes history only after a lossless, isolated archive round trip."""
from __future__ import annotations

from contextlib import closing, redirect_stdout
from datetime import UTC, datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from scripts.maintenance import session_retention as retention


@pytest.fixture
def history(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    now = time.time()
    cutoff = now - 90 * 86400
    eligible = {"raw-old", "changed-old", "old-parent", "old-child", "dependent-old"}
    protected = {
        "open-messaging", "open-automation", "pinned", "root", "recent-child",
        "lease", "guarded-child", "compression-locked", "message-fresh",
        "started-fresh", "activity-fresh", "routed-old", "routed-open",
    }
    database = home / "state.db"
    code = """
import json
from pathlib import Path
import sys
from tests.scripts.maintenance.test_session_retention import _seed_history

payload = json.loads(sys.argv[1])
_seed_history(Path(payload["home"]), payload["now"], set(payload["eligible"]), set(payload["protected"]))
"""
    payload = {"home": str(home), "now": now, "eligible": sorted(eligible), "protected": sorted(protected)}
    process = subprocess.run([sys.executable, "-c", code, json.dumps(payload)],
                             cwd=Path(__file__).resolve().parents[3], env=os.environ.copy(),
                             capture_output=True, text=True, timeout=30, check=False)
    assert process.returncode == 0, process.stderr
    return SimpleNamespace(home=home, database=database, cutoff=cutoff, eligible=eligible,
                           protected=protected,
                           cutoff_iso=datetime.fromtimestamp(cutoff, UTC).isoformat())


def _seed_history(home, now, eligible, protected):
    """Let native process-lifetime header probes exit with the fixture's database owner."""
    from hermes_state import SessionDB

    old = now - 150 * 86400
    database = home / "state.db"
    db = SessionDB(db_path=database)
    try:
        for sid in sorted(eligible | protected):
            source = "cron" if sid == "open-automation" else "feishu"
            db.create_session(sid, source, system_prompt="shared retention prompt")

        def prepare(connection):
            connection.executemany(
                "INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,'user',?,?)",
                [(sid, "history for " + sid, old) for sid in sorted(eligible | protected)],
            )
            connection.execute(
                "INSERT INTO messages(session_id,role,content,timestamp) VALUES ('raw-old','assistant',?,?)",
                ("archived answer", old + 1),
            )
            connection.execute("ALTER TABLE messages ADD COLUMN retention_payload BLOB")
            connection.execute(
                "UPDATE sessions SET started_at=?,ended_at=?,last_activity_at=?", (old, old + 2, old))
            connection.execute(
                "UPDATE sessions SET ended_at=NULL WHERE id IN ('open-messaging','open-automation','routed-open')")
            connection.execute("UPDATE sessions SET pinned=1 WHERE id='pinned'")
            connection.execute("UPDATE sessions SET end_reason='compression' WHERE id='root'")
            connection.execute(
                "UPDATE sessions SET parent_session_id='root',last_activity_at=? WHERE id='recent-child'", (now,))
            connection.execute("UPDATE sessions SET parent_session_id='old-parent' WHERE id='old-child'")
            connection.execute("UPDATE sessions SET parent_session_id='pinned' WHERE id='dependent-old'")
            connection.execute("UPDATE sessions SET parent_session_id='lease' WHERE id='guarded-child'")
            connection.execute("UPDATE sessions SET started_at=? WHERE id='started-fresh'", (now,))
            connection.execute("UPDATE sessions SET last_activity_at=? WHERE id='activity-fresh'", (now,))
            connection.execute("UPDATE messages SET timestamp=? WHERE session_id='message-fresh'", (now,))
            connection.execute(
                "UPDATE messages SET active=0,compacted=1,display_identity=?,retention_payload=? "
                "WHERE session_id='raw-old' AND role='assistant'", (b"raw-display-identity", b"\x00\xffopaque"))
            connection.execute(
                "UPDATE messages SET retention_payload=? WHERE session_id='changed-old'", (b"original",))
            tools_hash = db._store_system_prompt(connection, "shared tool definitions")
            connection.execute("UPDATE sessions SET tool_names=? WHERE id='raw-old'", (tools_hash,))
            connection.execute(
                "INSERT INTO session_turn_leases VALUES ('lease','fixture',?,?)", (now, now + 600))
            connection.execute(
                "INSERT INTO compression_locks VALUES ('compression-locked','fixture',?,?)", (now, now + 600))
            for sid in ("routed-old", "routed-open"):
                connection.execute("INSERT INTO gateway_routing VALUES ('fixture',?,?,?)",
                                   (sid, json.dumps({"session_id": sid}), now))
            connection.execute(
                "INSERT INTO session_model_usage(session_id,model,task,input_tokens,actual_cost_usd) "
                "VALUES ('raw-old','fixture-model','main',123,0.25)")
        db._execute_write(prepare)
    finally:
        db.close()


def _run(*arguments):
    with redirect_stdout(io.StringIO()) as output:
        retention.main([str(argument) for argument in arguments])
    return json.loads(output.getvalue())


def _backup(history):
    result = _run("backup", "--home", history.home, "--cutoff", history.cutoff_iso, "--run-id", "fixture")
    return Path(result["manifest"])


def _apply_process(history, manifest, read_only=None):
    arguments = ["apply", "--home", str(history.home), "--manifest", str(manifest)]
    if read_only is None:
        command = [sys.executable, "-m", "scripts.maintenance.session_retention", *arguments]
    else:
        code = """
from pathlib import Path
import sys
from hermes_state import SessionDB
from scripts.maintenance import session_retention

sibling = SessionDB(db_path=Path(sys.argv[1]), read_only=sys.argv[2] == "true")
try:
    session_retention.main(sys.argv[3:])
finally:
    sibling.close()
"""
        command = [sys.executable, "-c", code, str(history.database), str(read_only).lower(), *arguments]
    return subprocess.run(command, cwd=Path(__file__).resolve().parents[3], env=os.environ.copy(),
                          capture_output=True, text=True, timeout=30, check=False)


def _session_ids(database):
    with retention.readonly(database) as connection:
        return {row[0] for row in connection.execute("SELECT id FROM sessions")}


def _archive_rows(database, selected, dependencies=()):
    rows = {}
    with retention.readonly(database) as connection:
        for table, column, identifiers in (
            ("sessions", "id", selected | set(dependencies)),
            ("messages", "session_id", selected),
            ("session_model_usage", "session_id", selected),
        ):
            placeholders = ",".join("?" for _ in identifiers)
            rows[table] = [dict(row) for row in connection.execute(
                f'SELECT * FROM "{table}" WHERE "{column}" IN ({placeholders}) ORDER BY 1', sorted(identifiers))]
        rows["system_prompts"] = [dict(row) for row in connection.execute("SELECT * FROM system_prompts ORDER BY hash")]
    return rows


def test_retention_roundtrip_preserves_raw_history_and_only_deletes_unprotected_unchanged_rows(history):
    original_hash = retention.sha_file(history.database)
    planned = _run("plan", "--cutoff", history.cutoff_iso)
    selected = [row["id"] for row in planned["sessions"]]
    assert planned["writes"] is False
    assert set(selected) == history.eligible
    assert selected.index("old-child") < selected.index("old-parent")
    with retention.readonly(history.database) as connection:
        automation = {row["id"] for row in retention.candidates(connection, history.cutoff, include_stale_open=True)}
        messaging = {row["id"] for row in retention.candidates(connection, history.cutoff, include_stale_messaging=True)}
    assert automation == history.eligible | {"open-automation"}
    assert messaging == history.eligible | {"open-automation", "open-messaging"}

    expected = _archive_rows(history.database, history.eligible, dependencies={"pinned"})
    manifest = _backup(history)
    assert retention.sha_file(history.database) == original_hash
    _run("verify", "--manifest", manifest, "--verify-restore")
    verification = json.loads((manifest.parent / "restore-verified.json").read_text())
    assert verification["verified"] is True
    assert verification["manifest_sha256"] == retention.sha_file(manifest)
    restored = history.home / "archives" / "restored.db"
    result = _run("restore", "--manifest", manifest, "--output", restored)
    assert result["verified"] is True
    assert result["live_database_replaced"] is False
    assert _archive_rows(restored, history.eligible, dependencies={"pinned"}) == expected

    # This unknown BLOB is absent from the decoded message projection; raw drift must still retain it.
    with closing(sqlite3.connect(history.database)) as connection, connection:
        connection.execute("UPDATE messages SET retention_payload=? WHERE session_id='changed-old'", (b"edited",))
    process = _apply_process(history, manifest)
    assert process.returncode == 0, process.stderr
    applied = json.loads(process.stdout)
    assert applied["vacuum"] is False
    assert _session_ids(history.database) == history.protected | {"changed-old"}
    receipt = {row["id"]: row for row in map(json.loads, Path(applied["receipt"]).read_text().splitlines())}
    assert receipt["changed-old"]["status"] == "retained_changed_since_backup"
    assert {sid for sid, row in receipt.items() if row["status"] == "deleted"} == history.eligible - {"changed-old"}
    assert all(row["checkpoint"][0] == 0 for row in receipt.values())
    for path in (manifest.parent, *manifest.parent.rglob("*"), restored):
        assert path.stat().st_mode & 0o077 == 0, path


def test_retention_refuses_unverified_archives_live_direct_holders_and_schema_drift(history):
    manifest = _backup(history)
    original_ids = _session_ids(history.database)
    process = _apply_process(history, manifest)
    assert process.returncode == 1
    assert "restore-verified" in process.stderr
    assert _session_ids(history.database) == original_ids
    assert not (manifest.parent / "apply.jsonl").exists()
    _run("verify", "--manifest", manifest, "--verify-restore")

    # Direct handles are not in the shared generation registry. Both access modes must block deletion.
    for read_only in (False, True):
        process = _apply_process(history, manifest, read_only=read_only)
        assert process.returncode == 1
        assert "in-process SessionDB" in process.stderr
        assert _session_ids(history.database) == original_ids
        assert not (manifest.parent / "apply.jsonl").exists()

    with closing(sqlite3.connect(history.database)) as connection, connection:
        connection.execute("ALTER TABLE messages ADD COLUMN added_after_backup TEXT")
    process = _apply_process(history, manifest)
    assert process.returncode == 1
    assert "schema changed" in process.stderr
    assert _session_ids(history.database) == original_ids
    assert not (manifest.parent / "apply.jsonl").exists()



def test_retention_keeps_runtime_ledger_sessions_and_their_ancestors(history):
    """Runtime admissions/workers are not archival transcript rows; retain them verbatim."""
    with closing(sqlite3.connect(history.database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "INSERT INTO session_admissions(admission_id,request_id,principal_id,target_session_id,"
            "lineage_json,payload_json,payload_digest,intent,status) "
            "VALUES ('a','r','human','old-child','[]','{}','digest','queue','terminal')")
        connection.execute(
            "INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,"
            "adoption_digest) VALUES ('w','raw-old','cron',1,1,'terminal','digest')")
        connection.commit()
        before = list(connection.iterdump())
        retention.canonical_schema(connection)
        selected = {row["id"] for row in retention.candidates(connection, history.cutoff)}
        assert not {"old-child", "old-parent", "raw-old"} & selected
        assert "changed-old" in selected
        assert list(connection.iterdump()) == before


def test_retention_refuses_a_runtime_table_with_cascading_session_deletes():
    with closing(sqlite3.connect(":memory:")) as connection:
        for table in retention.TABLES:
            connection.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE session_admissions (target_session_id TEXT "
                           "REFERENCES sessions(id) ON DELETE CASCADE)")
        with pytest.raises(RuntimeError, match="Unarchived foreign-key dependency"):
            retention.canonical_schema(connection)
