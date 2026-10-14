"""Owner-private, serialized writes for the SSH selection state store."""

from __future__ import annotations

import errno
import functools
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class SshStateUnavailable(RuntimeError):
    """State is not known well enough to admit backend execution."""


_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_held = threading.local()


def state_path(path=None) -> Path:
    if path is not None:
        return Path(path).expanduser().absolute()
    from gateway.ssh_bindings import default_ssh_bindings_path
    return default_ssh_bindings_path().absolute()


def _try_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def store_lock(path=None):
    target = state_path(path)
    key = str(target)
    with _locks_guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_held, "paths", set())
        if key in held:
            yield target
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(target) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            deadline = time.monotonic() + 10
            while True:
                try:
                    _try_lock(fd)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN) or time.monotonic() >= deadline:
                        raise SshStateUnavailable("SSH state lock unavailable; execution blocked") from None
                    time.sleep(0.02)
            _held.paths = held | {key}
            try:
                yield target
            finally:
                _held.paths = held
                _unlock(fd)
        finally:
            os.close(fd)


def serialized_store(fn):
    @functools.wraps(fn)
    def call(*args, **kwargs):
        with store_lock(kwargs.get("path")) as target:
            return fn(*args, **{**kwargs, "path": target})
    return call


def write_state(data: dict, path=None) -> None:
    target = state_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, staged = tempfile.mkstemp(prefix=target.name + ".", suffix=".stage", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, target)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
