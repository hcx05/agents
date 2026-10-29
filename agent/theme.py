"""Symbols and colours for the full-screen chat room: what to use is decided here, from the environment, in pure
functions that take their inputs as arguments (tests pass fake environments).

Symbols. The pretty glyphs (◆ ◇ ● ○ ·) are "ambiguous width": 1 cell in Windows Terminal and most Linux terminals,
2 cells in the legacy console host with a CJK system locale, and some fonts lack them. A wrong guess misaligns the
right edge, so the automatic choice is conservative and the user can always override it:
    AGENT_SYMBOLS=unicode | ascii | auto      (also:  agent --symbols ascii)
Colours. prompt_toolkit already degrades hex colours to 256 / 16 colours by itself; for the 16-colour case this
module hands it ANSI names instead, which follow the user's own terminal theme. Dark vs light:
    AGENT_THEME=dark | light | auto           (auto reads COLORFGBG when the terminal sets it, else dark)
NO_COLOR is honoured by prompt_toolkit (monochrome); bold / dim / reverse still separate the roles."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Mapping, NamedTuple, Optional

from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style


class Symbols(NamedTuple):
    user: str
    manager: str
    worker: str
    system: str
    error: str
    run: str
    idle: str
    sep: str            # between header fields
    arrow: str          # "→ w1" in a header
    gutter: str         # replayed history


UNICODE = Symbols("›", "◆", "◇", "·", "!", "●", "○", "·", "→", "│")
ASCII = Symbols(">", "*", "+", "-", "!", "*", "o", "-", "->", "|")

def pick_symbols(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None,
                 encoding: Optional[str] = None) -> Symbols:
    """AGENT_SYMBOLS wins. Otherwise Unicode only where it is known to line up:
    - the terminal must be able to carry it (UTF-8 encoding, TERM not dumb/linux);
    - on Windows only inside Windows Terminal (WT_SESSION) or the VS Code terminal. Everything else, in particular the
      legacy console host (2-cell ambiguous glyphs under a CJK system locale, missing glyphs in raster fonts), gets
      ASCII. WT_SESSION is a hint, not a guarantee for the font, which is why the override exists."""
    env = os.environ if env is None else env
    choice = (env.get("AGENT_SYMBOLS") or "auto").strip().lower()
    if choice == "unicode":
        return UNICODE
    if choice == "ascii":
        return ASCII
    plat = platform or sys.platform
    term = (env.get("TERM") or "").lower()
    if term in ("dumb", "linux", "vt100", "vt220"):
        return ASCII
    if encoding is None:
        encoding = getattr(sys.stdout, "encoding", None) or ""
    if "utf" not in encoding.lower() and plat != "win32":
        return ASCII
    if plat == "win32":
        if env.get("WT_SESSION"):
            return UNICODE                               # Windows Terminal draws ambiguous-width glyphs 1 cell wide
        if (env.get("TERM_PROGRAM") or "").lower() == "vscode":
            return UNICODE                               # xterm.js: same
        return ASCII                                     # old console host (any code page): width and font unknown
    return UNICODE


def pick_theme(env: Optional[Mapping[str, str]] = None) -> str:
    """'dark' or 'light'. COLORFGBG ("15;0" = white on black) is set by rxvt-family terminals and some others."""
    env = os.environ if env is None else env
    choice = (env.get("AGENT_THEME") or "auto").strip().lower()
    if choice in ("dark", "light"):
        return choice
    fgbg = env.get("COLORFGBG") or ""
    try:
        bg = int(fgbg.split(";")[-1])
        return "light" if bg in (7, 15) else "dark"
    except ValueError:
        return "dark"


# Role colours: low saturation, readable on the matching background. 'hex' for 256 / true colour.
_DARK = {"user": "#d7d7d7", "manager": "#5fafd7", "worker": "#87af87", "worker2": "#af87d7", "worker3": "#d7af5f",
         "system": "#808080", "error": "#d75f5f", "ask": "#87d7ff", "dim": "#808080", "run": "#87af87",
         "status_bg": "#262626", "status_fg": "#bcbcbc"}
_LIGHT = {"user": "#303030", "manager": "#005f87", "worker": "#3a6b3a", "worker2": "#6b3f9a", "worker3": "#8a6200",
          "system": "#6c6c6c", "error": "#af0000", "ask": "#005f87", "dim": "#6c6c6c", "run": "#3a6b3a",
          "status_bg": "#e4e4e4", "status_fg": "#303030"}
# 16-colour terminals: ANSI names, the terminal's own palette decides the exact shade.
_ANSI16 = {"user": "", "manager": "ansicyan", "worker": "ansigreen", "worker2": "ansimagenta", "worker3": "ansiyellow",
           "system": "ansibrightblack", "error": "ansired", "ask": "ansibrightcyan", "dim": "ansibrightblack",
           "run": "ansigreen", "status_bg": "", "status_fg": ""}

WORKER_STYLES = ("worker", "worker2", "worker3")


def build_style(depth: Any, theme: str) -> Style:
    """Style table for a colour depth (prompt_toolkit.output.ColorDepth) and 'dark' / 'light'."""
    if depth == ColorDepth.DEPTH_1_BIT:
        pal: Dict[str, str] = {k: "" for k in _ANSI16}
    elif depth == ColorDepth.DEPTH_4_BIT:
        pal = _ANSI16
    else:
        pal = _LIGHT if theme == "light" else _DARK

    def fg(key: str, extra: str = "") -> str:
        c = pal[key]
        return " ".join(x for x in (c, extra) if x)

    mono = depth == ColorDepth.DEPTH_1_BIT
    status = "reverse" if (mono or not pal["status_bg"]) else f"bg:{pal['status_bg']} {pal['status_fg']}"
    return Style.from_dict({
        "me": fg("user", "bold"),
        "manager": fg("manager", "bold"),
        "worker": fg("worker", "bold"),
        "worker2": fg("worker2", "bold"),
        "worker3": fg("worker3", "bold"),
        "ask": fg("ask", "bold"),
        "body": "",
        "system": fg("system"),
        "error": fg("error", "bold"),
        "dim": fg("dim") if not mono else "",
        "replay": fg("dim") if not mono else "",
        "evidence": fg("dim", "italic" if mono else ""),
        "status": status,
        "status.run": status + " bold",
        "status.dot.run": fg("run", "bold"),
        "status.dot.idle": fg("dim"),
        "status.hint": fg("dim"),
        "title": fg("dim"),
        "input-title": fg("manager", "bold"),
        "prompt": fg("manager", "bold"),
    })


def worker_style(index: int) -> str:
    return WORKER_STYLES[index % len(WORKER_STYLES)]
