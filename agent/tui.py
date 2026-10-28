"""Full-screen chat room (prompt_toolkit). Screen and keys only: state, dispatch and the event log stay in the Engine.

Threads: the Engine and its turn threads call TuiUI.chat/system/... from any thread; those only append to a locked
list and ask the application to repaint (`Application.invalidate` is thread-safe). Nothing outside prompt_toolkit's
own event loop ever writes to the terminal.

Text is wrapped by display width (CJK = 2 cells) on every repaint, so a resize reflows by itself. Structure is drawn
with plain ASCII on purpose: "ambiguous width" glyphs (box drawing, bullets) are 2 cells wide in some CJK consoles and
would misalign the right edge."""
from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

from .orchestrator import fmt_elapsed, fmt_quiet, model_label
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
    label: Optional[str] = None     # model label of the turn that produced it (CLI-reported / requested / unknown)
    replay: bool = False
    ts: float = field(default_factory=time.time)
    _cache: Optional[Tuple[int, List[Line]]] = field(default=None, repr=False, compare=False)

    def lines(self, width: int) -> List[Line]:
        if self._cache and self._cache[0] == width:
            return self._cache[1]
        out = self._render(width)
        self._cache = (width, out)
        return out

    def _style(self) -> str:
        if self.kind == "error":
            return "class:error"
        if self.kind in ("system", "banner", "note"):
            return "class:system"
        if self.kind == "ask":
            return "class:ask"
        if self.sender == "Me":
            return "class:me"
        if self.sender == "Manager":
            return "class:manager"
        return "class:worker"

    def _render(self, width: int) -> List[Line]:
        gutter = "| " if self.replay else ""
        dim = " class:replay" if self.replay else ""
        inner = max(MIN_WIDTH, width) - len(gutter)
        gstyle = "class:replay"

        def row(style: str, text: str, pre: str = "") -> Line:
            return ([(gstyle, gutter)] if gutter else []) + ([(style + dim, pre)] if pre else []) + [(style + dim, text)]

        text = sanitize(self.text)
        st = self._style()
        if self.kind in ("chat", "ask"):
            who = sanitize(self.sender + (f" -> {self.to}" if self.to else "")).replace("\n", " ")
            head = ([] if self.replay else [time.strftime("%H:%M:%S", time.localtime(self.ts))]) + [who]
            if self.kind == "ask":
                head.append("[需要你的回答]")
            if self.label:
                head.append(f"({sanitize(self.label)})")
            title = clip("  ".join(head), inner)
            out = [([(gstyle, gutter)] if gutter else []) + [(st + " bold" + dim, title)]]
            for ln in wrap(text, inner - 2):
                out.append(row("", ln, "  "))
            out.append([])
            return out
        pre, cont = {"error": ("! ", "  "), "banner": ("", ""), "note": ("~ ", "  ")}.get(self.kind, ("* ", "  "))
        out = []
        for i, ln in enumerate(wrap(text, inner - len(pre))):
            out.append(row(st, ln, pre if i == 0 else cont))
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

    def add(self, entry: Entry) -> None:
        with self.lock:
            rows = len(entry.lines(self.width))
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
            chunks = [e.lines(self.width) for e in self.entries]
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
        label = None
        if isinstance(meta, dict) and sender != "Me":
            try:
                label = model_label(meta)
            except Exception:
                label = None
        self._add(Entry("chat", str(sender), str(text), to, label))

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
        label = None
        if isinstance(meta, dict) and sender != "Me":
            try:
                label = model_label(meta)
            except Exception:
                label = None
        self.p._add(Entry("chat", str(sender), str(text), to, label, replay=True))

    def system(self, text): self.p._add(Entry("note", "system", str(text), replay=True))
    def error(self, text): self.p._add(Entry("error", "system", str(text), replay=True))
    def ask_user(self, text): self.p._add(Entry("ask", "Manager", str(text), "Me", replay=True))


STYLE = Style.from_dict({
    "me": "bold",
    "manager": "ansicyan",
    "worker": "ansigreen",
    "ask": "ansibrightcyan",
    "system": "ansiyellow",
    "error": "ansired",
    "replay": "ansibrightblack",
    "status": "reverse",
    "status.run": "reverse bold",
    "title": "ansibrightblack",
    "input-title": "ansicyan bold",
})

TITLE = "Agent 聊天室（輸入送給 Manager，不是 PowerShell）"
KEYS = ("Enter 送出 | Ctrl+J 換行 | PgUp/PgDn 捲動 | End 最新 | Ctrl+C 中斷 | /exit 離開",
        "Enter 送出 | Ctrl+J 換行 | PgUp/PgDn 捲動 | /exit 離開",
        "Enter 送出 | Ctrl+J 換行 | /exit 離開")


def title_fragments(columns: int) -> List[Tuple[str, str]]:
    """The line above the input box: what this box is, plus as many key hints as fit."""
    room = columns - text_width(TITLE) - 2
    for k in KEYS:
        if text_width(k) <= room:
            return [("class:input-title", TITLE), ("class:title", "  " + k)]
    return [("class:input-title", clip(TITLE, max(1, columns - 1)))]


def status_line(snap: Dict[str, Any], view: ChatView) -> str:
    """One line: who is doing what, from facts the Engine really has. No progress estimate, no token count."""
    parts: List[str] = []
    if view.scroll > 0:
        parts.append("[捲動中" + (f"，{view.unseen} 則新訊息" if view.unseen else "") + "；End 回到最新]")
    agents = snap.get("agents") or []
    for a in agents:
        name = one_line(a.get("name"))
        if a.get("running"):
            s = f"{name} 執行中 {fmt_elapsed(a['elapsed'])}"
            q = a.get("quiet")
            if q is not None and q >= 5:
                s += f"，{fmt_quiet(q)}沒有輸出"
            tool = one_line(a.get("tool"))
            if tool:
                s += f"，工具 {tool}（{fmt_elapsed(a.get('tool_ago') or 0)}前）"
            task = one_line(a.get("task"))
            if task:
                s += f"，任務 {task}"
        else:
            s = f"{name} 閒置"
        if a.get("queued"):
            s += f"（佇列 {int(a['queued'])}）"
        parts.append(f"[{s}]")
    state = snap.get("state")
    if snap.get("hold"):
        parts.append("已暫停：輸入訊息或 /continue 繼續")
    elif snap.get("awaiting_user"):
        parts.append("等你回答 Manager 的問題")
    elif not any(a.get("running") for a in agents):
        if state == "waiting_worker":
            parts.append("Manager 等待 Worker")
        elif state == "waiting_manager":
            parts.append("等待 Manager 處理回報")
    elif state == "waiting_worker":
        parts.append("Manager 等待 Worker")
    st = snap.get("import_stage")
    if st and st != "ready":
        parts.append(f"匯入：{one_line(st, 20)}")
    return " ".join(parts) or "準備中"


class TuiApp:
    """The prompt_toolkit application. `on_line(text)` is called (on the app's thread, so it must not block) for every
    submitted message; `on_ctrl_c()` returns True when it did something (e.g. interrupted a running turn)."""

    def __init__(self, on_line: Callable[[str], None], status: Callable[[], Dict[str, Any]],
                 on_ctrl_c: Callable[[], bool], input=None, output=None) -> None:
        self.on_line = on_line
        self._status = status
        self.on_ctrl_c = on_ctrl_c
        self.view = ChatView()
        self.ui = TuiUI(self.view, self.invalidate)
        self.input = TextArea(multiline=True, wrap_lines=True, height=Dimension(min=1, max=6), dont_extend_height=True,
                              complete_while_typing=False, focus_on_click=True,
                              get_line_prefix=lambda n, wrap: "Me> " if n == 0 and wrap == 0 else "    ")
        kb = self._bindings()
        root = HSplit([
            Window(ChatControl(self.view), wrap_lines=False, always_hide_cursor=True),
            Window(FormattedTextControl(self._status_frags), height=1, style="class:status", wrap_lines=False),
            Window(FormattedTextControl(lambda: title_fragments(self._columns())), height=1, wrap_lines=False),
            self.input,
        ])
        self.app: Application = Application(layout=Layout(root, focused_element=self.input), key_bindings=kb,
                                            style=STYLE, full_screen=True, mouse_support=False, refresh_interval=1.0,
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
        running = any(a.get("running") for a in snap.get("agents") or [])
        return [("class:status.run" if running else "class:status", " " + status_line(snap, self.view) + " ")]

    def submit(self) -> None:
        text = self.input.text
        self.input.buffer.reset()
        s = text.strip()
        if not s:
            return
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
