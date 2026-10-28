"""Helpers for `agent sessions` / `agent resume`: compatibility check, chat-room replay, listing."""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import Config


def _norm(path: Optional[str]) -> str:
    return os.path.normcase(os.path.abspath(path)) if path else ""


def saved_config(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Agent identities of a saved session: version 2 stores `config`, version 1 only the per-agent entries."""
    cfg = state.get("config")
    if isinstance(cfg, dict) and isinstance(cfg.get("agents"), dict):
        return {k: v for k, v in cfg["agents"].items() if isinstance(v, dict)}
    agents = state.get("agents") if isinstance(state.get("agents"), dict) else {}
    return {k: {"role": v.get("role"), "cli": v.get("cli"), "model": v.get("model"), "effort": v.get("effort")}
            for k, v in agents.items() if isinstance(v, dict)}


def check_compat(cfg: Config, state: Dict[str, Any], cwd: str, here: bool = False) -> Tuple[List[str], List[str]]:
    """-> (errors, warnings). Errors mean the saved CLI sessions cannot be continued safely with this setup."""
    errors: List[str] = []
    warns: List[str] = []
    saved = saved_config(state)
    if not saved:
        errors.append("this session has no saved agent information, it cannot be resumed")
    for aid, info in saved.items():
        cur = cfg.agents.get(aid)
        if cur is None:
            errors.append(f"agent {aid!r} of the saved session is missing from the current config")
            continue
        if info.get("role") and info["role"] != cur.role:
            errors.append(f"agent {aid!r}: role was {info['role']!r}, config now says {cur.role!r}")
        if info.get("cli") and info["cli"] != cur.cli:
            errors.append(f"agent {aid!r}: it ran on {info['cli']!r}, config now says {cur.cli!r}; "
                          "that CLI's session id would not exist there")
        for key in ("model", "effort"):
            if info.get(key) != getattr(cur, key):
                warns.append(f"agent {aid!r}: {key} was {info.get(key) or 'default'}, config now says "
                             f"{getattr(cur, key) or 'default'} (the new value applies from the next turn)")
    for aid in cfg.agents:
        if saved and aid not in saved:
            errors.append(f"agent {aid!r} exists in the current config but not in the saved session")
    old = state.get("cwd")
    if old and _norm(old) != _norm(cwd):
        msg = f"the session was started in {old!r}, you are in {cwd!r}"
        if here:
            warns.append(msg + " (continuing here because of --here; file snapshots now refer to this folder)")
        else:
            errors.append(msg + ". Run `cd` to the original folder, or add --here to continue in this one")
    return errors, warns


def timeline(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Chat-room items in the order they happened. Every displayed line has exactly ONE source event:
    chat -> chat lines (the user's and the agents'), ask_user -> questions, ui -> system notices and errors."""
    items: List[Dict[str, Any]] = []
    for e in events:
        t = e.get("type")
        if t == "chat" and isinstance(e.get("text"), str):
            items.append({"kind": "chat", "sender": str(e.get("sender") or "?"), "to": e.get("to"),
                          "text": e["text"], "ts": e.get("ts"), "turn": e.get("turn")})
        elif t == "ui" and isinstance(e.get("text"), str) and e.get("kind") in ("system", "error"):
            items.append({"kind": e["kind"], "sender": "system", "to": None, "text": e["text"], "ts": e.get("ts")})
        elif t == "ask_user" and isinstance(e.get("text"), str):
            items.append({"kind": "ask_user", "sender": "Manager", "to": "Me", "text": e["text"], "ts": e.get("ts")})
    return items


def replay(ui: Any, items: List[Dict[str, Any]], limit: int) -> int:
    """Print the last `limit` items through `ui` (the plain, NON-recording UI). Returns how many were shown."""
    if limit <= 0 or not items:
        return 0
    shown = items[-limit:]
    omitted = len(items) - len(shown)
    ui.system(f"── 以下是先前的對話（最近 {len(shown)} 則" + (f"，另有 {omitted} 則較早的未顯示" if omitted else "")
              + "）──")
    for it in shown:
        k = it["kind"]
        if k == "chat":
            ui.chat(it["sender"], it["text"], it.get("to"), it.get("turn"))
        elif k == "ask_user":
            ui.ask_user(it["text"])
        elif k == "error":
            ui.error(it["text"])
        else:
            ui.system(it["text"])
    ui.system("── 先前的對話到此為止 ──")
    return len(shown)


def format_sessions(rows: List[Dict[str, Any]], limit: int = 20) -> str:
    if not rows:
        return "No saved sessions."
    rows = sorted(rows, key=lambda r: r.get("updated") or 0)[-limit:]
    out = [f"{'ID':<28} {'UPDATED':<16} {'STATE':<16} {'CLIs':<12} FOLDER / FIRST MESSAGE"]
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["updated"])) if r.get("updated") else "?"
        flags = ("" if r["ok"] else " [state.json unreadable]") + (" [IN USE]" if r["locked"] else "") \
            + (" [v1]" if r.get("version") == 1 else "")
        first = f"  \"{r['first']}\"" if r.get("first") else ""
        out.append(f"{r['id']:<28} {when:<16} {str(r.get('state') or '?'):<16} {r.get('agents') or '?':<12} "
                   f"{r.get('cwd') or '?'}{first}{flags}")
    return "\n".join(out)
