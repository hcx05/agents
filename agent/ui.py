"""Terminal chat room output. All printing goes through one lock; prompt is redrawn after async output."""
from __future__ import annotations

import os
import sys
import threading
from typing import List, Optional, Tuple

IS_WINDOWS = os.name == "nt"


def enable_vt() -> bool:
    """Enable ANSI escapes on Windows 10+ consoles."""
    if not IS_WINDOWS:
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


class UI:
    """Base UI; also the no-op interface the engine talks to."""

    def chat(self, sender: str, text: str, to: Optional[str] = None) -> None: ...
    def system(self, text: str) -> None: ...
    def error(self, text: str) -> None: ...
    def ask_user(self, text: str) -> None: ...


class BufferUI(UI):
    """Collects everything; used by tests."""

    def __init__(self) -> None:
        self.lines: List[Tuple[str, str, str, Optional[str]]] = []
        self._lock = threading.Lock()

    def _add(self, kind: str, sender: str, text: str, to: Optional[str] = None) -> None:
        with self._lock:
            self.lines.append((kind, sender, text, to))

    def chat(self, sender, text, to=None): self._add("chat", sender, text, to)
    def system(self, text): self._add("system", "system", text)
    def error(self, text): self._add("error", "system", text)
    def ask_user(self, text): self._add("ask_user", "Manager", text, "Me")

    def texts(self, kind: Optional[str] = None, sender: Optional[str] = None) -> List[str]:
        return [t for k, s, t, _ in self.lines if (kind is None or k == kind) and (sender is None or s == sender)]


class ChatUI(UI):
    COLORS = {"Manager": "36", "Me": "1", "system": "33", "error": "31", "ask": "1;36"}

    def __init__(self, out=None) -> None:
        self.out = out or sys.stdout
        self.lock = threading.RLock()
        self.tty = bool(getattr(self.out, "isatty", lambda: False)())
        self.color = self.tty and not os.environ.get("NO_COLOR") and enable_vt()
        self.prompt_text = "Me> "
        self.prompt_shown = False

    def _paint(self, key: str, s: str) -> str:
        if not self.color:
            return s
        code = self.COLORS.get(key, "32")
        return f"\x1b[{code}m{s}\x1b[0m"

    def _emit(self, prefix: str, text: str, key: str, indent: bool = True) -> None:
        with self.lock:
            lines = text.splitlines() or [""]
            pad = " " * len(prefix) if indent else ""
            body = "\n".join((prefix if i == 0 else pad) + ln for i, ln in enumerate(lines))
            if self.prompt_shown and self.tty:
                self.out.write("\r\x1b[2K" if self.color else "\n")
            self.out.write(self._paint(key, body) + "\n")
            if self.prompt_shown and self.tty:
                self.out.write(self.prompt_text)
            self.out.flush()

    def chat(self, sender: str, text: str, to: Optional[str] = None) -> None:
        tag = f"@{to} " if to else ""
        self._emit(f"{sender}> {tag}", text, sender)

    def system(self, text: str) -> None:
        self._emit("[system] ", text, "system")

    def error(self, text: str) -> None:
        self._emit("[error] ", text, "error")

    def ask_user(self, text: str) -> None:
        # header and question stay in the one timeline; the question starts at the left edge so it is easy to spot
        self._emit("Manager> @Me ", "[需要你的回答]\n" + text, "ask", indent=False)

    def banner(self, lines: List[str]) -> None:
        with self.lock:
            self.out.write("\n".join(lines) + "\n\n")
            self.out.flush()

    def read_line(self) -> Optional[str]:
        """Blocking read of one user line; None on EOF."""
        if self.tty:
            with self.lock:
                self.out.write(self.prompt_text)
                self.out.flush()
                self.prompt_shown = True
        try:
            line = sys.stdin.readline()
        finally:
            with self.lock:
                self.prompt_shown = False
        if line == "":
            return None
        text = line.rstrip("\r\n")
        if not self.tty:   # piped/scripted input: echo so the transcript reads like the chat room
            with self.lock:
                self.out.write(self._paint("Me", f"Me> {text}") + "\n")
                self.out.flush()
        return text
