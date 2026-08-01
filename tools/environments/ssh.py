"""SSH remote execution environment with ControlMaster connection persistence."""

import hashlib
import json
import logging
import os
import posixpath
import shlex
import shutil
import subprocess
import tempfile
import time
from math import ceil
from pathlib import Path
from time import monotonic as _monotonic

from hermes_constants import get_hermes_home
from tools.environments.base import BaseEnvironment, _popen_bash
from tools.environments.file_sync import (
    FileSyncManager,
    iter_sync_files,
    quoted_mkdir_command,
    quoted_rm_command,
    unique_parent_dirs,
)

logger = logging.getLogger(__name__)


_MATERIALIZE_DENIED_SYSTEM_ROOTS = (
    "/etc", "/proc", "/sys", "/dev", "/root", "/boot", "/run",
    "/var/log", "/var/lib", "/var/run",
)
_MATERIALIZE_DENIED_HOME_DIRS = (
    ".ssh", ".aws", ".gnupg", ".kube", ".docker",
    ".config", ".azure", ".gcloud", "Library/Keychains",
)
_MATERIALIZE_HERMES_SECRET_FILES = (
    ".env", "auth.json", "auth.lock", "credentials", "config.yaml",
    ".anthropic_oauth.json", "google_token.json",
    "google_oauth_pending.json", "auth/google_oauth.json",
    "webhook_subscriptions.json", "cache/bws_cache.json",
    "cache/bws_cache.enc.json",
)
_MATERIALIZE_HERMES_SECRET_DIRS = ("pairing", "mcp-tokens")
_MATERIALIZE_TRUSTED_CACHE_DIRS = (
    "images", "audio", "videos", "documents", "screenshots",
)


def _posix_path_is_within(path: str, root: str) -> bool:
    clean_root = root.rstrip("/") or "/"
    return path == clean_root or path.startswith(clean_root + "/")


def _ensure_ssh_available() -> None:
    """Fail fast with a clear error when the SSH client is unavailable."""
    if not shutil.which("ssh"):
        raise RuntimeError(
            "SSH is not installed or not in PATH. Install OpenSSH client: apt install openssh-client"
        )
    if not shutil.which("scp"):
        raise RuntimeError(
            "SCP is not installed or not in PATH. Install OpenSSH client: apt install openssh-client"
        )


def _normalize_host_key_policy(policy: str | None) -> str:
    """Map Hermes-friendly host-key policy names to OpenSSH values."""
    normalized = (policy or "accept-new").strip().lower()
    aliases = {
        "strict": "yes",
        "ask": "ask",
        "accept-new": "accept-new",
        "accept_new": "accept-new",
        "insecure": "no",
        "no": "no",
        "yes": "yes",
    }
    return aliases.get(normalized, "accept-new")


class SSHEnvironment(BaseEnvironment):
    """Run commands on a remote machine over SSH.

    Spawn-per-call: every execute() spawns a fresh ``ssh ... bash -c`` process.
    Session snapshot preserves env vars across calls.
    CWD persists via in-band stdout markers.
    Uses SSH ControlMaster for connection reuse.
    """

    supports_file_materialization = True

    def __init__(self, host: str, user: str, cwd: str = "~",
                 timeout: int = 60, port: int = 22, key_path: str = "",
                 identities_only: bool = True, known_hosts_path: str | Path = "",
                 host_key_policy: str = "accept-new", persistent: bool = True,
                 sync_back_on_cleanup: bool = False,
                 sync_hermes_files: bool = False):
        super().__init__(cwd=cwd, timeout=timeout)
        self._persistent = bool(persistent)
        self._persist_session_state = self._persistent
        # SSH targets are real, long-lived machines with their own filesystem
        # and possibly their own Hermes installation.  Unlike disposable
        # container backends, default SSH execution must not upload local
        # ~/.hermes resources or pull remote ~/.hermes back.  Terminal and file
        # tools operate directly on the remote target; Hermes control-plane
        # state stays local.
        self._sync_hermes_files = bool(sync_hermes_files)
        self._sync_back_on_cleanup = bool(sync_back_on_cleanup)
        self._cleaned = False
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self.identities_only = identities_only
        self.known_hosts_path = (
            Path(known_hosts_path).expanduser()
            if known_hosts_path
            else get_hermes_home() / "ssh" / "known_hosts"
        )
        self.known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        self.known_hosts_path.touch(exist_ok=True)
        self.host_key_policy = _normalize_host_key_policy(host_key_policy)

        self.control_dir = self._make_control_dir()
        # Keep the socket filename short and deterministic so the full path
        # stays under the 104-byte sun_path limit that macOS enforces on
        # Unix domain sockets. A raw ``user@host:port`` — especially with an
        # IPv6 host — plus the 16-byte random suffix SSH appends in
        # ControlMaster mode easily exceeds the limit under macOS's
        # deeply-nested $TMPDIR (e.g. /var/folders/xx/yy/T/). Hashing the
        # triple keeps the path stable across reconnects so ControlMaster
        # reuse still works.
        _socket_id = hashlib.sha256(
            f"{user}@{host}:{port}".encode()
        ).hexdigest()[:16]
        self.control_socket = self.control_dir / f"{_socket_id}.sock"
        _ensure_ssh_available()
        self._establish_connection()
        self._remote_home = self._detect_remote_home()
        self._sync_manager = None
        if self._sync_hermes_files:
            self._ensure_remote_dirs()
            self._sync_manager = FileSyncManager(
                get_files_fn=lambda: iter_sync_files(f"{self._remote_home}/.hermes"),
                upload_fn=self._scp_upload,
                delete_fn=self._ssh_delete,
                bulk_upload_fn=self._ssh_bulk_upload,
                bulk_download_fn=self._ssh_bulk_download,
            )
            self._sync_manager.sync(force=True)

        self.init_session()

    @staticmethod
    def _control_dir_suffix() -> str:
        """Return a short local-user suffix for ControlMaster socket dirs."""
        getuid = getattr(os, "getuid", None)
        if callable(getuid):
            return str(getuid())
        user = os.getenv("USERNAME") or os.getenv("USER") or "user"
        return hashlib.sha256(user.encode()).hexdigest()[:8]

    @classmethod
    def _make_control_dir(cls) -> Path:
        """Create a local-user-isolated OpenSSH ControlMaster directory.

        ``/tmp`` is shared across local users on Linux/macOS.  A fixed global
        directory such as ``/tmp/hermes-ssh`` can be created by one gateway user
        and then block another user from binding a control socket.  Use a short
        per-local-user directory name to avoid cross-user permission pollution
        while preserving the macOS Unix-socket path length budget.
        """
        control_dir = Path(tempfile.gettempdir()) / f"hssh-{cls._control_dir_suffix()}"
        control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if control_dir.is_symlink():
            raise RuntimeError(f"SSH control socket directory must not be a symlink: {control_dir}")
        getuid = getattr(os, "getuid", None)
        expected_uid = getuid() if callable(getuid) else None
        if expected_uid is not None:
            actual_uid = control_dir.stat().st_uid
            if actual_uid != expected_uid:
                raise RuntimeError(
                    "SSH control socket directory is owned by another local user: "
                    f"{control_dir} (uid {actual_uid}, expected {expected_uid})"
                )
        try:
            control_dir.chmod(0o700)
        except OSError:
            logger.debug("SSH: failed to chmod control dir %s", control_dir, exc_info=True)
        if not os.access(control_dir, os.W_OK | os.X_OK):
            raise RuntimeError(f"SSH control socket directory is not writable: {control_dir}")
        final_mode = control_dir.stat().st_mode & 0o777
        if final_mode & 0o077:
            raise RuntimeError(
                "SSH control socket directory must be private to the local user: "
                f"{control_dir} (mode {final_mode:o})"
            )
        return control_dir

    def _build_ssh_command(self, extra_args: list | None = None) -> list:
        cmd = ["ssh"]
        cmd.extend(["-o", f"ControlPath={self.control_socket}"])
        cmd.extend(["-o", "ControlMaster=auto"])
        cmd.extend(["-o", "ControlPersist=300"])
        cmd.extend(["-o", "BatchMode=yes"])
        cmd.extend(["-o", f"UserKnownHostsFile={self.known_hosts_path}"])
        cmd.extend(["-o", f"StrictHostKeyChecking={self.host_key_policy}"])
        cmd.extend(["-o", "ConnectTimeout=10"])
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        if self.key_path:
            if self.identities_only:
                cmd.extend(["-o", "IdentitiesOnly=yes"])
            cmd.extend(["-i", self.key_path])
        if extra_args:
            cmd.extend(extra_args)
        cmd.append(f"{self.user}@{self.host}")
        return cmd

    def _establish_connection(self):
        cmd = self._build_ssh_command()
        cmd.append("echo 'SSH connection established'")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"SSH connection failed: {error_msg}")
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"SSH connection to {self.user}@{self.host} timed out")

    def _detect_remote_home(self) -> str:
        """Detect the remote user's home directory."""
        try:
            cmd = self._build_ssh_command()
            cmd.append("echo $HOME")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
            )
            home = result.stdout.strip()
            if home and result.returncode == 0:
                logger.debug("SSH: remote home = %s", home)
                return home
        except Exception:
            pass
        if self.user == "root":
            return "/root"
        return f"/home/{self.user}"

    def _normalize_materialize_source(self, source_path: str) -> str:
        """Return an absolute POSIX path in the remote filesystem."""
        raw = str(source_path or "").strip()
        if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
            raise ValueError("Remote materialization path is empty or contains control characters")
        if raw == "~":
            raw = self._remote_home
        elif raw.startswith("~/"):
            raw = f"{self._remote_home.rstrip('/')}/{raw[2:]}"
        if not posixpath.isabs(raw):
            raise ValueError("Remote materialization requires an absolute path or ~/ path")
        normalized = posixpath.normpath(raw)
        # POSIX permits implementation-defined semantics for exactly two
        # leading slashes. SSH targets are addressed from one filesystem root,
        # so collapse them before prefix-based security checks.
        if normalized.startswith("//"):
            normalized = "/" + normalized.lstrip("/")
        return normalized

    def _materialize_source_is_denied(self, source_path: str) -> bool:
        remote_home = posixpath.normpath(self._remote_home)
        for root in _MATERIALIZE_DENIED_SYSTEM_ROOTS:
            # A root SSH user may export ordinary files from their own home;
            # the more-specific credential paths below remain denied.
            if root == remote_home:
                continue
            if _posix_path_is_within(source_path, root):
                return True
        for relative in _MATERIALIZE_DENIED_HOME_DIRS:
            if _posix_path_is_within(
                source_path,
                posixpath.join(remote_home, relative),
            ):
                return True
        hermes_home = posixpath.join(remote_home, ".hermes")
        for relative in _MATERIALIZE_HERMES_SECRET_FILES:
            if _posix_path_is_within(
                source_path,
                posixpath.join(hermes_home, relative),
            ):
                return True
        for relative in _MATERIALIZE_HERMES_SECRET_DIRS:
            if _posix_path_is_within(
                source_path,
                posixpath.join(hermes_home, relative),
            ):
                return True
        return False

    def _materialize_source_is_trusted_cache(self, source_path: str) -> bool:
        cache_root = posixpath.join(self._remote_home, ".hermes", "cache")
        return any(
            _posix_path_is_within(source_path, posixpath.join(cache_root, name))
            for name in _MATERIALIZE_TRUSTED_CACHE_DIRS
        )

    def _inspect_materialize_source(self, source_path: str, timeout: int) -> dict:
        """Resolve symlinks and stat a remote file without transferring bytes."""
        script = (
            "import json, os, stat, sys\n"
            "path = os.path.realpath(sys.argv[1])\n"
            "try:\n"
            "    info = os.stat(path)\n"
            "except OSError as exc:\n"
            "    print(json.dumps({'error': str(exc)}))\n"
            "    raise SystemExit(2)\n"
            "if not stat.S_ISREG(info.st_mode):\n"
            "    print(json.dumps({'error': 'not a regular file'}))\n"
            "    raise SystemExit(3)\n"
            "print(json.dumps({'source_path': path, 'size': info.st_size, "
            "'mtime': info.st_mtime, 'device': info.st_dev, 'inode': info.st_ino}))\n"
        )
        cmd = self._build_ssh_command()
        cmd.append(
            f"python3 -c {shlex.quote(script)} {shlex.quote(source_path)}"
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Remote file metadata check timed out") from exc

        lines = [line for line in result.stdout.splitlines() if line.strip()]
        payload = None
        if lines:
            try:
                payload = json.loads(lines[-1])
            except json.JSONDecodeError:
                payload = None
        if result.returncode != 0 or not isinstance(payload, dict):
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("error") or "")
            if not detail:
                detail = result.stderr.strip() or "remote metadata check failed"
            raise RuntimeError(detail)
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        try:
            return {
                "source_path": str(payload["source_path"]),
                "size": int(payload["size"]),
                "mtime": float(payload["mtime"]),
                "device": int(payload["device"]),
                "inode": int(payload["inode"]),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Remote file metadata response was invalid") from exc

    def materialize_file(
        self,
        source_path: str,
        destination_path: str | Path,
        *,
        max_bytes: int,
        timeout: int,
        require_recent_seconds: float | None = None,
    ) -> dict:
        """Copy one safe, bounded remote file onto the gateway host."""
        if max_bytes <= 0:
            raise ValueError("Remote file materialization requires a positive size limit")
        if timeout <= 0:
            raise ValueError("Remote file materialization requires a positive timeout")
        deadline = _monotonic() + timeout

        def _remaining_timeout() -> int:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise RuntimeError("Remote file materialization timed out")
            return max(1, ceil(remaining))

        requested = self._normalize_materialize_source(source_path)
        if self._materialize_source_is_denied(requested):
            raise PermissionError("Remote materialization path is protected")

        metadata = self._inspect_materialize_source(requested, _remaining_timeout())
        canonical = self._normalize_materialize_source(metadata["source_path"])
        if self._materialize_source_is_denied(canonical):
            raise PermissionError("Resolved remote materialization path is protected")

        size = int(metadata["size"])
        if size > max_bytes:
            raise ValueError(
                f"Remote file is too large to materialize ({size} bytes > {max_bytes} bytes)"
            )
        if (
            require_recent_seconds is not None
            and require_recent_seconds > 0
            and not self._materialize_source_is_trusted_cache(canonical)
            and (time.time() - float(metadata["mtime"])) > require_recent_seconds
        ):
            raise PermissionError("Remote file is not recent enough for strict media delivery")

        transfer_timeout = _remaining_timeout()

        destination = Path(destination_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        transfer_script = (
            "import os, stat, sys\n"
            "path = sys.argv[1]\n"
            "limit, expected_dev, expected_ino, expected_size = map(int, sys.argv[2:])\n"
            "flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)\n"
            "try:\n"
            "    fd = os.open(path, flags)\n"
            "except OSError as exc:\n"
            "    print(str(exc), file=sys.stderr)\n"
            "    raise SystemExit(2)\n"
            "with os.fdopen(fd, 'rb') as source:\n"
            "    info = os.fstat(source.fileno())\n"
            "    if (not stat.S_ISREG(info.st_mode) or info.st_dev != expected_dev "
            "or info.st_ino != expected_ino or info.st_size != expected_size):\n"
            "        print('remote file changed after metadata check', file=sys.stderr)\n"
            "        raise SystemExit(3)\n"
            "    remaining = limit + 1\n"
            "    while remaining > 0:\n"
            "        chunk = source.read(min(65536, remaining))\n"
            "        if not chunk:\n"
            "            break\n"
            "        sys.stdout.buffer.write(chunk)\n"
            "        remaining -= len(chunk)\n"
        )
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination.name}.",
                suffix=".part",
                dir=destination.parent,
                delete=False,
            ) as staged:
                temp_path = Path(staged.name)
                cmd = self._build_ssh_command()
                cmd.append(
                    "python3 -c "
                    f"{shlex.quote(transfer_script)} "
                    f"{shlex.quote(canonical)} {max_bytes} "
                    f"{int(metadata['device'])} {int(metadata['inode'])} {size}"
                )
                try:
                    result = subprocess.run(
                        cmd,
                        stdout=staged,
                        stderr=subprocess.PIPE,
                        timeout=transfer_timeout,
                        stdin=subprocess.DEVNULL,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError("Remote file transfer timed out") from exc

            if result.returncode != 0:
                stderr = result.stderr.decode(errors="replace").strip()
                raise RuntimeError(stderr or "remote file transfer failed")

            actual_size = temp_path.stat().st_size
            if actual_size > max_bytes:
                raise ValueError(
                    f"Remote file grew beyond the transfer limit ({actual_size} bytes > {max_bytes} bytes)"
                )
            if actual_size != size:
                raise RuntimeError(
                    f"Remote file changed during transfer (expected {size} bytes, received {actual_size})"
                )
            os.replace(temp_path, destination)
            temp_path = None
            return {
                "source_path": canonical,
                "path": str(destination),
                "size": actual_size,
                "mtime": float(metadata["mtime"]),
            }
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # File sync (via FileSyncManager)
    # ------------------------------------------------------------------

    def _ensure_remote_dirs(self) -> None:
        """Create base ~/.hermes directory tree on remote in one SSH call."""
        base = f"{self._remote_home}/.hermes"
        dirs = [base, f"{base}/skills", f"{base}/credentials", f"{base}/cache"]
        cmd = self._build_ssh_command()
        cmd.append(quoted_mkdir_command(dirs))
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )

    # _get_sync_files provided via iter_sync_files in FileSyncManager init

    def _scp_upload(self, host_path: str, remote_path: str) -> None:
        """Upload a single file via scp over ControlMaster."""
        parent = str(Path(remote_path).parent)
        mkdir_cmd = self._build_ssh_command()
        mkdir_cmd.append(f"mkdir -p {shlex.quote(parent)}")
        subprocess.run(
            mkdir_cmd,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )

        scp_cmd = [
            "scp",
            "-o",
            f"ControlPath={self.control_socket}",
            "-o",
            f"UserKnownHostsFile={self.known_hosts_path}",
            "-o",
            f"StrictHostKeyChecking={self.host_key_policy}",
        ]
        if self.port != 22:
            scp_cmd.extend(["-P", str(self.port)])
        if self.key_path:
            if self.identities_only:
                scp_cmd.extend(["-o", "IdentitiesOnly=yes"])
            scp_cmd.extend(["-i", self.key_path])
        scp_cmd.extend([host_path, f"{self.user}@{self.host}:{remote_path}"])
        result = subprocess.run(
            scp_cmd,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError(f"scp failed: {result.stderr.strip()}")

    def _ssh_bulk_upload(self, files: list[tuple[str, str]]) -> None:
        """Upload many files in a single tar-over-SSH stream.

        Pipes ``tar c`` on the local side through an SSH connection to
        ``tar x`` on the remote, transferring all files in one TCP stream
        instead of spawning a subprocess per file.  Directory creation is
        batched into a single ``mkdir -p`` call beforehand.

        Typical improvement: ~580 files goes from O(N) scp round-trips
        to a single streaming transfer.
        """
        if not files:
            return

        base = f"{self._remote_home}/.hermes"
        parents = unique_parent_dirs(files)
        if parents:
            cmd = self._build_ssh_command()
            cmd.append(quoted_mkdir_command(parents))
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                raise RuntimeError(f"remote mkdir failed: {result.stderr.strip()}")

        # Symlink staging avoids fragile GNU tar --transform rules.
        # On Windows without Developer Mode, symlink creation raises
        # OSError with winerror 1314 (privilege not held).  Catch only
        # that specific error and fall back to a plain copy; all other
        # OSErrors (e.g. disk full, bad path) are re-raised as normal.
        with tempfile.TemporaryDirectory(prefix="hermes-ssh-bulk-") as staging:
            for host_path, remote_path in files:
                try:
                    rel_remote = os.path.relpath(remote_path, base)
                except ValueError as exc:
                    raise RuntimeError(
                        f"remote path {remote_path!r} is not under sync base {base!r}"
                    ) from exc

                if rel_remote == "." or rel_remote.startswith("../"):
                    raise RuntimeError(
                        f"remote path {remote_path!r} escapes sync base {base!r}"
                    )

                staged = os.path.join(staging, rel_remote)
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                try:
                    os.symlink(os.path.abspath(host_path), staged)
                except OSError as e:
                    # WinError 1314: symlink privilege not held (Windows without Dev Mode)
                    if getattr(e, "winerror", None) == 1314:
                        shutil.copy2(host_path, staged)
                    else:
                        raise

            tar_cmd = ["tar", "-chf", "-", "-C", staging, "."]
            ssh_cmd = self._build_ssh_command()
            # --no-overwrite-dir prevents tar from overwriting the mode of
            # existing directories (e.g. /home/<user>) with the staging
            # directory's mode.  Without this, a umask 002 produces 0775
            # dirs which breaks sshd StrictModes (refuses authorized_keys).
            ssh_cmd.append(f"tar xf - --no-overwrite-dir -C {shlex.quote(base)}")

            tar_proc = subprocess.Popen(
                tar_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                ssh_proc = subprocess.Popen(
                    ssh_cmd, stdin=tar_proc.stdout, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except Exception:
                tar_proc.kill()
                tar_proc.wait()
                raise

            # Allow tar_proc to receive SIGPIPE if ssh_proc exits early
            tar_proc.stdout.close()

            try:
                _, ssh_stderr = ssh_proc.communicate(timeout=120)
                # Use communicate() instead of wait() to drain stderr and
                # avoid deadlock if tar produces more than PIPE_BUF of errors.
                tar_stderr_raw = b""
                if tar_proc.poll() is None:
                    _, tar_stderr_raw = tar_proc.communicate(timeout=10)
                else:
                    tar_stderr_raw = tar_proc.stderr.read() if tar_proc.stderr else b""
            except subprocess.TimeoutExpired:
                tar_proc.kill()
                ssh_proc.kill()
                tar_proc.wait()
                ssh_proc.wait()
                raise RuntimeError("SSH bulk upload timed out")

            if tar_proc.returncode != 0:
                raise RuntimeError(
                    f"tar create failed (rc={tar_proc.returncode}): "
                    f"{tar_stderr_raw.decode(errors='replace').strip()}"
                )
            if ssh_proc.returncode != 0:
                raise RuntimeError(
                    f"tar extract over SSH failed (rc={ssh_proc.returncode}): "
                    f"{ssh_stderr.decode(errors='replace').strip()}"
                )

        logger.debug("SSH: bulk-uploaded %d file(s) via tar pipe", len(files))

    def _ssh_bulk_download(self, dest: Path) -> None:
        """Download remote .hermes/ as a tar archive."""
        # Tar from / with the full path so archive entries preserve absolute
        # paths (e.g. home/user/.hermes/skills/f.py), matching _pushed_hashes keys.
        rel_base = f"{self._remote_home}/.hermes".lstrip("/")
        ssh_cmd = self._build_ssh_command()
        ssh_cmd.append(f"tar cf - -C / {shlex.quote(rel_base)}")
        with open(dest, "wb") as f:
            result = subprocess.run(
                ssh_cmd,
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.PIPE,
                timeout=120,
            )
        if result.returncode != 0:
            raise RuntimeError(f"SSH bulk download failed: {result.stderr.decode(errors='replace').strip()}")

    def _ssh_delete(self, remote_paths: list[str]) -> None:
        """Batch-delete remote files in one SSH call."""
        cmd = self._build_ssh_command()
        cmd.append(quoted_rm_command(remote_paths))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError(f"remote rm failed: {result.stderr.strip()}")

    def _before_execute(self) -> None:
        """No-op by default; SSH targets must not receive local Hermes sync."""
        if self._sync_manager is not None:
            self._sync_manager.sync()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """Spawn an SSH process that runs bash on the remote host."""
        cmd = self._build_ssh_command()
        if login:
            cmd.extend(["bash", "-l", "-c", shlex.quote(cmd_string)])
        else:
            cmd.extend(["bash", "-c", shlex.quote(cmd_string)])

        return _popen_bash(cmd, stdin_data)

    def cleanup(self):
        if getattr(self, "_cleaned", False):
            return
        self._cleaned = True

        if self._sync_manager and self._sync_back_on_cleanup:
            logger.info("SSH: syncing files from sandbox...")
            self._sync_manager.sync_back()

        if self.control_socket.exists():
            try:
                cmd = ["ssh", "-o", f"ControlPath={self.control_socket}",
                       "-O", "exit", f"{self.user}@{self.host}"]
                subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=5,
                    stdin=subprocess.DEVNULL,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                self.control_socket.unlink()
            except OSError:
                pass
