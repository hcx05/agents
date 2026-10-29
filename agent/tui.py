"""Full-screen chat room (prompt_toolkit). Screen and keys only: state, dispatch and the event log stay in the Engine.

Threads: the Engine and its turn threads call TuiUI.chat/system/... from any thread; those only append to a locked
list and ask the application to repaint (`Application.invalidate` is thread-safe). Nothing outside prompt_toolkit's
own event loop ever writes to the terminal.

Text is wrapped by display width (CJK = 2 cells) on every repaint, so a resize reflows by itself. Decoration glyphs
come from theme.Symbols: Unicode where it is known to line up, ASCII otherwise ("ambiguous width" glyphs are 2 cells
wide in some CJK consoles and would misalign the right edge). Message text never depends on them."""
from __future__ import annotations

import re
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_session
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

from .inputhistory import HISTORY_LIMIT
from .modelname import header_parts, role_tag
from .orchestrator import fmt_elapsed, fmt_quiet
from .theme import UNICODE, Symbols, build_style, pick_symbols, pick_theme, worker_style
from .ui import UI

Line = List[Tuple[str, str]]          # one screen row: (style, text) fragments

MAX_ENTRIES = 5000                     # oldest entries drop off the in-memory view (the event log keeps everything)
MIN_WIDTH = 12

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]")


def sanitize(text: str) -> str:
    """Agent output is untrusted terminal input: drop escape sequences, control characters and bidi overrides."""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return _CTRL.sub("", _ANSI.sub("", text))


def one_line(value: Any, limit: int = 40) -> str:
    """A dynamic value (tool name, task id, agent name ...) as safe single-line status text. prompt_toolkit already
    shows ESC / BEL / NUL / C1 as inert caret text (^[), but it passes bidi overrides through untouched and starts a new
    row at a newline, so those are removed here; whitespace is collapsed and the length is capped."""
    text = " ".join(sanitize("" if value is None else value).split())
    return clip(text, limit)


def text_width(s: str) -> int:
    return sum(get_cwidth(c) for c in s)


def clip(s: str, width: int) -> str:
    """Cut to `width` cells (never splits a wide character)."""
    out, w = [], 0
    for ch in s:
        cw = get_cwidth(ch)
        if w + cw > width:
            break
        out.append(ch)
        w += cw
    return "".join(out)


def shorten_cells(s: str, width: int) -> str:
    """Cut to `width` cells with "..." (plain dots: the ellipsis character is ambiguous-width)."""
    if text_width(s) <= width:
        return s
    return clip(s, max(0, width - 3)).rstrip() + "..." if width > 3 else clip(s, width)


def wrap(text: str, width: int) -> List[str]:
    """Wrap by display width. Breaks at a space when the line has one, otherwise anywhere (CJK has no spaces).
    Blank lines are kept; every row is at most `width` cells (unless one character is wider than the width)."""
    width = max(1, width)
    rows: List[str] = []
    for para in text.split("\n"):
        cur: List[str] = []
        w = 0
        sp = -1                                   # number of chars in `cur` up to and including the last space
        for ch in para:
            cw = get_cwidth(ch)
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
                continue                          # no leading space on a continuation row
            cur.append(ch)
            w += cw
            if ch == " ":
                sp = len(cur)
        rows.append("".join(cur))
    return rows


@dataclass
class Entry:
    kind: str                       # chat | ask | system | error | banner | note
    sender: str
    text: str
    to: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None    # turn_info of the turn that produced it (reported / requested / unknown)
    replay: bool = False
    ts: float = field(default_factory=time.time)
    gap_before: bool = False        # set by ChatView.add: a blank row separates it from the previous entry
    _cache: Optional[Tuple[Tuple[int, Symbols], List[Line]]] = field(default=None, repr=False, compare=False)

    def lines(self, width: int, sym: Symbols = UNICODE) -> List[Line]:
        key = (width, sym)
        if self._cache and self._cache[0] == key:
            return self._cache[1]
        out = self._render(width, sym)
        self._cache = (key, out)
        return out

    @property
    def compact(self) -> bool:
        return self.kind in ("system", "error", "note")

    def _role(self) -> str:
        if self.kind == "error":
            return "error"
        if self.kind in ("system", "banner", "note"):
            return "system"
        if self.sender == "Me":
            return "me"
        if self.sender == "Manager" or (self.meta or {}).get("agent") == "manager":
            return "manager"
        return worker_style(zlib.crc32(self.sender.encode("utf-8", "replace")) % 3)

    def _header(self, sym: Symbols, room: int) -> Line:
        """`◆ Claude Sonnet 5.5 (Mgr) · 未確認 · → w1 · 14:31`, trimmed to `room` cells. When it is too narrow the
        clock goes first, then the recipient, then the "needs your answer" note; the evidence note and the name stay,
        and a name that still does not fit is cut (a header never wraps)."""
        role = self._role()
        user = role == "me"
        mark = sym.user if user else (sym.manager if role == "manager" else sym.worker)
        sender = one_line(self.sender, 24)
        meta = self.meta if isinstance(self.meta, dict) else None
        if user:
            name, tag, evidence, extra = "You", "", "", ""
        else:
            h = header_parts(sender, meta)
            name, tag, evidence, extra = one_line(h.name, 40), one_line(h.tag, 16), one_line(h.evidence, 12), one_line(h.extra, 6)
        tag_text = f" ({tag})" if tag else ""
        extra_text = f" {extra}" if extra else ""
        tail: List[List[Tuple[str, str]]] = []           # optional pieces, most important first
        if evidence:
            tail.append([("class:evidence", evidence)])
        if self.kind == "ask":
            tail.append([("class:ask", "需要你的回答")])
        to = one_line(self.to, 16) if self.to and not user and self.to not in ("Me", "Manager") else ""
        if to:
            tail.append([("class:dim", f"{sym.arrow} {to}")])
        if not self.replay:
            tail.append([("class:dim", time.strftime("%H:%M", time.localtime(self.ts)))])
        sep = f" {sym.sep} "

        def width(parts: List[List[Tuple[str, str]]], nm: str) -> int:
            return (text_width(mark + " " + nm + extra_text + tag_text)
                    + sum(text_width(sep) + text_width("".join(t for _, t in p)) for p in parts))

        keep = list(tail)
        while len(keep) > (1 if evidence else 0) and width(keep, name) > room:
            keep.pop()                                   # the clock goes first, the evidence note is the last to go
        if width(keep, name) > room:                     # still too wide: shorten the model name, not the evidence
            spare = room - (width(keep, "") )
            name = shorten_cells(name, max(4, spare))
        out: Line = [(f"class:{role}", mark + " " + name + extra_text + tag_text)]
        for piece in keep:
            out.append(("class:dim", sep))
            out.extend(piece)
        if width(keep, name) > room:                     # nothing left to drop: cut, a header never wraps
            flat = "".join(t for _, t in out)
            return [(f"class:{role}", clip(flat, max(1, room)))]
        return out

    def _render(self, width: int, sym: Symbols) -> List[Line]:
        gutter = sym.gutter + " " if self.replay else ""
        inner = max(MIN_WIDTH, width) - text_width(gutter)
        dimmed = " class:replay" if self.replay else ""

        def row(frags: Line) -> Line:
            return ([("class:replay", gutter)] if gutter else []) + [((st + dimmed).strip(), t) for st, t in frags]

        text = sanitize(self.text)
        out: List[Line] = [[]] if self.gap_before else []
        role = self._role()
        if self.kind in ("chat", "ask"):
            out.append(row([("", "  ")] + self._header(sym, inner - 2)))
            for ln in wrap(text, inner - 4):
                out.append(row([("", "    "), ("", ln)]))
            return out
        if self.kind == "banner":
            return [row([("class:system", ln)]) for ln in wrap(text, inner)]
        pre = "  " + (sym.error if self.kind == "error" else sym.system) + " "
        pw = text_width(pre)
        for i, ln in enumerate(wrap(text, inner - pw)):
            out.append(row([(f"class:{role}", (pre if i == 0 else " " * pw) + ln)]))
        return out


class ChatView:
    """Thread-safe timeline + scroll state. `scroll` = rows hidden BELOW the window (0 = following the newest)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.entries: List[Entry] = []
        self.scroll = 0
        self.unseen = 0                       # entries that arrived while the user was reading older ones
        self.width = 80
        self.height = 20
        self.total = 0
        self.symbols: Symbols = UNICODE

    def add(self, entry: Entry) -> None:
        with self.lock:
            prev = self.entries[-1] if self.entries else None
            entry.gap_before = prev is not None and not (prev.compact and entry.compact) and entry.kind != "banner"
            rows = len(entry.lines(self.width, self.symbols))
            self.entries.append(entry)
            if self.scroll > 0:               # keep what the user is reading exactly where it is
                self.scroll += rows
                self.unseen += 1
            if len(self.entries) > MAX_ENTRIES:
                del self.entries[:len(self.entries) - MAX_ENTRIES]

    def scroll_by(self, delta: int) -> None:
        """delta > 0 goes back in time."""
        with self.lock:
            self.scroll = max(0, min(self.scroll + delta, max(0, self.total - self.height)))
            if self.scroll == 0:
                self.unseen = 0

    def page(self, direction: int) -> None:
        self.scroll_by(direction * max(1, self.height - 2))

    def to_bottom(self) -> None:
        with self.lock:
            self.scroll = 0
            self.unseen = 0

    def window_rows(self, width: int, height: int) -> List[Line]:
        """The rows to show for a window of `width` x `height` cells."""
        with self.lock:
            self.width, self.height = max(MIN_WIDTH, width), max(1, height)
            chunks = [e.lines(self.width, self.symbols) for e in self.entries]
            self.total = sum(len(c) for c in chunks)
            self.scroll = max(0, min(self.scroll, max(0, self.total - self.height)))
            if self.scroll == 0:
                self.unseen = 0
            end = self.total - self.scroll
            start = max(0, end - self.height)
            rows: List[Line] = []
            pos = 0
            for c in chunks:
                nxt = pos + len(c)
                if nxt > start and pos < end:
                    rows.extend(c[max(0, start - pos):max(0, end - pos)])
                pos = nxt
                if pos >= end:
                    break
            return rows or [[]]


class ChatControl(UIControl):
    def __init__(self, view: ChatView) -> None:
        self.view = view

    def create_content(self, width: int, height: int) -> UIContent:
        rows = self.view.window_rows(width, height)
        return UIContent(get_line=lambda i: rows[i], line_count=len(rows), show_cursor=False)

    def is_focusable(self) -> bool:
        return False


class TuiUI(UI):
    """What the Engine talks to. Safe to call from any thread."""

    def __init__(self, view: ChatView, invalidate: Callable[[], None]) -> None:
        self.view = view
        self._invalidate = invalidate

    def _add(self, entry: Entry) -> None:
        self.view.add(entry)
        self._invalidate()

    def chat(self, sender, text, to=None, meta=None):
        self._add(Entry("chat", str(sender), str(text), to, meta if isinstance(meta, dict) and sender != "Me" else None))

    def system(self, text): self._add(Entry("system", "system", str(text)))
    def error(self, text): self._add(Entry("error", "system", str(text)))
    def ask_user(self, text): self._add(Entry("ask", "Manager", str(text), "Me"))

    def banner(self, lines: List[str]) -> None:
        self._add(Entry("banner", "system", "\n".join(lines) + "\n"))

    def echo_me(self, text: str, to: Optional[str] = "Manager") -> None:
        """What the user just typed. The Engine logs it; it never calls ui.chat for 'Me', so the TUI shows it itself."""
        self._add(Entry("chat", "Me", text, to))

    def replay_view(self) -> "UI":
        return _ReplayUI(self)


class _ReplayUI(UI):
    """Same view, entries marked as replay (dim, gutter). Handed to resume.replay(), which never reaches the Engine
    or the event log: nothing is re-executed or re-recorded."""

    def __init__(self, parent: TuiUI) -> None:
        self.p = parent

    def chat(self, sender, text, to=None, meta=None):
        self.p._add(Entry("chat", str(sender), str(text), to, meta if isinstance(meta, dict) and sender != "Me" else None,
                          replay=True))

    def system(self, text): self.p._add(Entry("note", "system", str(text), replay=True))
    def error(self, text): self.p._add(Entry("error", "system", str(text), replay=True))
    def ask_user(self, text): self.p._add(Entry("ask", "Manager", str(text), "Me", replay=True))


TITLE = "Agent 聊天室（輸入送給 Manager，不是 PowerShell）"
KEYS = ("Enter 送出 | Ctrl+J 換行 | Up/Down 或 Ctrl+P/N 歷史 | PgUp/PgDn 捲動 | End 最新 | Ctrl+C 中斷 | /exit 離開",
        "Enter 送出 | Ctrl+J 換行 | Up/Down 歷史 | PgUp/PgDn 捲動 | /exit 離開",
        "Enter 送出 | Ctrl+J 換行 | Up/Down 歷史 | /exit 離開",
        "Enter 送出 | Ctrl+J 換行 | /exit 離開")


def title_fragments(columns: int) -> List[Tuple[str, str]]:
    """The line above the input box: what this box is, plus as many key hints as fit."""
    room = columns - text_width(TITLE) - 2
    for k in KEYS:
        if text_width(k) <= room:
            return [("class:input-title", TITLE), ("class:title", "  " + k)]
    return [("class:input-title", clip(TITLE, max(1, columns - 1)))]


STATUS_GAP = "    "


def _agent_segment(a: Dict[str, Any], level: int, sym: Symbols) -> Line:
    """One agent's part of the status bar at detail level 3 (everything) .. 0 (dot and name). Level 3 reads like
    `● Mgr 執行中 8s · 12 秒沒有輸出 · 工具 Read（3s前）`; lower levels drop the least useful facts first."""
    running = bool(a.get("running"))
    name = one_line(role_tag(str(a.get("name") or "?"), a.get("role")), 16)
    dot = ("class:status class:status.dot.run", sym.run) if running else ("class:status class:status.dot.idle", sym.idle)
    out: Line = [dot, ("class:status", " " + name)]
    if level == 0:
        return out
    bits: List[str] = []
    if running:
        bits.append(f"執行中 {fmt_elapsed(a.get('elapsed') or 0)}")
        q = a.get("quiet")
        if level >= 3 and q is not None and q >= 5:
            bits.append(f"{fmt_quiet(q)}沒有輸出")
        tool = one_line(a.get("tool"))
        if tool and level >= 2:
            bits.append(f"工具 {tool}" + (f"（{fmt_elapsed(a.get('tool_ago') or 0)}前）" if level >= 3 else ""))
        task = one_line(a.get("task"))
        if task and level >= 2:
            bits.append(f"任務 {task}")
    else:
        bits.append("閒置")
    if a.get("queued") and level >= 2:
        bits.append(f"佇列 {int(a['queued'])}")
    sep = f" {sym.sep} "
    out.append(("class:status", " " + bits[0]))
    for b in bits[1:]:
        out.append(("class:status class:status.hint", sep))
        out.append(("class:status", b))
    return out


def status_fragments(snap: Dict[str, Any], view: ChatView, columns: int, sym: Symbols = UNICODE) -> Line:
    """The status bar for `columns` cells. Facts only (no progress estimate, no token count). When it does not fit it
    gets less detailed in a fixed order: per-agent detail goes level by level, then minor notices, then idle agents;
    the scroll hint and the hold / waiting-for-you notices are the last to go."""
    if columns < 3:
        return [("class:status", "")]
    room = columns - 2
    agents = [a for a in (snap.get("agents") or []) if isinstance(a, dict)]
    state = snap.get("state")
    urgent: List[str] = []
    if view.scroll > 0:
        urgent.append("捲動中" + (f" · {view.unseen} 則新訊息" if view.unseen else "") + " · End 回到最新")
    if snap.get("hold"):
        urgent.append("已暫停：輸入訊息或 /continue 繼續")
    elif snap.get("awaiting_user"):
        urgent.append("等你回答 Manager 的問題")
    minor: List[str] = []
    any_run = any(a.get("running") for a in agents)
    if not snap.get("hold") and not snap.get("awaiting_user"):
        if state == "waiting_worker":
            minor.append("Manager 等待 Worker")
        elif state == "waiting_manager" and not any_run:
            minor.append("等待 Manager 處理回報")
    st = snap.get("import_stage")
    notes = [f"匯入：{one_line(st, 20)}"] if st and st != "ready" else []
    hint = "class:status class:status.hint"

    def build(level: int, drop_idle: bool) -> Line:
        out: Line = []
        for i, seg in enumerate([_agent_segment(a, level, sym) for a in agents if a.get("running") or not drop_idle]):
            if i:
                out.append(("class:status", STATUS_GAP))
            out.extend(seg)
        extra = list(urgent) + (minor if level >= 2 else []) + (notes if level >= 1 else [])
        for text in extra:
            if out:
                out.append(("class:status", STATUS_GAP))
            out.append((hint, text))
        return out

    def width(frags: Line) -> int:
        return text_width("".join(t for _, t in frags))

    chosen: Line = []
    fits = False
    for drop_idle in (False, True):
        for level in (3, 2, 1, 0):
            chosen = build(level, drop_idle)
            if width(chosen) <= room:
                fits = True
                break
        if fits:
            break
    if not chosen:
        chosen = [("class:status", "準備中")]
    out, used = [], 0                                   # last resort: cut the end, whatever is left
    for style, text in chosen:
        t = clip(text, room - used)
        if t:
            out.append((style, t))
        used += text_width(t)
        if t != text or used >= room:                   # cut here: nothing after a cut may slip in
            break
    return [("class:status", " ")] + out


def status_line(snap: Dict[str, Any], view: ChatView, columns: int = 10000, sym: Symbols = UNICODE) -> str:
    """Plain-text form of status_fragments (tests and logs)."""
    return "".join(t for _, t in status_fragments(snap, view, columns, sym)).strip()


class TuiApp:
    """The prompt_toolkit application. `on_line(text)` is called (on the app's thread, so it must not block) for every
    submitted message; `on_ctrl_c()` returns True when it did something (e.g. interrupted a running turn)."""

    def __init__(self, on_line: Callable[[str], None], status: Callable[[], Dict[str, Any]],
                 on_ctrl_c: Callable[[], bool], input=None, output=None, history: Sequence[str] = (),
                 symbols: Optional[Symbols] = None, theme: Optional[str] = None, color_depth=None) -> None:
        self.on_line = on_line
        self._status = status
        self.on_ctrl_c = on_ctrl_c
        self.view = ChatView()
        self.view.symbols = symbols or pick_symbols()
        self.symbols = self.view.symbols
        out = output or get_app_session().output
        depth = color_depth or out.get_default_color_depth()
        self.ui = TuiUI(self.view, self.invalidate)
        # Up / Down / Ctrl+P / Ctrl+N are prompt_toolkit's own history keys (Up and Down move the cursor inside a
        # multi-line draft first), and the unsent draft stays at the newest position. Only what the user SENT is
        # added; picking an entry just fills the box, nothing is submitted until Enter.
        self._history: List[str] = [h for h in history if isinstance(h, str) and h.strip()][-HISTORY_LIMIT:]
        self.input = TextArea(multiline=True, wrap_lines=True, height=Dimension(min=1, max=6), dont_extend_height=True,
                              history=InMemoryHistory(self._history),
                              complete_while_typing=False, focus_on_click=True,
                              get_line_prefix=lambda n, wrap: [("class:prompt", "Me> ")] if n == 0 and wrap == 0 else "    ")
        kb = self._bindings()
        root = HSplit([
            Window(ChatControl(self.view), wrap_lines=False, always_hide_cursor=True),
            Window(FormattedTextControl(self._status_frags), height=1, style="class:status", wrap_lines=False),
            Window(FormattedTextControl(lambda: title_fragments(self._columns())), height=1, wrap_lines=False),
            self.input,
        ])
        self.app: Application = Application(layout=Layout(root, focused_element=self.input), key_bindings=kb,
                                            style=build_style(depth, theme or pick_theme()), color_depth=depth,
                                            full_screen=True, mouse_support=False, refresh_interval=1.0,
                                            input=input, output=output)

    # ---- called from any thread
    def invalidate(self) -> None:
        try:
            self.app.invalidate()
        except Exception:
            pass

    def request_exit(self) -> None:
        loop = getattr(self.app, "loop", None)
        if loop is not None:
            loop.call_soon_threadsafe(self._exit_now)
        else:
            self._exit_now()

    def _exit_now(self) -> None:
        if self.app.is_running and not self.app.future.done():
            self.app.exit()

    def run(self) -> None:
        self.app.run(handle_sigint=False)

    # ---- internals
    def _columns(self) -> int:
        try:
            return self.app.output.get_size().columns
        except Exception:
            return 80

    def _status_frags(self) -> List[Tuple[str, str]]:
        try:
            snap = self._status() or {}
        except Exception:
            snap = {}
        try:
            return status_fragments(snap, self.view, self._columns(), self.symbols)
        except Exception:
            return [("class:status", " ")]

    def _remember(self, s: str) -> None:
        """Add one sent message (slash commands included: this lives in memory only). An immediate repeat is not added
        twice, and only the newest HISTORY_LIMIT entries are kept. The history object is replaced rather than edited
        so no private prompt_toolkit state is touched; the buffer reloads it on its next repaint."""
        if self._history and self._history[-1] == s:
            return
        self._history.append(s)
        del self._history[:-HISTORY_LIMIT]
        self.input.buffer.history = InMemoryHistory(self._history)

    def submit(self) -> None:
        text = self.input.text
        self.input.buffer.reset()
        s = text.strip()
        if not s:
            return
        self._remember(s)
        self.view.to_bottom()
        self.ui.echo_me(s, None if s.startswith("/") else "Manager")
        self.on_line(s)

    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()
        empty = Condition(lambda: not self.input.text)

        @kb.add("enter")
        def _(event):
            self.submit()

        @kb.add("c-j")
        @kb.add("escape", "enter")
        def _(event):                                   # Ctrl+J / Alt+Enter: newline (Shift+Enter is not relied on)
            event.current_buffer.insert_text("\n")

        @kb.add("c-p")
        def _(event):                                   # prompt_toolkit's own Ctrl+P is Up (cursor line first): make it a jump
            event.current_buffer.history_backward(count=event.arg)

        @kb.add("c-n")
        def _(event):
            event.current_buffer.history_forward(count=event.arg)

        @kb.add("pageup")
        def _(event):
            self.view.page(+1)

        @kb.add("pagedown")
        def _(event):
            self.view.page(-1)

        @kb.add("c-end")
        def _(event):
            self.view.to_bottom()

        @kb.add("end", filter=empty)
        def _(event):
            self.view.to_bottom()

        @kb.add("c-c")
        def _(event):
            if self.on_ctrl_c():
                return
            if self.input.text:
                self.input.buffer.reset()
            else:
                self.ui.system("要離開請輸入 /exit（Ctrl+C 只用來中斷正在執行的工作）。")

        @kb.add("c-d", filter=empty)
        def _(event):
            self.on_line("/exit")

        return kb
