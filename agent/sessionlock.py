"""One writer per Agent Session: a lock file inside the session directory.

The file holds {pid, host, token, created}; the owner refreshes its mtime as a heartbeat. A lock whose owner process
is gone (crash, kill, power loss) is stale and is taken over. A lock whose owner is still alive is NEVER taken over,
even when its heartbeat looks old (the process may be stuck, or the OS may have reused the pid): the caller is told
which file to delete by hand instead. No signals are sent to the owner (on Windows os.kill with a non-console signal
would terminate it), liveness is checked read-only.
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
HEARTBEAT_STALE_SEC = 120      # only used for the wording of the "still alive" message


class SessionLocked(Exception):
    def __init__(self, message: str, info: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.info = info or {}


def pid_alive(pid: int) -> bool:
    """Read-only liveness probe. Never signals the process."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5          # ERROR_ACCESS_DENIED: exists but protected
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == 259                     # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)                                  # POSIX: signal 0 only checks existence
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class SessionLock:
    def __init__(self, session_dir: Path):
        self.path = Path(session_dir) / LOCK_NAME
        self.token = uuid.uuid4().hex
        self.held = False

    # -- helpers
    def _payload(self) -> str:
        return json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "token": self.token,
                           "created": time.time()})

    def read(self) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _create(self) -> bool:
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self._payload())
        return True

    # -- api
    def acquire(self) -> None:
        if self._create():
            self.held = True
            return
        info = self.read()
        if info is None:
            # unreadable / half-written: if it is brand new its writer is probably mid-write, so do not touch it
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                age = 1e9
            if age < 5:
                raise SessionLocked(f"session lock {self.path} is being written by another process; try again", {})
        else:
            pid = info.get("pid")
            same_host = info.get("host") == socket.gethostname()
            if not same_host:
                raise SessionLocked(f"session is locked by another machine ({info.get('host')}); "
                                    f"if that is wrong delete {self.path}", info)
            if isinstance(pid, int) and pid_alive(pid):
                try:
                    beat = time.time() - self.path.stat().st_mtime
                except OSError:
                    beat = 0
                hint = ("" if beat < HEARTBEAT_STALE_SEC else
                        f" (no heartbeat for {int(beat)}s: it may be stuck or the pid was reused; only if you are sure, "
                        f"delete {self.path})")
                raise SessionLocked(f"session is in use by another agent process (pid {pid}){hint}", info)
        # stale: owner is gone. Take it over exactly once.
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            raise SessionLocked(f"stale session lock {self.path} could not be removed: {e}", info or {})
        if not self._create():
            raise SessionLocked("another agent process took the session at the same moment", self.read() or {})
        self.held = True

    def heartbeat(self) -> None:
        if not self.held:
            return
        try:
            os.utime(self.path, None)
        except OSError:
            pass

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        info = self.read()
        if info is not None and info.get("token") != self.token:
            return                                       # someone else owns it now; leave it alone
        try:
            self.path.unlink()
        except OSError:
            pass
