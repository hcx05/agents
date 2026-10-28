"""One writer per Agent Session, enforced by the operating system.

The lock is an OS file lock (fcntl.flock on POSIX, msvcrt.locking on Windows) on `session.lock`, held on an open file
for as long as the owner lives. That removes the two classic problems of "lock file + pid" schemes:

* No stale locks and therefore no takeover race: when the owner exits or is killed, the OS drops its lock by itself,
  and the next process simply acquires it. Two processes can never both "take over" the same dead owner, because the
  OS grants an exclusive lock to exactly one of them.
* A lock can never be deleted by someone who does not own it: the file is never unlinked (not on release, not on
  takeover). Releasing only unlocks this object's own descriptor.

The file's text (pid, host, token, time) is informational, it is what a refused process shows to its user. Locking is
non-blocking: a second writer is refused immediately. The locked byte lives far beyond the text so that other
processes can still read the info on Windows, where locked ranges are unreadable.
"""
from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

LOCK_NAME = "session.lock"
_LOCK_OFFSET = 1 << 30          # byte that is locked; the info text lives at offset 0


class SessionLocked(Exception):
    def __init__(self, message: str, info: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.info = info or {}


if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
        finally:
            os.lseek(fd, 0, os.SEEK_SET)

    def _unlock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def _read_info(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


class SessionLock:
    def __init__(self, session_dir: Path):
        self.path = Path(session_dir) / LOCK_NAME
        self.token = uuid.uuid4().hex
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def read(self) -> Optional[Dict[str, Any]]:
        return _read_info(self.path)

    def acquire(self) -> None:
        if self._fd is not None:
            raise SessionLocked("this process already holds the lock", self.read())
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)       # non-inheritable: children never keep it alive
        if not _try_lock(fd):
            os.close(fd)
            info = _read_info(self.path) or {}
            who = f" (pid {info['pid']} on {info.get('host', '?')})" if isinstance(info.get("pid"), int) else ""
            raise SessionLocked(f"session is in use by another agent process{who}", info)
        self._fd = fd
        try:                                                              # we own the lock now: describe the owner
            payload = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "token": self.token,
                                  "created": time.time()}).encode("utf-8")
            os.ftruncate(fd, 0)
            os.write(fd, payload)
            os.fsync(fd)
        except OSError:
            pass                                                          # the info is a courtesy; the lock is what counts

    def heartbeat(self) -> None:
        """Nothing to do: the OS lock lives exactly as long as this process. Kept so callers need no special case."""

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return                                                        # never held, or already released: touch nothing
        try:
            os.ftruncate(fd, 0)                                           # clear the info while the lock is still ours
        except OSError:
            pass
        _unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass

    @staticmethod
    def is_locked(session_dir: Path) -> bool:
        """True when some process holds the lock right now (probe: take it and let go again, create nothing)."""
        path = Path(session_dir) / LOCK_NAME
        try:
            fd = os.open(str(path), os.O_RDWR)
        except OSError:
            return False
        try:
            if _try_lock(fd):
                _unlock(fd)
                return False
            return True
        finally:
            os.close(fd)
