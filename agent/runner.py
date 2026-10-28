"""Windows-safe subprocess runner: threaded pipe readers, timeout, cancel, tree kill."""
from __future__ import annotations

import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set

IS_WINDOWS = os.name == "nt"

_ACTIVE: Set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()


class CommandNotFound(Exception):
    pass


class UnsafeArgument(Exception):
    pass


@dataclass
class ProcResult:
    status: str            # ok | timeout | cancelled | start_failed
    exit_code: Optional[int]
    stderr_tail: str = ""
    pid: Optional[int] = None
    killed_with: Optional[str] = None  # ctrl_break | taskkill | sigint | sigkill
    start_error: Optional[str] = None


def resolve_command(command: str) -> List[str]:
    """Resolve a CLI name/path into an argv prefix (never uses shell=True)."""
    path = shutil.which(command) or (command if os.path.isfile(command) else None)
    if path is None:
        raise CommandNotFound(f"command not found: {command!r} (check PATH or set agents.<id>.command)")
    ext = os.path.splitext(path)[1].lower()
    if IS_WINDOWS and ext == ".ps1":
        ps = shutil.which("pwsh") or shutil.which("powershell")
        if ps is None:
            raise CommandNotFound(f"{path} is a .ps1 script but no PowerShell executable was found")
        return [ps, "-NoProfile", "-NonInteractive", "-File", path]
    if ext == ".py":
        return [sys.executable, path]
    return [path]


def is_cmd_shim(prefix: List[str]) -> bool:
    return IS_WINDOWS and os.path.splitext(prefix[0])[1].lower() in (".cmd", ".bat")


_SHIM_BAD = set('"%^&|<>\r\n')


def check_shim_args(args: List[str]) -> None:
    """.cmd/.bat go through cmd.exe: refuse args that cmd.exe would reinterpret."""
    for a in args:
        if any(c in _SHIM_BAD for c in a):
            raise UnsafeArgument(f"argument not safe for a .cmd/.bat launcher: {a[:40]!r}")


def _popen(args: List[str], cwd: Optional[str], env: Dict[str, str]) -> subprocess.Popen:
    kw: Dict = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=cwd, env=env, shell=False)
    if IS_WINDOWS:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(args, **kw)


def terminate_tree(proc: subprocess.Popen, grace: float = 3.0) -> Optional[str]:
    """Stop a process this tool started (and its tree). Returns method used."""
    if proc.poll() is not None:
        return None
    method = None
    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            method = "ctrl_break"
        else:
            if os.getpgid(proc.pid) == proc.pid:
                os.killpg(proc.pid, signal.SIGINT)
            else:
                proc.send_signal(signal.SIGINT)
            method = "sigint"
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=grace)
        return method
    except subprocess.TimeoutExpired:
        pass
    # Escalate. `proc` is the Popen we created, so pid is ours; recheck it is still alive.
    if proc.poll() is None:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            method = "taskkill"
        else:
            try:
                if os.getpgid(proc.pid) == proc.pid:
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except OSError:
                pass
            method = "sigkill"
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    return method


def kill_all() -> None:
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
    for p in procs:
        terminate_tree(p, grace=1.0)


def _reader(stream, name: str, q: "queue.Queue") -> None:
    try:
        for raw in iter(stream.readline, b""):
            q.put((name, raw))
    except (OSError, ValueError):
        pass
    finally:
        q.put((name, None))


def _writer(proc: subprocess.Popen, data: bytes) -> None:
    try:
        proc.stdin.write(data)
        proc.stdin.flush()
    except (OSError, ValueError):
        pass  # child exited early; handled by exit status
    finally:
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass


def run_process(args: List[str], stdin_text: Optional[str], cwd: Optional[str],
                env: Optional[Dict[str, str]], timeout: float, cancel: threading.Event,
                on_line: Callable[[str, str], None]) -> ProcResult:
    """Run to completion. on_line(stream_name, text) is called in the caller's thread, in arrival order."""
    full_env = dict(os.environ if env is None else env)
    full_env.setdefault("PYTHONUTF8", "1")
    full_env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = _popen(args, cwd, full_env)
    except (OSError, ValueError) as e:
        return ProcResult("start_failed", None, start_error=f"{type(e).__name__}: {e}")
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)
    q: "queue.Queue" = queue.Queue()
    readers = [threading.Thread(target=_reader, args=(proc.stdout, "stdout", q), daemon=True),
               threading.Thread(target=_reader, args=(proc.stderr, "stderr", q), daemon=True)]
    for t in readers:
        t.start()
    data = (stdin_text or "").encode("utf-8")
    threading.Thread(target=_writer, args=(proc, data), daemon=True).start()

    status = "ok"
    killed_with = None
    deadline = time.monotonic() + timeout
    open_streams = 2
    stderr_tail: List[str] = []
    exit_seen_at: Optional[float] = None
    while open_streams > 0:
        if cancel.is_set():
            status = "cancelled"
            break
        if time.monotonic() >= deadline:
            status = "timeout"
            break
        try:
            name, raw = q.get(timeout=0.1)
        except queue.Empty:
            if proc.poll() is not None:
                # child gone but a grandchild may hold the pipes; don't wait forever
                exit_seen_at = exit_seen_at or time.monotonic()
                if time.monotonic() - exit_seen_at > 2.0:
                    break
            continue
        if raw is None:
            open_streams -= 1
            continue
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if text.startswith("﻿"):
            text = text[1:]
        if name == "stderr":
            stderr_tail.append(text)
            del stderr_tail[:-40]
        on_line(name, text)
    if status != "ok":
        killed_with = terminate_tree(proc)
    # drain whatever is left so late lines (e.g. final result) are not lost
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        terminate_tree(proc, grace=0.5)
    while True:
        try:
            name, raw = q.get_nowait()
        except queue.Empty:
            break
        if raw is not None and status == "ok":
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if name == "stderr":
                stderr_tail.append(text)
            on_line(name, text)
    for t, s in zip(readers, (proc.stdout, proc.stderr)):
        t.join(timeout=0.2)
        if not t.is_alive():   # closing under a blocked reader (grandchild holds the pipe) would hang
            try:
                s.close()
            except (OSError, ValueError):
                pass
    with _ACTIVE_LOCK:
        _ACTIVE.discard(proc)
    return ProcResult(status, proc.returncode, "\n".join(stderr_tail[-20:]), proc.pid, killed_with)
