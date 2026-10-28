"""Session storage: state.json (atomic), events.jsonl, limited/redacted raw logs."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

RAW_LINE_MAX = 2000
RAW_FILE_MAX = 2 * 1024 * 1024

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


class Store:
    def __init__(self, sessions_root: Path, session_id: Optional[str] = None):
        self.id = session_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.dir = sessions_root / self.id
        (self.dir / "raw").mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.events_path = self.dir / "events.jsonl"
        self._lock = threading.RLock()
        self._seq = 0
        self._raw_sizes: Dict[str, int] = {}
        self.state: Dict[str, Any] = {
            "version": 1, "id": self.id, "created": time.time(), "cwd": None, "state": "idle",
            "agents": {}, "tasks": {}, "awaiting_user": None, "manager_turns": 0, "last_error": None,
        }

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
