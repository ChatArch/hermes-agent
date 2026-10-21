"""Run a monthly backup/verify/cleanup cycle, retrying on idle days.

Run through with_managed_runtime.py. The daily launchd invocation does no work
after the current month's successful cycle. Scheduled runs wait for an idle gateway.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
import io
import json
import os
from pathlib import Path
import plistlib
import runpy
import signal
import sqlite3
import subprocess
import time

from hermes_constants import get_hermes_home
from hermes_platform.host.facts import os_family

HOME = get_hermes_home().resolve()
FOLDER = Path(__file__).resolve().parent
STATE = HOME / "backups/session-retention/cycle-state.json"
PRINCIPAL = f"session-retention:{os.getpid()}"


def save(value):
    value["updated_at"] = datetime.now(UTC).isoformat()
    tmp = STATE.with_suffix(".partial")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, STATE)
    sync_directory(STATE.parent)
    print(json.dumps(value), flush=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rotate_cycle_log(configured):
    if not configured:
        return
    path = HOME / "maintenance/session-retention/cycle.log"
    if Path(configured).resolve() != path or path.is_symlink():
        raise RuntimeError("Unexpected maintenance log path")
    if path.exists() and path.stat().st_size >= 1024 * 1024:
        for generation in (3, 2, 1):
            source = Path(f"{path}.{generation - 1}") if generation > 1 else path
            if source.exists():
                os.replace(source, Path(f"{path}.{generation}"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        os.close(fd)


def gateway_status():
    from gateway.control_socket import query_gateway_control
    status = query_gateway_control(HOME, "status", timeout=3)
    if status and status.get("pid") == status.get("answering_pid"):
        return status
    return None


def live(pid):
    from gateway.status import _pid_exists
    return _pid_exists(pid)


def gui_domain():
    if os_family() != "darwin":
        raise RuntimeError("The retention scheduler requires macOS launchd")
    return f"gui/{os.getuid()}"  # windows-footgun: ok - guarded macOS helper


def validate_service():
    plist = Path.home() / "Library/LaunchAgents/ai.hermes.gateway.plist"
    with plist.open("rb") as stream:
        job = plistlib.load(stream)
    configured = job.get("EnvironmentVariables", {}).get("HERMES_HOME")
    if not configured:
        raise RuntimeError("Gateway service must explicitly declare its Hermes home")
    configured_home = Path(configured).expanduser().resolve()
    if job.get("Label") != "ai.hermes.gateway" or configured_home != HOME:
        raise RuntimeError("Installed gateway service does not match retention home")


def restart_gateway(previous_pid=None):
    from hermes_cli.gateway import _parse_launchd_pid_from_print_output

    target = f"{gui_domain()}/ai.hermes.gateway"
    plist = Path.home() / "Library/LaunchAgents/ai.hermes.gateway.plist"
    def ensure_started():
        try:
            loaded = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, timeout=10, check=False)
            command = (["/bin/launchctl", "kickstart", target] if loaded.returncode == 0 else
                       ["/bin/launchctl", "bootstrap", gui_domain(), str(plist)])
            return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            # A transient launchctl failure must not abandon an already stopped gateway.
            return subprocess.CompletedProcess(["/bin/launchctl"], 1, stderr=str(error))
    result = ensure_started()
    last_error = result.stderr.strip() if result.returncode else ""
    deadline = time.monotonic() + 180
    next_kick = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = gateway_status()
        if result.returncode == 0 and status and status.get("pid") != previous_pid and status.get("gateway_state") == "running":
            feishu = status.get("platforms", {}).get("feishu", {})
            if feishu.get("state") == "connected" and feishu.get("writer_pid") == status["pid"]:
                try:
                    loaded = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
                    if loaded.returncode == 0 and _parse_launchd_pid_from_print_output(loaded.stdout) == status["pid"]:
                        return status["pid"]
                except (OSError, subprocess.TimeoutExpired) as error:
                    last_error = str(error)
        if time.monotonic() >= next_kick:
            result = ensure_started()
            if result.returncode:
                last_error = result.stderr.strip()
            next_kick = time.monotonic() + 5
        time.sleep(1)
    raise RuntimeError(f"Gateway restart requested; Feishu readiness was not confirmed within 180s: {last_error}")


def stop_gateway(pid):
    # Park launchd itself: signalling only its child lets KeepAlive race the DB gate.
    target = f"{gui_domain()}/ai.hermes.gateway"
    result = subprocess.run(["/bin/launchctl", "bootout", target], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=80, check=False)
    if result.returncode:
        raise RuntimeError(f"Could not park gateway supervisor: {result.stderr.strip()}")
    deadline = time.monotonic() + 65
    while live(pid) and time.monotonic() < deadline:
        time.sleep(1)
    if live(pid):
        raise RuntimeError("Gateway did not stop normally; cleanup refused")


def cleanup_routes(manifest_path):
    """Back up registry pointers before removing pointers to deleted history."""
    manifest_path = Path(manifest_path)
    receipt = manifest_path.parent / "apply.jsonl"
    deleted = {row["id"] for line in receipt.read_text(encoding="utf-8-sig").splitlines()
               if (row := json.loads(line)).get("status") == "deleted"}
    if not deleted:
        return 0
    registry = HOME / "sessions/sessions.json"
    index = json.loads(registry.read_text(encoding="utf-8-sig")) if registry.exists() else {}
    connection = sqlite3.connect(HOME / "state.db", timeout=3)
    connection.execute("PRAGMA synchronous=FULL")
    rows = []
    try:
        deleted = {sid for sid in deleted if not connection.execute(
            "SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()}
        removed = {k: v for k, v in index.items() if isinstance(v, dict) and v.get("session_id") in deleted}
        for scope, key, raw, updated in connection.execute(
                "SELECT scope,session_key,entry_json,updated_at FROM gateway_routing"):
            if json.loads(raw).get("session_id") in deleted:
                rows.append([scope, key, raw, updated])
        backup = manifest_path.parent / f"removed-routing-{time.time_ns()}.json"
        with backup.open("x", encoding="utf-8") as stream:
            json.dump({"registry": removed, "gateway_routing": rows}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(backup.parent)
        with connection:
            connection.executemany("DELETE FROM gateway_routing WHERE scope=? AND session_key=? AND entry_json=? AND updated_at=?",
                                   rows)
        if removed:
            tmp = registry.with_suffix(".retention-partial")
            with tmp.open("w", encoding="utf-8") as stream:
                json.dump({k: v for k, v in index.items() if k not in removed}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, registry)
            sync_directory(registry.parent)
    finally:
        connection.close()
    return len(rows)


def wait_for_drain(pid, allow_active):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = gateway_status()
        if not status or status["pid"] != pid:
            return None
        if status.get("gateway_state") == "draining":
            if allow_active or status.get("active_agents") == 0:
                return status
            return None
        time.sleep(1)
    return None


def timeout_handler(signum, frame):
    raise TimeoutError("Retention cycle exceeded its 30 minute budget")


def interrupt_handler(signum, frame):
    raise InterruptedError(f"Retention cycle interrupted by signal {signum}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", action="store_true", help="Run even if this month's cycle already succeeded")
    parser.add_argument("--manifest", type=Path, help="Reuse a completed backup; apply still enforces 90 idle days")
    parser.add_argument("--allow-active-stop", action="store_true", help="Manual maintenance only: normal shutdown may interrupt a turn")
    parser.add_argument("--log-file", type=Path, help="Private maintenance log; rotates at 1 MiB with three backups")
    args = parser.parse_args()
    if os_family() != "darwin":
        parser.error("The retention scheduler requires macOS launchd")
    import fcntl

    os.umask(0o077)
    os.nice(10)
    STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (STATE.parent / ".cycle.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    rotate_cycle_log(args.log_file)
    prior = json.loads(STATE.read_text(encoding="utf-8-sig")) if STATE.exists() else {}
    month = datetime.now().strftime("%Y-%m")
    if not args.now and prior.get("completed_month") == month:
        return
    state = {key: prior[key] for key in ("completed_month", "manifest", "attempt_month") if key in prior}
    state.update(phase="starting", retention_days=90)
    stopped = False
    marker_owned = False
    signal.signal(signal.SIGALRM, timeout_handler)  # windows-footgun: ok - main is macOS-only
    signal.signal(signal.SIGTERM, interrupt_handler)
    signal.signal(signal.SIGINT, interrupt_handler)
    signal.alarm(1800)
    from gateway.drain_control import drain_request_path, write_drain_request, clear_drain_request, read_drain_request
    def phase(name, **values):
        state.update(phase=name, **values)
        save(state)
    try:
        validate_service()
        status = gateway_status()
        if not status:
            phase("skipped_gateway_unavailable")
            return
        if status.get("gateway_state") != "running" or (status.get("active_agents") != 0 and not args.allow_active_stop):
            phase("skipped_gateway_busy")
            return
        if drain_request_path(HOME).exists():
            phase("skipped_existing_drain")
            return
        module = runpy.run_path(str(FOLDER / "session_retention.py"), run_name="retention_cycle_library")
        invoke = module["main"]
        as_of = datetime.now(UTC)
        cutoff = (as_of - timedelta(days=90)).isoformat()
        selection = ["--home", str(HOME), "--cutoff", cutoff,
                     "--include-stale-open", "--include-stale-messaging"]
        reuse = args.manifest or (Path(prior["manifest"]) if prior.get("manifest") and prior.get("attempt_month") == month else None)
        state["attempt_month"] = month
        if reuse and reuse.is_file():
            manifest = str(reuse.resolve())
        else:
            output = io.StringIO()
            with redirect_stdout(output):
                invoke(["plan", *selection])
            if not json.loads(output.getvalue())["count"]:
                phase("complete_no_expired_sessions", completed_month=month)
                return
            phase("backing_up")
            output = io.StringIO()
            with redirect_stdout(output):
                invoke(["backup", *selection])
            manifest = json.loads(output.getvalue())["manifest"]
        state["manifest"] = manifest
        phase("verifying_restore")
        verification_path = Path(manifest).parent / "restore-verified.json"
        verification = json.loads(verification_path.read_text(encoding="utf-8-sig")) if verification_path.exists() else {}
        if not (verification.get("verified") and verification.get("manifest_sha256") == module["sha_file"](Path(manifest))):
            invoke(["verify", "--manifest", manifest, "--verify-restore"])
        backup_cutoff = datetime.fromisoformat(json.loads(Path(manifest).read_text(encoding="utf-8-sig"))["cutoff"])
        cutoff = min(datetime.fromisoformat(cutoff), backup_cutoff).isoformat()
        status = gateway_status()
        if not status or status.get("gateway_state") != "running" or (status.get("active_agents") != 0 and not args.allow_active_stop):
            phase("backup_complete_cleanup_deferred_gateway_busy")
            return
        write_drain_request(home=HOME, principal=PRINCIPAL, suppress_notification=True)
        marker_owned = True
        fresh = wait_for_drain(status["pid"], args.allow_active_stop)
        if not fresh:
            phase("cleanup_deferred_drain_not_ready")
            return
        if (read_drain_request(home=HOME) or {}).get("principal") != PRINCIPAL:
            phase("cleanup_deferred_drain_owner_changed")
            return
        state["stopping_gateway_pid"] = fresh["pid"]
        phase("stopping_gateway")
        stopped = True
        stop_gateway(fresh["pid"])
        phase("applying_verified_retention")
        invoke(["apply", "--home", str(HOME), "--manifest", manifest, "--cutoff", cutoff])
        module["offline"](HOME / "state.db")
        state["removed_route_count"] = cleanup_routes(manifest)
        phase("cleanup_complete_awaiting_restart")
    except Exception as error:
        phase("failed", error=str(error))
        raise
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            if marker_owned:
                marker = read_drain_request(home=HOME) or {}
                if marker.get("principal") == PRINCIPAL:
                    clear_drain_request(home=HOME)
                    if drain_request_path(HOME).exists():
                        phase("failed", error="Could not clear owned drain marker")
        finally:
            try:
                if stopped:
                    try:
                        state["restarted_gateway_pid"] = restart_gateway(state.get("stopping_gateway_pid"))
                        if state["phase"] == "cleanup_complete_awaiting_restart":
                            state.update(phase="complete", completed_month=month)
                        save(state)
                    except Exception as error:
                        phase("failed_gateway_restart", restart_error=str(error))
                        raise
            finally:
                lock.close()


if __name__ == "__main__":
    main()
