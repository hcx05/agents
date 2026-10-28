"""Telescope-style full-screen Session Picker (`agent pick`): Find -> Filter -> Preview -> Pick.

Read-only by design: browsing, searching, and previewing sessions never calls a model, never starts the Engine,
never creates a session or Claude fork, never acquires a SessionLock, and never modifies state.json, events.jsonl,
or any Claude native transcript. Once the user picks a session (Enter) or requests a new one (Ctrl+N), the full-screen
picker exits cleanly first and hands off to the existing CLI resume / new-chat flow."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import threading
import time
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import importer
from .resume import _norm, saved_config, timeline
from .sessionlock import SessionLock
from .store import SESSION_ID_RE, STATE_VERSION, Store, StoreError, _valid_ts, safe_session_dir

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import Dimension, FormattedTextControl, HSplit, Layout, Window
    from prompt_toolkit.layout.controls import UIContent, UIControl
    from prompt_toolkit.styles import Style
    from prompt_toolkit.utils import get_cwidth as _ptk_cwidth
    from prompt_toolkit.widgets import TextArea
    HAVE_PTK = True
except ImportError:
    HAVE_PTK = False
    Application = None  # type: ignore[assignment,misc]
    Condition = None  # type: ignore[assignment,misc]
    KeyBindings = None  # type: ignore[assignment,misc]
    Keys = None  # type: ignore[assignment,misc]
    Dimension = None  # type: ignore[assignment,misc]
    FormattedTextControl = None  # type: ignore[assignment,misc]
    HSplit = None  # type: ignore[assignment,misc]
    Layout = None  # type: ignore[assignment,misc]
    Window = None  # type: ignore[assignment,misc]
    UIContent = None  # type: ignore[assignment,misc]
    UIControl = object  # type: ignore[assignment,misc]
    Style = None  # type: ignore[assignment,misc]
    _ptk_cwidth = None  # type: ignore[assignment]
    TextArea = None  # type: ignore[assignment,misc]

Line = List[Tuple[str, str]]          # one screen row: (style, text) fragments

MIN_WIDTH = 16
MIN_SPLIT_WIDTH = 68                  # below this terminal width, hide the right preview pane to keep list usable
PREVIEW_MAX_ITEMS = 10                # limit recent chat items in preview (~10 items)
PREVIEW_CACHE_MAX = 64                # lightweight LRU cache cap for session previews
TAIL_INITIAL_BYTES = 65536            # 64 KB initial tail read for large events.jsonl
TAIL_MAX_BYTES = 524288               # 512 KB cap when searching tail for recent chat items

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def _char_width(ch: str) -> int:
    if _ptk_cwidth is not None:
        return int(_ptk_cwidth(ch))
    if not ch or ord(ch) < 32 or (0x7F <= ord(ch) < 0xA0):
        return 0
    if unicodedata.combining(ch):
        return 0
    eaw = unicodedata.east_asian_width(ch)
    return 2 if eaw in ("W", "F") else 1


def sanitize(text: Any) -> str:
    """Drop terminal escape sequences, control characters, and bidi overrides."""
    s = str("" if text is None else text).replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return _CTRL.sub("", _ANSI.sub("", s))


def one_line(value: Any, limit: int = 80) -> str:
    text = " ".join(sanitize(value).split())
    return clip(text, limit)


def text_width(s: str) -> int:
    return sum(_char_width(c) for c in s)


def clip(s: str, width: int, ellipsis: str = "") -> str:
    """Cut `s` to at most `width` display cells without splitting wide CJK characters."""
    width = max(0, width)
    if text_width(s) <= width:
        return s
    ew = text_width(ellipsis)
    target = max(0, width - ew) if width > ew else width
    out: List[str] = []
    w = 0
    for ch in s:
        cw = _char_width(ch)
        if w + cw > target:
            break
        out.append(ch)
        w += cw
    if width > ew and ellipsis:
        out.append(ellipsis)
    return "".join(out)


def pad_right(s: str, width: int) -> str:
    """Clip `s` to `width` cells and pad with ASCII spaces to exact display width."""
    width = max(0, width)
    c = clip(s, width)
    pad = max(0, width - text_width(c))
    return c + (" " * pad)


def wrap(text: str, width: int) -> List[str]:
    """Wrap text by display width (CJK = 2 cells). Breaks at spaces when available, otherwise anywhere."""
    width = max(1, width)
    rows: List[str] = []
    for para in text.split("\n"):
        cur: List[str] = []
        w = 0
        sp = -1
        for ch in para:
            cw = _char_width(ch)
            while w + cw > width and cur:
                if sp > 0 and ch != " ":
                    rows.append("".join(cur[:sp]).rstrip())
                    cur = cur[sp:]
                else:
                    rows.append("".join(cur))
                    cur = []
                w = text_width("".join(cur))
                sp = -1
            if ch == " " and not cur:
                continue
            cur.append(ch)
            w += cw
            if ch == " ":
                sp = len(cur)
        rows.append("".join(cur))
    return rows


def fmt_time(ts: Optional[float], with_seconds: bool = False) -> str:
    if ts is None:
        return "未知"
    try:
        fmt = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
        return time.strftime(fmt, time.localtime(float(ts)))
    except (OSError, OverflowError, ValueError, TypeError):
        return "未知"


@dataclass
class PickResult:
    action: str                         # "resume" | "new" | "cancel"
    session_id: Optional[str] = None


@dataclass
class SessionItem:
    id: str
    ok: bool = True
    error: Optional[str] = None
    version: Optional[int] = None
    created: Optional[float] = None
    updated: Optional[float] = None
    cwd: Optional[str] = None
    cwd_mismatch: bool = False
    state: str = "?"
    agents: str = "?"
    agents_detail: str = "?"
    locked: bool = False
    first: str = ""
    import_source: Optional[str] = None
    import_fork: Optional[str] = None
    import_stage: Optional[str] = None

    @property
    def description(self) -> str:
        if self.first:
            return one_line(self.first, 100)
        if not self.ok:
            return f"[損壞] {one_line(self.error or 'state.json 異常', 60)}"
        if self.import_source:
            return f"(已匯入 Claude {self.import_source[:8]}，尚無訊息)"
        return "(尚無使用者訊息)"

    @property
    def badges(self) -> List[str]:
        b: List[str] = []
        if not self.ok:
            b.append("[損壞]")
        if self.locked:
            b.append("[LOCKED]")
        if self.cwd_mismatch:
            b.append("[不同資料夾]")
        if self.import_source:
            b.append(f"[匯入:{self.import_source[:8]}]")
        if self.version == 1:
            b.append("[v1]")
        if self.state and self.state != "?":
            b.append(f"[{sanitize(self.state)}]")
        return b


@dataclass
class PreviewData:
    item: SessionItem
    first_message: str
    recent_items: List[Dict[str, Any]] = field(default_factory=list)
    omitted_earlier: bool = False
    error: Optional[str] = None


def _inspect_session_state(session_dir: Path, row: Dict[str, Any], current_cwd: Optional[str]) -> SessionItem:
    """Read-only validation and enrichment of one session's metadata from state.json."""
    sid = str(row.get("id") or session_dir.name)
    locked = bool(row.get("locked", False))
    first = sanitize(row.get("first") or "").replace("\n", " ").strip()
    state_path = session_dir / "state.json"

    ok = True
    error: Optional[str] = None
    version: Optional[int] = None
    created: Optional[float] = _valid_ts(row.get("created"))
    updated: Optional[float] = _valid_ts(row.get("updated"))
    cwd: Optional[str] = row.get("cwd") if isinstance(row.get("cwd"), str) else None
    state_str = str(row.get("state") or "?")
    agents_short = str(row.get("agents") or "?")
    agents_detail = agents_short
    import_source: Optional[str] = row.get("import_source") if isinstance(row.get("import_source"), str) else None
    import_fork: Optional[str] = None
    import_stage: Optional[str] = None

    try:
        raw_text = state_path.read_text(encoding="utf-8")
        st = json.loads(raw_text)
        if not isinstance(st, dict):
            raise ValueError("state.json 不是 JSON 物件")
    except (OSError, ValueError) as e:
        return SessionItem(
            id=sid, ok=False, error=f"state.json 無法解析 ({e})", version=None, created=created, updated=updated,
            cwd=cwd, cwd_mismatch=False, state="unreadable", agents="?", agents_detail="?", locked=locked,
            first=first, import_source=import_source,
        )

    ver = st.get("version", 1)
    if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1 or ver > STATE_VERSION:
        ok = False
        error = f"Session 版本不相容 (version={ver!r}，目前支援 1..{STATE_VERSION})"
    else:
        version = ver

    created = _valid_ts(st.get("created")) if _valid_ts(st.get("created")) is not None else created
    updated = _valid_ts(st.get("updated")) or created
    cwd_val = st.get("cwd")
    if cwd_val is not None and not isinstance(cwd_val, str):
        ok = False
        error = error or "Metadata 不完整（cwd 欄位格式錯誤）"
        cwd = None
    else:
        cwd = cwd_val

    st_state = st.get("state")
    state_str = sanitize(st_state) if isinstance(st_state, str) and st_state else "?"

    saved = saved_config(st)
    if not saved:
        ok = False
        error = error or "Metadata 不完整（缺少 agents 設定，無法安全恢復）"
        agents_short = "?"
        agents_detail = "缺少 agents 設定"
    else:
        short_parts: List[str] = []
        detail_parts: List[str] = []
        for aid, info in saved.items():
            cli_name = sanitize(info.get("cli") or "?")
            role = sanitize(info.get("role") or "?")
            model = sanitize(info.get("model") or "")
            short_parts.append(cli_name)
            label = f"{aid} ({role}: {cli_name}" + (f"/{model}" if model else "") + ")"
            detail_parts.append(label)
        agents_short = "+".join(short_parts) or "?"
        agents_detail = ", ".join(detail_parts) or "?"

    if "import" in st:
        imp = st.get("import")
        mgr_id = None
        if isinstance(st.get("config"), dict) and isinstance(st["config"].get("manager"), str):
            mgr_id = st["config"]["manager"]
        elif "manager" in saved:
            mgr_id = "manager"
        mgr_entry = (st.get("agents") or {}).get(mgr_id) if (mgr_id and isinstance(st.get("agents"), dict)) else None
        mgr_sid = mgr_entry.get("session_id") if isinstance(mgr_entry, dict) else None
        imp_errs = importer.validate_import_state(imp, mgr_sid)
        if isinstance(imp, dict):
            if isinstance(imp.get("source_id"), str):
                import_source = imp["source_id"]
            if isinstance(imp.get("fork_id"), str):
                import_fork = imp["fork_id"]
            if isinstance(imp.get("stage"), str):
                import_stage = imp["stage"]
        if imp_errs:
            ok = False
            error = error or ("匯入紀錄損壞：" + "；".join(imp_errs))

    cwd_mismatch = bool(cwd and current_cwd and _norm(cwd) != _norm(current_cwd))

    return SessionItem(
        id=sid, ok=ok, error=error, version=version, created=created, updated=updated, cwd=cwd,
        cwd_mismatch=cwd_mismatch, state=state_str, agents=agents_short, agents_detail=agents_detail,
        locked=locked, first=first, import_source=import_source, import_fork=import_fork, import_stage=import_stage,
    )


def load_session_items(sessions_root: Path, current_cwd: Optional[str] = None) -> List[SessionItem]:
    """Load lightweight metadata for all saved Agent sessions, ordered newest-updated first.

    Strictly read-only: never modifies state.json, events.jsonl, or session.lock, and ignores raw Claude UUIDs
    or unsafe directory names."""
    root = Path(sessions_root)
    if not root.exists():
        return []
    raw_rows = Store.summaries(root)
    items: List[SessionItem] = []
    for row in raw_rows:
        sid = row.get("id")
        if not isinstance(sid, str) or not SESSION_ID_RE.match(sid) or importer.is_claude_id(sid):
            continue
        try:
            sdir = safe_session_dir(root, sid)
        except StoreError:
            continue
        items.append(_inspect_session_state(sdir, row, current_cwd))
    items.sort(key=lambda it: (it.updated if it.updated is not None else -1.0, it.id), reverse=True)
    return items


def _is_subsequence(needle: str, haystack: str) -> bool:
    """Simple, deterministic subsequence check for lightweight fuzzy matching."""
    if not needle:
        return True
    it = iter(haystack)
    return all(ch in it for ch in needle)


def _match_score(item: SessionItem, tokens: List[str]) -> Optional[Tuple[int, float, str]]:
    """Return a sort key `(tier, -updated, id)` when all query tokens match `item`, or None if not matched.
    tier 0: exact substring match in primary fields; tier 1: lightweight fuzzy subsequence match."""
    id_low = item.id.lower()
    first_low = (item.first or "").lower()
    cwd_low = (item.cwd or "").lower()
    cwd_base = os.path.basename(cwd_low.rstrip("/\\"))
    desc_low = " ".join([
        item.description.lower(),
        item.state.lower(),
        item.agents.lower(),
        item.agents_detail.lower(),
        (item.import_source or "").lower(),
        "locked 使用中 in use" if item.locked else "",
        "damaged 損壞 error unreadable" if not item.ok else "",
        "import 匯入 claude" if item.import_source else "",
    ])
    haystack_all = f"{id_low}\n{first_low}\n{cwd_low}\n{desc_low}"

    worst_tier = 0
    for tok in tokens:
        if tok in haystack_all:
            continue
        if len(tok) >= 2 and (_is_subsequence(tok, id_low) or _is_subsequence(tok, cwd_base) or _is_subsequence(tok, first_low)):
            worst_tier = max(worst_tier, 1)
        else:
            return None
    upd = item.updated if item.updated is not None else -1.0
    return (worst_tier, -upd, item.id)


def filter_sessions(items: Sequence[SessionItem], query: str) -> List[SessionItem]:
    """Filter sessions by query (ID, working folder, first user message, status/description).
    Case-insensitive for English, supports Chinese and multi-token search, keeps newest-updated order."""
    q = sanitize(query).strip().lower()
    if not q:
        return list(items)
    tokens = [t for t in q.split() if t]
    if not tokens:
        return list(items)
    scored: List[Tuple[Tuple[int, float, str], SessionItem]] = []
    for it in items:
        sc = _match_score(it, tokens)
        if sc is not None:
            scored.append((sc, it))
    scored.sort(key=lambda pair: pair[0])
    return [pair[1] for pair in scored]


def _parse_event_lines(lines: Sequence[str]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            events.append(rec)
    return events


def read_recent_timeline(session_dir: Path, limit: int = PREVIEW_MAX_ITEMS) -> Tuple[List[Dict[str, Any]], bool]:
    """Read the last `limit` timeline items from `events.jsonl` strictly read-only.
    Uses tail seeking on large files so multi-megabyte histories never stall the picker."""
    limit = max(1, limit)
    events_path = Path(session_dir) / "events.jsonl"
    try:
        if not events_path.is_file():
            return [], False
        size = events_path.stat().st_size
        if size <= 0:
            return [], False
        if size <= TAIL_INITIAL_BYTES:
            raw = events_path.read_text(encoding="utf-8", errors="replace")
            items = timeline(_parse_event_lines(raw.splitlines()))
            return items[-limit:], len(items) > limit

        window = TAIL_INITIAL_BYTES
        with open(events_path, "rb") as f:
            while True:
                offset = max(0, size - window)
                f.seek(offset, os.SEEK_SET)
                chunk = f.read(size - offset).decode("utf-8", errors="replace")
                lines = chunk.splitlines()
                if offset > 0 and lines:
                    lines = lines[1:]             # drop partial first line at seek boundary
                items = timeline(_parse_event_lines(lines))
                if len(items) >= limit or offset == 0 or window >= TAIL_MAX_BYTES:
                    omitted = (offset > 0) or (len(items) > limit)
                    return items[-limit:], omitted
                window = min(TAIL_MAX_BYTES, window * 2)
    except OSError:
        return [], False


def _read_first_message_fallback(session_dir: Path, max_lines: int = 200) -> str:
    """Read-only scan of the beginning of events.jsonl for the first user ('Me') chat message."""
    for idx, rec in enumerate(Store.iter_events(session_dir)):
        if idx >= max_lines:
            break
        if rec.get("type") == "chat" and rec.get("sender") == "Me":
            return sanitize(rec.get("text", "")).replace("\n", " ").strip()[:120]
    return ""


class PreviewCache:
    """Lightweight read-only LRU cache for session previews. Loads on demand only for the selected session."""

    def __init__(self, sessions_root: Path, max_items: int = PREVIEW_MAX_ITEMS, capacity: int = PREVIEW_CACHE_MAX):
        self.sessions_root = Path(sessions_root)
        self.max_items = max_items
        self.capacity = max(1, capacity)
        self._cache: "OrderedDict[str, Tuple[Tuple[int, int, bool, bool], PreviewData]]" = OrderedDict()
        self.load_count = 0                        # instrumentation for performance tests

    def get(self, item: SessionItem) -> PreviewData:
        try:
            sdir = safe_session_dir(self.sessions_root, item.id)
        except StoreError as e:
            return PreviewData(item=item, first_message=item.first, recent_items=[], error=str(e))

        events_path = sdir / "events.jsonl"
        try:
            st = events_path.stat()
            sig = (int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))), int(st.st_size), item.locked, item.ok)
        except OSError:
            sig = (0, 0, item.locked, item.ok)

        cached = self._cache.get(item.id)
        if cached is not None and cached[0] == sig:
            self._cache.move_to_end(item.id)
            preview = cached[1]
            preview.item = item
            return preview

        self.load_count += 1
        first_msg = item.first or _read_first_message_fallback(sdir)
        recent, omitted = read_recent_timeline(sdir, limit=self.max_items)
        preview = PreviewData(
            item=item,
            first_message=first_msg,
            recent_items=recent,
            omitted_earlier=omitted,
            error=item.error,
        )
        self._cache[item.id] = (sig, preview)
        self._cache.move_to_end(item.id)
        while len(self._cache) > self.capacity:
            self._cache.popitem(last=False)
        return preview


def _sender_tag_and_style(entry: Dict[str, Any]) -> Tuple[str, str]:
    """Distinguish Me, Manager, and Worker clearly in preview chat items."""
    kind = entry.get("kind") or "chat"
    sender = sanitize(entry.get("sender") or "?")
    to = sanitize(entry.get("to") or "")
    if kind == "error":
        return "! [系統錯誤]", "class:error"
    if kind == "system":
        return "* [系統訊息]", "class:system"
    if kind == "ask_user":
        return "Manager -> Me [需要你的回答]", "class:ask bold"
    if sender == "Me":
        target = f" -> {to}" if to else " -> Manager"
        return f"Me{target}", "class:me"
    if sender == "Manager":
        target = f" -> {to}" if to else ""
        return f"Manager{target}", "class:manager bold"
    target = f" -> {to}" if to else " -> Manager"
    label = f"Worker ({sender})" if sender.lower() != "worker" else "Worker"
    return f"{label}{target}", "class:worker bold"


class PickerView:
    """State and layout engine for the Telescope-style Session Picker."""

    def __init__(self, sessions_root: Path, current_cwd: Optional[str] = None,
                 items: Optional[Sequence[SessionItem]] = None, max_preview_items: int = PREVIEW_MAX_ITEMS) -> None:
        self.lock = threading.RLock()
        self.sessions_root = Path(sessions_root)
        self.current_cwd = current_cwd or os.getcwd()
        self.all_items: List[SessionItem] = list(items) if items is not None else load_session_items(
            self.sessions_root, self.current_cwd
        )
        self.query: str = ""
        self.filtered: List[SessionItem] = list(self.all_items)
        self.selected_index: int = 0
        self.scroll_offset: int = 0
        self.preview_cache = PreviewCache(self.sessions_root, max_items=max_preview_items)
        self.status_notice: Optional[str] = None
        self.result: Optional[PickResult] = None
        self.width: int = 100
        self.height: int = 24

    @property
    def selected_item(self) -> Optional[SessionItem]:
        with self.lock:
            if 0 <= self.selected_index < len(self.filtered):
                return self.filtered[self.selected_index]
            return None

    def set_query(self, text: str) -> None:
        """Update search filter in real time without disk reads. Multi-line text is collapsed to single-line query."""
        with self.lock:
            clean = " ".join(sanitize(text).split())
            if clean == self.query and self.status_notice is None:
                return
            self.query = clean
            self.filtered = filter_sessions(self.all_items, self.query)
            self.selected_index = 0
            self.scroll_offset = 0
            self.status_notice = None

    def move(self, delta: int) -> None:
        with self.lock:
            self.status_notice = None
            if not self.filtered:
                self.selected_index = 0
                self.scroll_offset = 0
                return
            self.selected_index = max(0, min(self.selected_index + delta, len(self.filtered) - 1))

    def page(self, direction: int) -> None:
        with self.lock:
            step = max(1, (self.height // 2) - 1)
            self.move(direction * step)

    def to_top(self) -> None:
        with self.lock:
            self.move(-max(1, len(self.filtered)))

    def to_bottom(self) -> None:
        with self.lock:
            self.move(max(1, len(self.filtered)))

    def confirm(self) -> Optional[PickResult]:
        """Confirm selection (Enter). Returns PickResult('resume', id) when a session is selected."""
        with self.lock:
            sel = self.selected_item
            if sel is None:
                self.status_notice = "目前沒有可開啟的對話；請修改搜尋條件、按 Ctrl+N 建立新對話，或按 Esc 退出。"
                return None
            try:
                sdir = safe_session_dir(self.sessions_root, sel.id)
                sel.locked = SessionLock.is_locked(sdir)
            except StoreError:
                pass
            self.result = PickResult(action="resume", session_id=sel.id)
            return self.result

    def new_session(self) -> PickResult:
        with self.lock:
            self.result = PickResult(action="new", session_id=None)
            return self.result

    def cancel(self) -> PickResult:
        with self.lock:
            self.result = PickResult(action="cancel", session_id=None)
            return self.result

    def header_fragments(self, columns: int) -> Line:
        with self.lock:
            total = len(self.all_items)
            matched = len(self.filtered)
            title = f"Agent Session Picker ({matched}/{total})"
            sub = f"  目前資料夾: {one_line(self.current_cwd, max(12, columns - text_width(title) - 16))}"
            if text_width(title + sub) <= columns:
                return [("class:input-title", title), ("class:title", sub)]
            return [("class:input-title", clip(title, max(1, columns - 1)))]

    def footer_fragments(self, columns: int) -> Line:
        with self.lock:
            keys_full = "↑↓ 選擇 | Enter 開啟 | Ctrl+N 新對話 | Esc 退出"
            keys_short = "↑↓ 選擇 | Enter 開啟 | Ctrl+N 新對話 | Esc"
            keys_mini = "Enter 開啟 | Ctrl+N 新 | Esc 退"
            base = keys_full
            for cand in (keys_full, keys_short, keys_mini):
                if text_width(cand) <= max(10, columns - 2):
                    base = cand
                    break
            else:
                base = clip(keys_mini, max(1, columns - 2))

            extra = ""
            sel = self.selected_item
            if self.status_notice:
                extra = f" | {one_line(self.status_notice, 50)}"
            elif sel is not None:
                if not sel.ok:
                    extra = " | [損壞：無法直接恢復]"
                elif sel.locked:
                    extra = " | [LOCKED：其他程序使用中]"
                elif sel.cwd_mismatch:
                    extra = " | [注意：原始工作資料夾不同]"

            line = " " + base
            if extra and text_width(line + extra) < columns:
                line += extra
            return [("class:status", pad_right(line, columns))]

    def _render_left_pane(self, width: int, height: int) -> List[Line]:
        width = max(8, width)
        height = max(1, height)
        rows: List[Line] = []

        hdr = pad_right(f" 對話列表 ({len(self.filtered)}/{len(self.all_items)})", width)
        rows.append([("class:pane-title", hdr)])
        rows.append([("class:border", "-" * width)])
        body_h = max(1, height - len(rows))

        if not self.all_items:
            msg1 = pad_right(" 尚無已儲存的 Agent 對話。", width)
            msg2 = pad_right(" 按 Ctrl+N 建立新對話，或 Esc 離開。", width)
            rows.append([("class:system", msg1)])
            if body_h >= 2:
                rows.append([("class:replay", msg2)])
            while len(rows) < height:
                rows.append([("", " " * width)])
            return rows[:height]

        if not self.filtered:
            msg1 = pad_right(f" 找不到符合「{one_line(self.query, max(4, width - 16))}」的對話", width)
            msg2 = pad_right(" 請修改搜尋關鍵字，或按 Ctrl+N 建立新對話", width)
            rows.append([("class:system", msg1)])
            if body_h >= 2:
                rows.append([("class:replay", msg2)])
            while len(rows) < height:
                rows.append([("", " " * width)])
            return rows[:height]

        rows_per_item = 2 if body_h >= 4 else 1
        visible_cap = max(1, body_h // rows_per_item)

        if self.selected_index < self.scroll_offset:
            self.scroll_offset = self.selected_index
        elif self.selected_index >= self.scroll_offset + visible_cap:
            self.scroll_offset = self.selected_index - visible_cap + 1
        self.scroll_offset = max(0, min(self.scroll_offset, max(0, len(self.filtered) - visible_cap)))

        visible = self.filtered[self.scroll_offset:self.scroll_offset + visible_cap]
        for idx_offset, it in enumerate(visible):
            abs_idx = self.scroll_offset + idx_offset
            is_sel = abs_idx == self.selected_index
            marker = "> " if is_sel else "  "
            badges_str = " ".join(it.badges)

            if rows_per_item == 1:
                when = fmt_time(it.updated)
                folder = one_line(it.cwd or "?", 18)
                line_txt = f"{marker}{it.description}  {badges_str}  {when}  {folder} ({it.id})"
                style = "class:selected" if is_sel else ("class:error" if not it.ok else "")
                rows.append([(style, pad_right(line_txt, width))])
            else:
                badge_room = min(text_width(badges_str), max(8, width // 2))
                desc_room = max(4, width - text_width(marker) - badge_room - (1 if badges_str else 0))
                desc_clipped = clip(it.description, desc_room)
                badge_clipped = clip(badges_str, max(0, width - text_width(marker) - text_width(desc_clipped) - 1))
                gap = max(1, width - text_width(marker) - text_width(desc_clipped) - text_width(badge_clipped))
                row1_txt = pad_right(f"{marker}{desc_clipped}{' ' * gap}{badge_clipped}", width)

                when = fmt_time(it.updated)
                cwd_str = sanitize(it.cwd or "?")
                meta_txt = f"  {when} | {cwd_str} | {it.id}"
                row2_txt = pad_right(meta_txt, width)

                if is_sel:
                    rows.append([("class:selected", row1_txt)])
                    rows.append([("class:selected", row2_txt)])
                else:
                    r1_style = "class:error" if not it.ok else ("class:system" if it.locked else "class:item-title")
                    rows.append([(r1_style, row1_txt)])
                    rows.append([("class:replay", row2_txt)])

        while len(rows) < height:
            rows.append([("", " " * width)])
        return rows[:height]

    def _render_right_pane(self, width: int, height: int) -> List[Line]:
        width = max(12, width)
        height = max(1, height)
        rows: List[Line] = []

        rows.append([("class:pane-title", pad_right(" 對話預覽 (唯讀 Preview)", width))])
        rows.append([("class:border", "-" * width)])

        sel = self.selected_item
        if sel is None:
            msg = " (未選取任何對話)" if self.all_items else " (尚無已儲存的對話；按 Ctrl+N 建立新對話)"
            rows.append([("class:replay", pad_right(msg, width))])
            while len(rows) < height:
                rows.append([("", " " * width)])
            return rows[:height]

        preview = self.preview_cache.get(sel)

        def add_kv(label: str, val: str, val_style: str = "") -> None:
            prefix = f" {label}: "
            avail = max(4, width - text_width(prefix))
            wrapped = wrap(sanitize(val), avail) or [""]
            first_line = pad_right(prefix + wrapped[0], width)
            rows.append([("class:title", prefix), (val_style, pad_right(wrapped[0], avail))] if val_style else [("", first_line)])
            indent = " " * text_width(prefix)
            for cont in wrapped[1:2]:
                rows.append([("class:title", indent), (val_style, pad_right(cont, avail))])

        add_kv("Session ID", sel.id, "class:me")
        if sel.created is not None:
            add_kv("建立時間  ", fmt_time(sel.created, with_seconds=True))
        add_kv("最後更新  ", fmt_time(sel.updated, with_seconds=True))
        add_kv("工作資料夾", sel.cwd or "?", "class:system" if sel.cwd_mismatch else "")
        if sel.cwd_mismatch:
            for ln in wrap(f"! 注意：與目前資料夾 ({self.current_cwd}) 不同；恢復時不會自動套用 --here", max(8, width - 2)):
                rows.append([("class:system", pad_right(" " + ln, width))])

        state_txt = sel.state if sel.ok else f"{sel.state} [損壞: {sel.error or '無法恢復'}]"
        add_kv("狀態      ", state_txt, "class:error" if not sel.ok else "")
        add_kv("CLI 配置  ", sel.agents_detail)

        if sel.import_source:
            imp_desc = f"是 (來源: {sel.import_source}"
            if sel.import_fork:
                imp_desc += f", Fork: {sel.import_fork[:8]}..."
            if sel.import_stage:
                imp_desc += f", 階段: {sel.import_stage}"
            imp_desc += ")"
            add_kv("Claude匯入", imp_desc, "class:manager")
        else:
            add_kv("Claude匯入", "否")

        lock_txt = "是 [LOCKED / 其他程序使用中]" if sel.locked else "否"
        add_kv("鎖定狀態  ", lock_txt, "class:error" if sel.locked else "")

        first_msg = preview.first_message or sel.first or "(無)"
        add_kv("首則訊息  ", first_msg, "class:me" if first_msg != "(無)" else "class:replay")

        rows.append([("class:border", "-" * width)])
        hdr_chat = f" 最近對話紀錄（顯示最近 {len(preview.recent_items)} 則" + (
            "，較早紀錄已省略）" if preview.omitted_earlier else "）"
        )
        rows.append([("class:pane-title", pad_right(hdr_chat, width))])

        if not sel.ok and preview.error:
            for ln in wrap(f"! 此 Session 損壞無法恢復：{preview.error}", max(8, width - 2)):
                rows.append([("class:error", pad_right(" " + ln, width))])

        if not preview.recent_items:
            rows.append([("class:replay", pad_right(" (無聊天紀錄)", width))])
        else:
            chat_rows: List[Line] = []
            inner_w = max(8, width - 3)
            for entry in preview.recent_items:
                who, who_style = _sender_tag_and_style(entry)
                ts_val = _valid_ts(entry.get("ts"))
                ts_prefix = time.strftime("%H:%M:%S ", time.localtime(ts_val)) if ts_val else ""
                head_txt = pad_right(f" {ts_prefix}{who}", width)
                chat_rows.append([(who_style, head_txt)])
                msg_lines = wrap(sanitize(entry.get("text") or ""), inner_w)
                for ln in msg_lines[:4]:
                    chat_rows.append([("", pad_right("   " + ln, width))])
                if len(msg_lines) > 4:
                    chat_rows.append([("class:replay", pad_right("   ...", width))])

            rem = max(0, height - len(rows))
            if rem > 0:
                rows.extend(chat_rows[-rem:])

        while len(rows) < height:
            rows.append([("", " " * width)])
        return rows[:height]

    def window_rows(self, width: int, height: int) -> List[Line]:
        """Render the picker main area for `width` x `height` cells.
        Splits Left Session List and Right Preview when `width >= MIN_SPLIT_WIDTH`; hides Preview when narrow."""
        with self.lock:
            self.width = max(MIN_WIDTH, width)
            self.height = max(1, height)

            if self.width < MIN_SPLIT_WIDTH:
                sel = self.selected_item
                if sel is not None and self.height >= 4:
                    list_rows = self._render_left_pane(self.width, self.height - 1)
                    summary = f" 預覽: ID={sel.id} | 資料夾={sel.cwd or '?'} | 狀態={'LOCKED' if sel.locked else sel.state}"
                    list_rows.append([("class:title", pad_right(summary, self.width))])
                    return list_rows
                return self._render_left_pane(self.width, self.height)

            left_w = max(28, min(self.width - 30, int(self.width * 0.44)))
            sep_w = 3
            right_w = max(24, self.width - left_w - sep_w)

            left_rows = self._render_left_pane(left_w, self.height)
            right_rows = self._render_right_pane(right_w, self.height)

            combined: List[Line] = []
            for r_idx in range(self.height):
                l_line = left_rows[r_idx] if r_idx < len(left_rows) else [("", " " * left_w)]
                r_line = right_rows[r_idx] if r_idx < len(right_rows) else [("", " " * right_w)]
                combined.append(l_line + [("class:border", " | ")] + r_line)
            return combined or [[]]


STYLE_DICT = {
    "me": "bold",
    "manager": "ansicyan",
    "worker": "ansigreen",
    "ask": "ansibrightcyan",
    "system": "ansiyellow",
    "error": "ansired",
    "replay": "ansibrightblack",
    "status": "reverse",
    "title": "ansibrightblack",
    "input-title": "ansicyan bold",
    "pane-title": "ansicyan bold",
    "item-title": "bold",
    "selected": "reverse bold",
    "border": "ansibrightblack",
}
STYLE = Style.from_dict(STYLE_DICT) if Style is not None else STYLE_DICT


class PickerControl(UIControl):  # type: ignore[misc]
    def __init__(self, view: PickerView) -> None:
        self.view = view

    def create_content(self, width: int, height: int):
        rows = self.view.window_rows(width, height)
        return UIContent(get_line=lambda i: rows[i], line_count=len(rows), show_cursor=False)

    def is_focusable(self) -> bool:
        return False


class PickerApp:
    """Full-screen prompt_toolkit Session Picker application."""

    def __init__(self, view: PickerView, input=None, output=None) -> None:
        if Application is None or TextArea is None or HSplit is None or Window is None or Layout is None:
            raise RuntimeError("prompt_toolkit is not installed")
        self.view = view
        self.input = TextArea(
            multiline=False,
            wrap_lines=False,
            height=Dimension.exact(1),
            dont_extend_height=True,
            complete_while_typing=False,
            focus_on_click=True,
            get_line_prefix=lambda n, wrap: "搜尋> " if n == 0 and wrap == 0 else "      ",
        )
        self.input.buffer.on_text_changed += self._on_buffer_changed
        kb = self._bindings()
        root = HSplit([
            Window(FormattedTextControl(lambda: self.view.header_fragments(self._columns())),
                   height=1, wrap_lines=False),
            self.input,
            Window(FormattedTextControl(lambda: [("class:border", "=" * self._columns())]),
                   height=1, wrap_lines=False),
            Window(PickerControl(self.view), wrap_lines=False, always_hide_cursor=True),
            Window(FormattedTextControl(lambda: self.view.footer_fragments(self._columns())),
                   height=1, style="class:status", wrap_lines=False),
        ])
        self.app = Application(
            layout=Layout(root, focused_element=self.input),
            key_bindings=kb,
            style=Style.from_dict(STYLE_DICT) if Style is not None else STYLE,
            full_screen=True,
            mouse_support=False,
            input=input,
            output=output,
        )
        self.app.ttimeoutlen = 0.05

    def _columns(self) -> int:
        try:
            return max(MIN_WIDTH, int(self.app.output.get_size().columns))
        except Exception:
            return 100

    def _on_buffer_changed(self, buf) -> None:
        txt = buf.text
        if "\n" in txt or "\r" in txt:
            clean = " ".join(txt.splitlines())
            buf.text = clean
            buf.cursor_position = len(clean)
            return
        self.view.set_query(txt)
        self.invalidate()

    def invalidate(self) -> None:
        try:
            self.app.invalidate()
        except Exception:
            pass

    def request_exit(self, result: Optional[PickResult] = None) -> None:
        if result is not None:
            self.view.result = result
        loop = getattr(self.app, "loop", None)
        if loop is not None:
            loop.call_soon_threadsafe(self._exit_now)
        else:
            self._exit_now()

    def _exit_now(self) -> None:
        if self.app.is_running and not self.app.future.done():
            self.app.exit(result=self.view.result)

    def run(self) -> PickResult:
        res = self.app.run(handle_sigint=False)
        if isinstance(res, PickResult):
            return res
        return self.view.result or PickResult(action="cancel")

    def _bindings(self):
        kb = KeyBindings()

        @kb.add("up")
        @kb.add("c-p")
        def _(event):
            self.view.move(-1)
            self.invalidate()

        @kb.add("down")
        def _(event):
            self.view.move(+1)
            self.invalidate()

        @kb.add("pageup")
        def _(event):
            self.view.page(-1)
            self.invalidate()

        @kb.add("pagedown")
        def _(event):
            self.view.page(+1)
            self.invalidate()

        @kb.add("enter")
        def _(event):
            res = self.view.confirm()
            if res is not None:
                event.app.exit(result=res)
            else:
                self.invalidate()

        @kb.add("c-n")
        def _(event):
            res = self.view.new_session()
            event.app.exit(result=res)

        @kb.add("escape")
        @kb.add("c-c")
        def _(event):
            res = self.view.cancel()
            event.app.exit(result=res)

        @kb.add("c-d", filter=Condition(lambda: not self.input.text))
        def _(event):
            res = self.view.cancel()
            event.app.exit(result=res)

        @kb.add(Keys.BracketedPaste)
        def _(event):
            pasted = " ".join(sanitize(event.data).splitlines())
            if pasted:
                event.current_buffer.insert_text(pasted)

        return kb


def run_picker(sessions_root: Path, current_cwd: Optional[str] = None,
               input=None, output=None) -> PickResult:
    """Run the full-screen Session Picker and return the user's choice (`resume`, `new`, or `cancel`)."""
    view = PickerView(sessions_root=sessions_root, current_cwd=current_cwd)
    app = PickerApp(view, input=input, output=output)
    return app.run()
