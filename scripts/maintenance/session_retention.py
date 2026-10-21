#!/usr/bin/env python3
"""Offline, backed-up Hermes history retention; use the configured Hermes Python environment.

Default action is plan (SQLite mode=ro + query_only, no output files). Supply a
fixed timezone-aware --cutoff, or --retention-days 90 with an optional --as-of.
backup writes owner-only, monthly grouped streaming gzip JSONL below
<home>/backups/session-retention/<run-id>. apply needs that completed manifest,
no foreign DB holders, unchanged exports, and adequate free space. No VACUUM.
restore creates a NEW isolated database; it never replaces the live store.

The JSONL includes native include_inactive exports plus lossless raw SQLite rows,
referenced prompts and usage accounting. Ancestors carry metadata dependencies.
Derived timings/search indexes are omitted; restored DBs are archival, never live.
Memory is bounded by one SQLite message row, not an entire session. A single
huge row still needs that row's memory. Restore commits at 100 rows / 4 MiB.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import UTC, datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time

MIN_FREE = 512 * 1024 * 1024
FORMAT = "hermes-session-retention-v1"
TABLES = ("system_prompts", "sessions", "messages", "session_model_usage")


def canonical_schema(connection):
    result = {}
    for table in TABLES:
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row is None:
            raise RuntimeError(f"Missing canonical table: {table}")
        result[table] = row[0]
    # Unknown cascades cannot silently become unarchived data loss after an upgrade.
    for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        name = row[0]
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise RuntimeError("Unsupported table identifier")
        for fk in connection.execute(f'PRAGMA foreign_key_list("{name}")'):
            if fk[2] in {"sessions", "messages"} and name not in TABLES:
                raise RuntimeError(f"Unarchived foreign-key dependency: {name}")
    return result


def ancestor_record(connection, sid):
    row = connection.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        raise RuntimeError(f"Missing referenced ancestor: {sid}")
    storage = dict(row)
    prompts = [dict(item) for item in connection.execute(
        "SELECT * FROM system_prompts WHERE hash IN (?,?) ORDER BY hash",
        (storage.get("system_prompt_hash"), storage.get("tool_names")))]
    return {"storage": portable(storage), "prompts": portable(prompts)}


def portable(value):
    if isinstance(value, bytes):
        return {"$sqlite_blob": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: portable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [portable(item) for item in value]
    return value


def unportable(value):
    if isinstance(value, dict):
        if set(value) == {"$sqlite_blob"}:
            return base64.b64decode(value["$sqlite_blob"], validate=True)
        return {key: unportable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unportable(item) for item in value]
    return value


def native():
    try:
        from hermes_state import SessionDB
        from hermes_state_maintenance import SessionMaintenanceMixin
        from hermes_state_common import _sql_session_last_active
    except ImportError as exc:
        raise RuntimeError("Run in the checkout's configured Hermes Python environment") from exc
    return SessionDB, SessionMaintenanceMixin, _sql_session_last_active


def cutoff_value(text):
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError("--cutoff must include a timezone, e.g. 2026-07-21T00:00:00+08:00")
    if value.timestamp() >= time.time():
        raise ValueError("--cutoff must be in the past")
    return value.timestamp()


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def free_space(path, extra=0):
    while not path.exists():
        path = path.parent
    free = shutil.disk_usage(path).free
    if free < MIN_FREE + extra:
        raise RuntimeError(f"Insufficient disk headroom: {free} bytes free, need {MIN_FREE + extra}")


def private_dir(path):
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlink directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".partial")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


@contextmanager
def readonly(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA cache_size=-2048")
    connection.execute("PRAGMA temp_store=MEMORY")
    try:
        yield connection
    finally:
        connection.close()


def offline(path, exclude=None):
    from hermes_state_holders import held_store_refusal, in_process_state_db_holders
    from hermes_state_readpool import _read_budget_for
    refusal = held_store_refusal(path, command="retention", force_hint=None)
    if refusal:
        raise RuntimeError(refusal)
    if in_process_state_db_holders(path, exclude=exclude):
        raise RuntimeError("Another in-process SessionDB holds the source store")
    # The generation registry omits direct SessionDB instances. The shared read
    # budget registers every open handle, including read-only siblings.
    budget = _read_budget_for(path)
    with budget._lock:
        if any(member is not exclude and member._conn is not None for member in budget._members):
            raise RuntimeError("Another in-process SessionDB holds the source store")


def candidates(connection, cutoff, include_stale_open=False, only_id=None, include_stale_messaging=False):
    """Native retention predicate plus conservative guards on whole parent graphs."""
    SessionDB, Maintenance, recency = native()
    where, params = Maintenance()._prune_where(None, None, {
        "last_active_before": cutoff, "archived": None,
    }, whole_lineages=False)
    # Recent creation is protected even when inherited activity predates it.
    where += " AND s.started_at < ?"
    params.append(cutoff)
    if include_stale_messaging:
        where = where.replace("s.ended_at IS NOT NULL", "1=1")
    elif include_stale_open:
        sources = SessionDB._AUTO_PRUNE_STALE_OPEN_SOURCES
        replacement = "(s.ended_at IS NOT NULL OR s.source IN (" + ",".join("?" for _ in sources) + "))"
        where = where.replace("s.ended_at IS NOT NULL", replacement)
        params = [*sources, *params]
    if only_id is not None:
        where += " AND s.id = ?"
        params.append(only_id)
    selected = {row[0] for row in connection.execute(f"SELECT s.id FROM sessions s WHERE {where}", params)}
    parents = dict(connection.execute("SELECT id, parent_session_id FROM sessions"))
    children = {}
    for sid, parent in parents.items():
        children.setdefault(parent, []).append(sid)
    now = time.time()
    guarded = {row[0] for row in connection.execute(
        "SELECT session_id FROM compression_locks WHERE expires_at > ?", (now,))}
    guarded.update(row[0] for row in connection.execute(
        "SELECT conversation_id FROM session_turn_leases WHERE expires_at > ?", (now,)))
    for routing in connection.execute("SELECT entry_json FROM gateway_routing WHERE updated_at >= ?", (cutoff,)):
        entry = json.loads(routing[0])
        if not isinstance(entry, dict) or not isinstance(entry.get("session_id"), str):
            raise RuntimeError("Cannot interpret recent gateway routing; refusing retention")
        guarded.add(entry["session_id"])
    frontier = list(guarded)
    while frontier:
        for child in children.get(frontier.pop(), ()):
            if child not in guarded:
                guarded.add(child)
                frontier.append(child)
    selected -= guarded
    # Keep ancestors of every retained row, including branches: this deliberately
    # retains more than native prune rather than detach a surviving conversation.
    kept = set(parents) - selected
    frontier = list(kept)
    while frontier:
        parent = parents.get(frontier.pop())
        if parent and parent not in kept:
            kept.add(parent)
            frontier.append(parent)
    selected -= kept
    if not selected:
        return []
    def depth(sid):
        seen = set()
        while parents.get(sid) and sid not in seen:
            seen.add(sid)
            sid = parents[sid]
        return len(seen)
    rows = []
    for sid in sorted(selected, key=lambda value: (-depth(value), value)):
        row = connection.execute(f"SELECT s.id,s.started_at,s.ended_at,{recency('s')} AS last_active "
                                 "FROM sessions s WHERE s.id=?", (sid,)).fetchone()
        rows.append(dict(row))
    return rows


def records(connection, sid):
    SessionDB, _, _ = native()
    row = connection.execute(
        "SELECT s.*, COALESCE(sp.prompt,s.system_prompt) AS _system_prompt_resolved, "
        "COALESCE(tp.prompt,s.tool_names) AS _tool_names_resolved FROM sessions s "
        "LEFT JOIN system_prompts sp ON sp.hash=s.system_prompt_hash "
        "LEFT JOIN system_prompts tp ON tp.hash=s.tool_names WHERE s.id=?", (sid,)).fetchone()
    if row is None:
        raise RuntimeError(f"Session disappeared: {sid}")
    metadata = SessionDB._session_row_dict(row)
    raw_session = dict(row)
    raw_session.pop("_system_prompt_resolved", None)
    raw_session.pop("_tool_names_resolved", None)
    hashes = [raw_session.get("system_prompt_hash"), raw_session.get("tool_names")]
    prompts = [dict(item) for item in connection.execute(
        "SELECT * FROM system_prompts WHERE hash IN (?,?) ORDER BY hash", hashes)]
    dependencies, seen = [], {sid}
    parent = raw_session.get("parent_session_id")
    while parent and parent not in seen:
        seen.add(parent)
        dependency = raw_session_record = ancestor_record(connection, parent)
        dependencies.append(dependency)
        parent = raw_session_record["storage"].get("parent_session_id")
    yield {"type": "session", "data": portable(metadata), "storage": portable(raw_session),
           "prompts": portable(prompts), "dependencies": list(reversed(dependencies))}
    # The native decoder is stateless; borrowing its class avoids any live DB opener.
    from hermes_state_messages import SessionMessagesMixin
    class Decoder(SessionMessagesMixin):
        _CONTENT_JSON_PREFIX = SessionDB._CONTENT_JSON_PREFIX
    decoder = Decoder()
    for row in connection.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (sid,)):
        yield {"type": "message", "data": portable(decoder._row_to_message_dict(
            row, warn_context="retention backup", summary_flag=True)), "storage": portable(dict(row))}
    for row in connection.execute("SELECT * FROM session_model_usage WHERE session_id=? "
                                  "ORDER BY model,billing_provider,billing_base_url,billing_mode,task", (sid,)):
        yield {"type": "usage", "storage": portable(dict(row))}


def snapshot_digest(connection, sid):
    digest, count, size = hashlib.sha256(), 0, 0
    for record in records(connection, sid):
        data = encoded(record)
        digest.update(data)
        size += len(data)
        count += record["type"] == "message"
    return digest.hexdigest(), count, size


def plan(args):
    with readonly(args.db) as connection:
        canonical_schema(connection)
        rows = candidates(connection, args.cutoff_epoch, args.include_stale_open,
                          include_stale_messaging=args.include_stale_messaging)
    print(json.dumps({"action": "plan", "cutoff": args.cutoff, "sessions": rows,
                      "count": len(rows), "include_stale_open": args.include_stale_open,
                      "writes": False}, indent=2))


def backup(args):
    root = args.home / "backups" / "session-retention"
    free_space(root)
    private_dir(root)
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S-%fZ")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Invalid run id")
    directory = root / run_id
    directory.mkdir(mode=0o700)
    manifest = {"format": FORMAT, "status": "incomplete", "database": str(args.db),
                "source_identity": [args.db.stat().st_dev, args.db.stat().st_ino],
                "cutoff": args.cutoff, "include_stale_open": args.include_stale_open,
                "include_stale_messaging": args.include_stale_messaging,
                "created_at": time.time(), "entries": []}
    write_json(directory / "incomplete.json", manifest)
    with readonly(args.db) as connection:
        manifest["schema"] = canonical_schema(connection)
        initial_rows = candidates(connection, args.cutoff_epoch, args.include_stale_open,
                                  include_stale_messaging=args.include_stale_messaging)
        ids = [row["id"] for row in initial_rows]
        for sid in ids:
            free_space(directory)
            connection.execute("BEGIN")
            try:
                # Apply rechecks eligibility and the exact raw snapshot before deletion.
                row = connection.execute("SELECT ended_at FROM sessions WHERE id=?", (sid,)).fetchone()
                if row is None:
                    continue
                last_active = next(value["last_active"] for value in initial_rows if value["id"] == sid)
                month = datetime.fromtimestamp(last_active, UTC).strftime("%Y-%m")
                private_dir(directory / month)
                relative = f"{month}/{hashlib.sha256(sid.encode()).hexdigest()}.jsonl.gz"
                target = directory / relative
                digest, count, size = hashlib.sha256(), 0, 0
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as raw:
                    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
                        for record in records(connection, sid):
                            data = encoded(record)
                            free_space(directory)
                            stream.write(data)
                            digest.update(data)
                            size += len(data)
                            count += record["type"] == "message"
                    raw.flush()
                    os.fsync(raw.fileno())
                sync_directory(target.parent)
                entry = {"id": sid, "path": relative, "sha256": sha_file(target),
                         "snapshot_sha256": digest.hexdigest(), "messages": count,
                         "uncompressed_bytes": size, "was_open": row["ended_at"] is None}
                verify_entry(directory, entry)
                manifest["entries"].append(entry)
                write_json(directory / "incomplete.json", manifest)
            finally:
                connection.rollback()
    manifest["status"] = "complete"
    write_json(directory / "manifest.json", manifest)
    print(json.dumps({"manifest": str(directory / "manifest.json"), "count": len(manifest["entries"])}))


def export_path(directory, entry):
    path = directory / entry["path"]
    if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
        raise RuntimeError("Unsafe export path")
    return path


def verify_entry(directory, entry):
    path = export_path(directory, entry)
    if sha_file(path) != entry["sha256"]:
        raise RuntimeError(f"Compressed checksum mismatch: {entry['id']}")
    digest, count, size = hashlib.sha256(), 0, 0
    with gzip.open(path, "rb") as stream:
        for index, line in enumerate(stream):
            record = json.loads(line)
            if (index == 0 and record.get("type") != "session") or (
                    index > 0 and record.get("type") not in {"message", "usage"}):
                raise RuntimeError("Invalid record sequence")
            if index == 0 and record["data"].get("id") != entry["id"]:
                raise RuntimeError("Export session id mismatch")
            if index and record["storage"].get("session_id") != entry["id"]:
                raise RuntimeError("Export message ownership mismatch")
            digest.update(encoded(record))
            size += len(encoded(record))
            count += record["type"] == "message"
    if (digest.hexdigest(), count, size) != (
            entry["snapshot_sha256"], entry["messages"], entry["uncompressed_bytes"]):
        raise RuntimeError(f"Snapshot checksum/count mismatch: {entry['id']}")


def read_manifest(path):
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    if manifest.get("format") != FORMAT or manifest.get("status") != "complete":
        raise RuntimeError("A completed retention manifest is required")
    ids = [entry["id"] for entry in manifest["entries"]]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Duplicate session ids in manifest")
    return manifest


def apply(args):
    manifest = read_manifest(args.manifest)
    if Path(manifest["database"]).resolve() != args.db:
        raise RuntimeError("Manifest belongs to a different database")
    if [args.db.stat().st_dev, args.db.stat().st_ino] != manifest["source_identity"]:
        raise RuntimeError("Source database was replaced after backup")
    backup_cutoff = cutoff_value(manifest["cutoff"])
    cutoff = cutoff_value(args.cutoff) if args.cutoff else backup_cutoff
    if cutoff > backup_cutoff:
        raise RuntimeError("Apply cutoff must be at least as conservative as the backup cutoff")
    directory = args.manifest.parent
    verification = json.loads((directory / "restore-verified.json").read_text(encoding="utf-8-sig"))
    if not verification.get("verified") or verification.get("manifest_sha256") != sha_file(args.manifest):
        raise RuntimeError("Run verify --verify-restore for this exact manifest before apply")
    for entry in manifest["entries"]:
        verify_entry(directory, entry)
    # Persist the backup's directory entries before deleting its source rows.
    for parent in (directory, *directory.parents):
        sync_directory(parent)
        if parent == args.home:
            break
    offline(args.db)
    free_space(args.db)
    with readonly(args.db) as connection:
        if canonical_schema(connection) != manifest["schema"]:
            raise RuntimeError("Canonical schema changed after backup")
    SessionDB, _, _ = native()
    db = SessionDB(db_path=args.db)
    receipt = directory / "apply.jsonl"
    fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "ab") as journal:
            for entry in manifest["entries"]:
                offline(args.db, exclude=db)
                # A single huge session can generate a large rollback journal/WAL.
                # Conservative refusal leaves its verified backup and source intact.
                free_space(args.db, extra=entry["uncompressed_bytes"] * 4)
                def delete_one(connection):
                    if canonical_schema(connection) != manifest["schema"]:
                        raise RuntimeError("Canonical schema changed after backup")
                    selected = {row["id"] for row in candidates(
                        connection, cutoff, manifest["include_stale_open"], only_id=entry["id"],
                        include_stale_messaging=manifest.get("include_stale_messaging", False))}
                    sid = entry["id"]
                    if sid not in selected:
                        return "retained_or_already_absent"
                    if db._guarded_ids(connection, [sid]):
                        return "retained_active_guard"
                    if snapshot_digest(connection, sid) != (
                            entry["snapshot_sha256"], entry["messages"], entry["uncompressed_bytes"]):
                        return "retained_changed_since_backup"
                    # Parent protection above means no surviving child is detached.
                    if connection.execute("SELECT 1 FROM sessions WHERE parent_session_id=? LIMIT 1", (sid,)).fetchone():
                        return "retained_has_children"
                    if entry["was_open"]:
                        connection.execute("UPDATE sessions SET ended_at=?,end_reason='retention_explicit_close' "
                                           "WHERE id=? AND ended_at IS NULL", (time.time(), sid))
                    connection.execute("DELETE FROM messages WHERE session_id=?", (sid,))
                    connection.execute("DELETE FROM sessions WHERE id=?", (sid,))
                    # Shared prompts are small; retaining them preserves source digests
                    # for subsequent entries even when this was their last live reference.
                    return "deleted"
                def disk_guard():
                    try:
                        free_space(args.db)
                        return 0
                    except (OSError, RuntimeError):
                        return 1
                db._conn.set_progress_handler(disk_guard, 10000)
                try:
                    status = db._execute_write(delete_one)
                finally:
                    db._conn.set_progress_handler(None, 0)
                # Offline checkpoint bounds WAL growth; never VACUUM/rewrite the DB.
                with db._lock:
                    checkpoint = db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    db._record_db_file_identity()
                journal.write(encoded({"id": entry["id"], "status": status, "at": time.time(),
                                       "checkpoint": list(checkpoint)}))
                journal.flush()
                os.fsync(journal.fileno())
                if checkpoint[0]:
                    raise RuntimeError("Offline checkpoint was busy; stopped before next session")
                free_space(args.db)
    finally:
        db.close()
    print(json.dumps({"receipt": str(receipt), "vacuum": False,
                      "note": "Freed DB pages are reusable; main-file size normally does not shrink. Transcript files retained."}))


def insert_storage(connection, table, storage):
    values = unportable(storage)
    columns = list(values)
    permitted = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    if not set(columns) <= permitted:
        raise RuntimeError(f"Unknown restore columns for {table}")
    names = ",".join('"' + column + '"' for column in columns)
    connection.execute(f'INSERT OR IGNORE INTO "{table}" ({names}) VALUES ({",".join("?" for _ in columns)})',
                       [values[column] for column in columns])


def new_archive_database(path, schema):
    if path.exists():
        raise RuntimeError("Restore output must be a NEW isolated database")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA cache_size=-2048")
    connection.execute("PRAGMA foreign_keys=ON")
    # Only canonical tables: derived FTS indexes need no backup and consume
    # substantial temporary space. This is an archival recovery DB, not a live profile.
    for table in TABLES:
        connection.execute(schema[table])
    connection.commit()
    return connection


def restore_entry(connection, path, entry):
    with gzip.open(export_path(path, entry), "rb") as stream:
        header = json.loads(next(stream))
        for session in [*header.get("dependencies", []), header]:
            for prompt in session["prompts"]:
                insert_storage(connection, "system_prompts", prompt)
            insert_storage(connection, "sessions", session["storage"])
        connection.commit()
        size = count = 0
        for line in stream:
            record = json.loads(line)
            table = {"message": "messages", "usage": "session_model_usage"}[record["type"]]
            insert_storage(connection, table, record["storage"])
            size += len(line)
            count += 1
            if count >= 100 or size >= 4 * 1024 * 1024:
                connection.commit()
                free_space(path)
                size = count = 0
        connection.commit()


def verify_restored_entry(connection, directory, entry):
    message_count = 0
    with gzip.open(export_path(directory, entry), "rb") as stream:
        for record in map(json.loads, stream):
            table = {"session": "sessions", "message": "messages", "usage": "session_model_usage"}[record["type"]]
            expected = unportable(record["storage"])
            if table == "session_model_usage":
                keys = ("session_id", "model", "billing_provider", "billing_base_url", "billing_mode", "task")
            else:
                keys = ("id",)
            clause = " AND ".join('"' + key + '" IS ?' for key in keys)
            row = connection.execute(f'SELECT * FROM "{table}" WHERE {clause}',
                                     [expected[key] for key in keys]).fetchone()
            if row is None or dict(row) != expected:
                raise RuntimeError(f"Raw restore differs from archive: {entry['id']} / {table}")
            if record["type"] == "session":
                for session in [*record.get("dependencies", []), record]:
                    storage = unportable(session["storage"])
                    saved = connection.execute("SELECT * FROM sessions WHERE id=?", (storage["id"],)).fetchone()
                    if saved is None or dict(saved) != storage:
                        raise RuntimeError("Restored ancestor metadata differs")
                    for prompt in map(unportable, session["prompts"]):
                        saved = connection.execute("SELECT * FROM system_prompts WHERE hash=?", (prompt["hash"],)).fetchone()
                        if saved is None or dict(saved) != prompt:
                            raise RuntimeError("Restored referenced prompt differs")
            message_count += table == "messages"
    actual = connection.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (entry["id"],)).fetchone()[0]
    if actual != message_count or actual != entry["messages"]:
        raise RuntimeError("Restored message count mismatch")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("Restored foreign-key check failed")
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise RuntimeError("Restored database quick_check failed")


def verify_restore(manifest_path, manifest):
    directory = manifest_path.parent
    check_path = directory / "restore-check.db"
    if check_path.exists():
        raise RuntimeError("Existing restore-check.db needs inspection before retry")
    for entry in manifest["entries"]:
        free_space(directory, entry["uncompressed_bytes"] * 2)
        connection = new_archive_database(check_path, manifest["schema"])
        try:
            restore_entry(connection, directory, entry)
            verify_restored_entry(connection, directory, entry)
        except BaseException:
            # Preserve the bounded recovery artifact for diagnosing a real failure.
            connection.close()
            raise
        connection.close()
        check_path.unlink()
    write_json(directory / "restore-verified.json", {"verified": True, "at": time.time(),
               "manifest_sha256": sha_file(manifest_path), "sessions": len(manifest["entries"]),
               "mode": "per-session canonical raw roundtrip; ancestor dependencies metadata only"})


def restore(args):
    manifest = read_manifest(args.manifest)
    if args.session_id:
        wanted = set(args.session_id)
        entries = [entry for entry in manifest["entries"] if entry["id"] in wanted]
        if {entry["id"] for entry in entries} != wanted:
            raise RuntimeError("Requested restore session is absent from this manifest")
        manifest = {**manifest, "entries": entries}
    directory = args.manifest.parent
    if args.output.resolve() == Path(manifest["database"]).resolve():
        raise RuntimeError("Restore must never replace the live database")
    for entry in manifest["entries"]:
        verify_entry(directory, entry)
    free_space(args.output, sum(entry["uncompressed_bytes"] for entry in manifest["entries"]) * 2)
    private_dir(args.output.parent)
    connection = new_archive_database(args.output, manifest["schema"])
    try:
        for entry in manifest["entries"]:
            restore_entry(connection, directory, entry)
        for entry in manifest["entries"]:
            verify_restored_entry(connection, directory, entry)
    finally:
        connection.close()
    print(json.dumps({"restored_database": str(args.output), "sessions": len(manifest["entries"]),
                      "live_database_replaced": False, "derived_indexes": "omitted", "verified": True,
                      "ancestor_dependencies": "metadata only when not separately archived"}))


def main(argv=None):
    from hermes_constants import get_hermes_home

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", nargs="?", choices=("plan", "backup", "verify", "apply", "restore"), default="plan")
    parser.add_argument("--home", type=Path, default=get_hermes_home(), help="Hermes home/profile; defaults to the active Hermes scope")
    parser.add_argument("--db", type=Path, help="Source state.db; defaults to <home>/state.db")
    parser.add_argument("--cutoff", help="Fixed ISO timestamp WITH timezone; plan/backup/apply (apply can only become more conservative)")
    parser.add_argument("--retention-days", type=int, help="Resolve a fixed cutoff N days before --as-of (default now)")
    parser.add_argument("--as-of", help="Timezone-aware ISO reference time for --retention-days")
    parser.add_argument("--include-stale-open", action="store_true", help="Explicitly close/delete stale native automation sources; never messaging/UI")
    parser.add_argument("--include-stale-messaging", action="store_true", help="Explicitly include stale open messaging/UI too; recent routing and guards stay protected")
    parser.add_argument("--run-id")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, help="NEW isolated restore database")
    parser.add_argument("--session-id", action="append", help="restore only: restore specific session(s), including ancestor metadata")
    parser.add_argument("--verify-restore", action="store_true", help="verify: bounded per-session raw restoration and receipt required by apply")
    args = parser.parse_args(argv)
    if args.session_id and args.action != "restore":
        parser.error("--session-id is only supported for restore")
    args.home = args.home.expanduser().resolve()
    args.db = (args.db or args.home / "state.db").expanduser().resolve()
    if args.action in {"plan", "backup", "apply"}:
        if args.cutoff and args.retention_days is not None:
            parser.error("Use either --cutoff or --retention-days")
        if args.retention_days is not None:
            from datetime import timedelta
            if args.retention_days < 1:
                parser.error("--retention-days must be positive")
            reference = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(UTC)
            if reference.tzinfo is None:
                parser.error("--as-of must include a timezone")
            args.cutoff = (reference - timedelta(days=args.retention_days)).isoformat()
        if not args.cutoff and args.action != "apply":
            parser.error("plan/backup require --cutoff or --retention-days")
        if args.cutoff:
            args.cutoff_epoch = cutoff_value(args.cutoff)
    if args.action in {"verify", "apply", "restore"} and not args.manifest:
        parser.error("verify/apply/restore require --manifest")
    if args.manifest:
        args.manifest = args.manifest.expanduser().resolve()
    if args.action == "restore":
        if not args.output:
            parser.error("restore requires --output")
        args.output = args.output.expanduser().resolve()
    if args.action == "verify":
        manifest = read_manifest(args.manifest)
        for entry in manifest["entries"]:
            verify_entry(args.manifest.parent, entry)
        if args.verify_restore:
            verify_restore(args.manifest, manifest)
        print(json.dumps({"verified": True, "sessions": len(manifest["entries"])}))
    else:
        globals()[args.action](args)


if __name__ == "__main__":
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    sys.dont_write_bytecode = True
    os.umask(0o077)
    try:
        main()
    except (OSError, RuntimeError, ValueError, sqlite3.Error, KeyError, TypeError, EOFError) as error:
        print(f"session-retention: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
