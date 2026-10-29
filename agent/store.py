"""Session storage: state.json (atomic), events.jsonl, limited/redacted raw logs."""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# 1: first resumable layout; 2: + config/cwd/mail/preamble_sent; 3: + `import` (native Claude session import, with its own
# restore rules). Older versions load and are upgraded in memory; a program that only knows an older version refuses v3.
STATE_VERSION = 3
RAW_LINE_MAX = 2000
RAW_FILE_MAX = 2 * 1024 * 1024
MAX_FIRST_MSG_SCAN_BYTES = 16 * 1024 * 1024   # safety cap when scanning a session that has no 'Me' message at all
MAX_FIRST_MSG_CHARS = 65536                   # cap single first-message string kept in memory for search

# Cross-platform valid timestamp range (POSIX + Windows MSVCRT localtime/strftime: 1970-01-01 .. 3000-01-01)
_MIN_VALID_TS = 0.0
_MAX_VALID_TS = 32503680000.0

_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)(\"?(?:api[_-]?key|token|secret|password|passwd|authorization)\"?\s*[:=]\s*)\"?[^\s\",}]{6,}\"?"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
]
_SECRET_ENV = re.compile(r"(?i)(key|token|secret|password|passwd|credential|auth)")


def redact(text: str) -> str:
    if not text:
        return text
    for name, val in os.environ.items():
        if len(val) >= 8 and _SECRET_ENV.search(name) and val in text:
            text = text.replace(val, "[REDACTED]")
    for p in _PATTERNS:
        if p.groups:
            text = p.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = p.sub("[REDACTED]", text)
    return text


def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    last: Optional[Exception] = None
    for _ in range(8):  # Windows: replace can fail briefly while AV/indexer holds the file
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last = e
            time.sleep(0.05)
    raise last  # type: ignore[misc]


class StoreError(Exception):
    pass


# Session ids this program creates look like 20261028-131316-be0f76. Anything else - separators, dots, drive letters,
# NUL, whitespace, over-long text - is refused before it can reach the file system.
SESSION_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,99}$")


def safe_session_dir(sessions_root: Path, session_id: str) -> Path:
    """`sessions_root/session_id`, guaranteed to be a direct child of sessions_root. Raises StoreError otherwise
    (path traversal, absolute paths, drive letters, odd characters, or a symlink/junction leading outside)."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise StoreError(f"invalid session id {session_id!r}: use an id from `agent sessions`")
    root = Path(sessions_root)
    target = root / session_id
    try:
        if target.resolve().parent != root.resolve():
            raise StoreError(f"session id {session_id!r} does not point inside the sessions folder")
    except OSError as e:
        raise StoreError(f"session id {session_id!r} cannot be resolved: {e}")
    return target


def _valid_ts(val: Any) -> Optional[float]:
    """Validate that `val` is a finite timestamp within cross-platform localtime/strftime limits."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    try:
        f = float(val)
    except (OverflowError, ValueError, TypeError):
        return None
    if not math.isfinite(f) or f < _MIN_VALID_TS or f > _MAX_VALID_TS:
        return None
    try:
        tm = time.localtime(f)
        time.strftime("%Y-%m-%d %H:%M:%S", tm)
    except (OSError, OverflowError, ValueError, TypeError):
        return None
    return f


class Store:
    def __init__(self, sessions_root: Path, session_id: Optional[str] = None, state: Optional[Dict[str, Any]] = None):
        self.id = session_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.dir = sessions_root / self.id
        (self.dir / "raw").mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.events_path = self.dir / "events.jsonl"
        self._lock = threading.RLock()
        self._seq = 0
        self._raw_sizes: Dict[str, int] = {}
        self.loaded_version: Optional[int] = None      # set when an existing session was opened
        self.state: Dict[str, Any] = state if state is not None else {
            "version": STATE_VERSION, "id": self.id, "created": time.time(), "cwd": None, "state": "idle",
            "agents": {}, "tasks": {}, "awaiting_user": None, "manager_turns": 0, "last_error": None,
            "config": None, "mail": {},
        }

    @classmethod
    def open_existing(cls, sessions_root: Path, session_id: str, repair: bool = True) -> "Store":
        """Open a saved session for resuming. Old (version 1) states are upgraded in memory; nothing is written
        until the engine saves. A damaged state.json or a half-written last event line never raises past StoreError."""
        d = safe_session_dir(Path(sessions_root), session_id)
        if not (d / "state.json").is_file():
            raise StoreError(f"session {session_id!r} not found under {sessions_root}")
        try:
            st = json.loads((d / "state.json").read_text(encoding="utf-8"))
            if not isinstance(st, dict):
                raise ValueError("state.json is not an object")
        except (OSError, ValueError) as e:
            raise StoreError(f"state.json of session {session_id!r} is unreadable ({e}); it was not modified")
        ver = st.get("version", 1)
        if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1 or ver > STATE_VERSION:
            raise StoreError(f"session {session_id!r} was written by a newer version of agent (state version {ver!r})")
        for key, default in (("agents", {}), ("tasks", {}), ("mail", {}), ("config", None)):
            if not isinstance(st.get(key), type(default)) and not (default is None and st.get(key) is None):
                st[key] = default
        store = cls(Path(sessions_root), session_id, state=st)
        store.loaded_version = ver
        store.state["version"] = STATE_VERSION
        if repair:
            store._repair_events_tail()
        store._seq = max([e.get("seq", 0) for e in cls.read_events(d) if isinstance(e.get("seq"), int)] or [0])
        for p in (d / "raw").glob("*.jsonl"):
            try:
                store._raw_sizes[p.stem] = p.stat().st_size
            except OSError:
                pass
        return store

    def _repair_events_tail(self) -> None:
        """A crash can leave the last line unterminated; appending to it would glue two records together."""
        try:
            with open(self.events_path, "rb+") as f:
                f.seek(0, os.SEEK_END)
                if f.tell() == 0:
                    return
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    f.write(b"\n")
        except OSError:
            pass

    @staticmethod
    def iter_events(session_dir: Path):
        """Parseable event records in file order. Blank, truncated or non-object lines are skipped."""
        try:
            with open(Path(session_dir) / "events.jsonl", "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        yield rec
        except OSError:
            return

    @staticmethod
    def first_user_message(session_dir: Path, max_scan_bytes: int = MAX_FIRST_MSG_SCAN_BYTES) -> str:
        """Read-only scan for the first user ('Me') chat message in `events.jsonl`.
        Uses fast substring pre-filtering before JSON parsing and stops immediately on the first match."""
        events_path = Path(session_dir) / "events.jsonl"
        scanned = 0
        try:
            with open(events_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    scanned += len(line)
                    if '"Me"' in line and '"chat"' in line:
                        s = line.strip()
                        if s:
                            try:
                                rec = json.loads(s)
                            except ValueError:
                                rec = None
                            if isinstance(rec, dict) and rec.get("type") == "chat" and rec.get("sender") == "Me":
                                txt = str(rec.get("text", "")).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
                                return txt[:MAX_FIRST_MSG_CHARS]
                    if scanned >= max_scan_bytes:
                        break
        except OSError:
            return ""
        return ""

    @staticmethod
    def read_events(session_dir: Path) -> List[Dict[str, Any]]:
        return list(Store.iter_events(session_dir))

    # -- state
    def save(self) -> None:
        with self._lock:
            self.state["updated"] = time.time()
            atomic_write_json(self.state_path, self.state)

    def update(self, **fields: Any) -> None:
        with self._lock:
            self.state.update(fields)
            self.save()

    def set_agent(self, agent_id: str, **fields: Any) -> None:
        with self._lock:
            self.state["agents"].setdefault(agent_id, {}).update(fields)
            self.save()

    def set_task(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            self.state["tasks"].setdefault(task_id, {}).update(fields)
            self.save()

    @staticmethod
    def load_state(session_dir: Path) -> Dict[str, Any]:
        """Read a saved session state (foundation for a future /resume)."""
        return json.loads((session_dir / "state.json").read_text(encoding="utf-8"))

    @staticmethod
    def list_sessions(sessions_root: Path) -> List[str]:
        if not sessions_root.exists():
            return []
        return sorted(p.name for p in sessions_root.iterdir() if (p / "state.json").exists())

    @staticmethod
    def summaries(sessions_root: Path) -> List[Dict[str, Any]]:
        """One dict per saved session (oldest first) for `agent sessions`. Damaged sessions are listed, not fatal."""
        from .sessionlock import SessionLock
        out: List[Dict[str, Any]] = []
        for sid in Store.list_sessions(sessions_root):
            d = Path(sessions_root) / sid
            row: Dict[str, Any] = {"id": sid, "ok": True, "version": None, "created": None, "updated": None,
                                   "cwd": None, "state": None, "agents": "", "locked": False, "first": "",
                                   "import_source": None}
            try:
                st = json.loads((d / "state.json").read_text(encoding="utf-8"))
                if not isinstance(st, dict):
                    raise ValueError("state.json is not an object")
                created = _valid_ts(st.get("created"))
                updated = _valid_ts(st.get("updated"))
                if updated is None:
                    updated = created
                row.update(version=st.get("version", 1), created=created, updated=updated,
                           cwd=st.get("cwd") if isinstance(st.get("cwd"), str) else None,
                           state=st.get("state") if isinstance(st.get("state"), str) else None,
                           agents="+".join(str(a.get("cli", "?")) for a in (st.get("agents") or {}).values()))
                imp = st.get("import")
                if isinstance(imp, dict) and isinstance(imp.get("source_id"), str):
                    row["import_source"] = imp["source_id"]
            except (OSError, ValueError, AttributeError, TypeError):
                row["ok"] = False
            row["locked"] = SessionLock.is_locked(d)
            first_full = Store.first_user_message(d)
            row["first"] = first_full[:50]
            out.append(row)
        return out

    # -- logs
    def log(self, type_: str, **fields: Any) -> None:
        with self._lock:
            self._seq += 1
            rec = {"seq": self._seq, "ts": time.time(), "type": type_}
            rec.update(fields)
            line = redact(json.dumps(rec, ensure_ascii=False))
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def raw(self, agent_id: str, stream: str, line: str) -> None:
        with self._lock:
            size = self._raw_sizes.get(agent_id, 0)
            if size >= RAW_FILE_MAX:
                return
            text = redact(line)
            if len(text) > RAW_LINE_MAX:
                text = text[:RAW_LINE_MAX] + f"...[truncated {len(line) - RAW_LINE_MAX} chars]"
            rec = json.dumps({"ts": round(time.time(), 3), "s": stream, "l": text}, ensure_ascii=False) + "\n"
            if size + len(rec) >= RAW_FILE_MAX:
                rec = json.dumps({"s": "note", "l": "raw log size cap reached; further lines dropped"}) + "\n"
                self._raw_sizes[agent_id] = RAW_FILE_MAX
            else:
                self._raw_sizes[agent_id] = size + len(rec)
            with open(self.dir / "raw" / f"{agent_id}.jsonl", "a", encoding="utf-8") as f:
                f.write(rec)
