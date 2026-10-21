# Session History Retention

`session_retention.py` is an operator tool for retaining recent Hermes history
and archiving older conversations before deletion. It does not install a
scheduler, stop a gateway, or enable native automatic pruning. Run it with the
checkout's configured Hermes Python environment, from the repository root:

```bash
python -m scripts.maintenance.session_retention --help
```

The default home comes from `get_hermes_home()`, including the active profile
scope. `--home /path/to/profile` selects another home explicitly; `--db` can
select its source database. Keep manifests and exported conversations private:
they contain the original conversation content and metadata. Backup directories
are owner-only, and files are created with mode `0600`.

## A Rolling 90-Day Policy

Retention uses the freshest activity time, rather than only the session's
creation date. New activity renews an old conversation's retention window.
The default action, `plan`, opens SQLite with `mode=ro` and `query_only` and
does not create output files:

```bash
python -m scripts.maintenance.session_retention plan --retention-days 90
```

Only ended sessions are candidates by default. `--include-stale-open` also
allows the native automation sources, such as CLI, cron, and subagent sessions.
`--include-stale-messaging` explicitly allows old open messaging and UI sessions
as well. These are deletion opt-ins, not required for planning ended history.

Pinned continuations, recent messages or creation, recent routing entries,
unexpired turn leases and compression locks, their descendants, and ancestors
of every retained conversation remain protected. Apply checks these guards
again before each deletion and keeps a session whose raw snapshot changed
after backup. Selected children are processed before their parents; a parent
with a surviving child is retained.

## Back Up, Verify, Then Apply Offline

Choose one timezone-aware reference time for a cycle. For example:

```bash
AS_OF='2026-10-21T09:00:00+08:00'
RUN_ID='retention-20261021'
python -m scripts.maintenance.session_retention backup \
  --retention-days 85 --as-of "$AS_OF" --run-id "$RUN_ID"
```

An 85-day backup provides a buffer for a later, more conservative 90-day apply.
Using 90 days for both steps is also supported. Pass the same explicit stale
session opt-ins to `backup` if those conversations are in scope. The backup
prints its manifest path; use that exact path for the remaining steps:

```bash
MANIFEST='/path/to/profile/backups/session-retention/retention-20261021/manifest.json'
python -m scripts.maintenance.session_retention verify \
  --manifest "$MANIFEST" --verify-restore
```

Backup can run while the gateway serves traffic: each session is exported from
its own read snapshot. Verification restores one session at a time into an
isolated temporary archive database, checks every raw field and the database's
foreign keys and integrity, and writes a receipt bound to the manifest hash.
The apply step requires that receipt and validates the archives again.

Before applying, stop the gateway and other clients of the chosen database
through their normal lifecycle, and confirm they have exited. Then run:

```bash
python -m scripts.maintenance.session_retention apply \
  --manifest "$MANIFEST" --retention-days 90 --as-of "$AS_OF"
```

Apply refuses foreign process holders and other in-process `SessionDB`
instances, including read-only handles. It also refuses a replaced source
database, changed canonical schema, or an apply cutoff newer than the backup
cutoff. There is no force option. Restart the gateway after apply, including
after an apply failure. A lifecycle runner must restore the service in its
cleanup path.

Apply commits and checkpoints one session at a time. A failure can leave
earlier sessions deleted and later ones untouched; `apply.jsonl` records each
completed decision, and every deleted session keeps its verified archive.
Old raw transcript files and routing mirrors are retained by this tool. A
lifecycle runner may separately back up and reconcile routing entries for the
IDs actually deleted; it must not remove routes solely because they appeared
in a plan.

## Recovery Is an Isolated Archive

Restore creates a new database and refuses an existing output. Choose a private
recovery directory. It never replaces the source database or registers a live
gateway route:

```bash
python -m scripts.maintenance.session_retention restore \
  --manifest "$MANIFEST" --output /path/to/private-recovery/archive.db
```

Use repeatable `--session-id SESSION_ID` arguments to recover selected entries.
The archive preserves raw sessions, all stored messages including inactive and
compacted rows, BLOB values, referenced prompts, and per-model usage accounting.
Ancestor dependencies contain metadata and prompts; their messages are present
only when those ancestors also have their own entries in the selected archive.

Derived search indexes, runtime locks, and gateway registry state are omitted.
This database is for inspecting or extracting historical conversations. Do not
copy it over a live profile's `state.db` or treat it as a fully initialized
Hermes store. Future unknown foreign-key dependencies fail backup closed, so
new cascade-deleted data cannot be silently omitted.

## Resource Boundaries

- No `VACUUM` or full-database rewrite runs. Deleted pages are reusable inside
  SQLite; the main file normally does not shrink, so deletion does not promise
  an equivalent increase in operating-system free space.
- Export streams one message row at a time. A single unusually large row still
  requires memory for that row. Restore commits after 100 rows or 4 MiB.
- At least 512 MiB of free space is required, plus conservative per-session
  headroom. Low space aborts work; apply also checks during SQLite execution.
- Backups are grouped by activity month under
  `<home>/backups/session-retention/<run-id>/`. The tool does not expire these
  archives automatically.

## Focused Tests

Use an existing configured test interpreter; no new environment is needed when
`HERMES_PYTHON` already has the checkout's test dependencies:

```bash
HERMES_PYTHON=/path/to/test/python scripts/run_tests.sh -j 1 \
  tests/scripts/maintenance/test_session_retention.py
```

The tests exercise isolated SQLite stores, full raw recovery, activity and
lineage guards, holder refusal, drift refusal, and real offline apply. They do
not open the operator's live store or start a gateway.

## macOS Scheduling

The installer supports the default gateway supervised in `gui/<uid>` as
`ai.hermes.gateway`. Its existing plist must explicitly declare the same
`HERMES_HOME` selected by the managed runtime. Run the installer from the
checkout after updating Hermes; pass the installed managed launcher explicitly
to both the helper and installer:

```bash
LAUNCHER='/absolute/path/to/managed/hermes'
/usr/bin/python3 scripts/maintenance/with_managed_runtime.py \
  --launcher "$LAUNCHER" scripts/maintenance/install_macos.py \
  --launcher "$LAUNCHER"
```

Installation copies all four maintenance scripts into the private
`<home>/maintenance/session-retention/` directory. It backs up previous scripts,
the maintenance plist and `config.yaml` under
`<home>/backups/retention-policy/<timestamp>/`. It refuses a running maintenance
job or held cycle lock. Failed updates restore previous files, configuration
and the previously loaded maintenance job. The gateway keeps serving during
installation.

The `ai.hermes.session-retention` LaunchAgent runs at **05:30 local time each
day**. The runner performs at most one successful cycle per calendar month,
skips a busy or unavailable gateway, and retries on later scheduled days.
After backup and restoration verification, an idle gateway acknowledges a
drain request before the runner temporarily unloads its supervisor for offline
cleanup. Its cleanup path restores the gateway and confirms Feishu readiness.
The scheduled command never uses `--allow-active-stop`.

The installer uses native `hermes config set` commands to set
`sessions.retention_days=90`, `auto_archive=false`, `auto_prune=false` and
`vacuum_after_prune=false`. Native pruning and soft archiving remain disabled;
the scheduled backup-before-delete cycle owns automatic cleanup. Conversations
with recent activity remain online for 90 days, exceeding a one-month minimum.
Backups remain available until explicitly removed.

The job runs at low priority, with a 240-second graceful-exit allowance.
Its runner writes a private `cycle.log` in the maintenance directory, rotating
at 1 MiB with three previous generations. launchd stdout and stderr go to
`/dev/null`, avoiding separate unbounded logs. Inspect the cycle status at
`<home>/backups/session-retention/cycle-state.json`.
