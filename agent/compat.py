"""CLI version awareness. Read-only: runs the official `--version` (no model call, short timeout) so the version is
recorded for debugging and unfamiliar versions get a soft notice. It never updates, downgrades or blocks anything."""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import runner

# cli -> {version: (status, note)}. Keep this honest: "tested" = exercised on a real Windows install for the paths
# listed in the note; "partial" = some paths are known to be unverified. Never put "full compatibility" here.
KNOWN: Dict[str, Dict[str, Tuple[str, str]]] = {
    "claude": {
        "2.1.312": ("tested", "Windows: probe suite, chat room (/status, ask_user, /interrupt) and real Manager->agy Worker->"
                              "Manager dispatch passed; real permission-denial detection not observed"),
    },
    "agy": {
        "1.4.9": ("partial", "Windows: JSON events, session resume, stdin streaming, --conversation, --json-schema, interrupt, "
                             "invalid-effort classification and real Claude Manager->agy Worker->Manager dispatch passed; "
                             "real permission-denial detection not observed"),
    },
}
VERSION_RE = re.compile(r"\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.]+)?")


def parse_version(text: str) -> Optional[str]:
    m = VERSION_RE.search(text or "")
    return m.group(0) if m else None


def assess(cli: str, version: Optional[str]) -> Tuple[str, str]:
    """-> (status, note). status: tested | partial | unverified | unknown."""
    if version is None:
        return "unknown", "could not read the version"
    known = KNOWN.get(cli, {})
    if version in known:
        return known[version]
    listed = ", ".join(known) or "none"
    return "unverified", f"version is not in the tested list ({listed}); it is still used as-is"


def check_version(cli: str, prefix: List[str], cwd: Optional[str], timeout: float = 8.0) -> Dict[str, Any]:
    """Run `<cli> --version`. Always returns a dict, never raises."""
    t0 = time.monotonic()
    out: List[str] = []
    info: Dict[str, Any] = {"cli": cli, "version": None, "raw": "", "ok": False, "status": "unknown", "note": ""}
    try:
        proc = runner.run_process(prefix + ["--version"], None, cwd, None, timeout, threading.Event(),
                                  lambda s, l: out.append(l))
        info["raw"] = (out[0].strip() if out else "")[:80]
        if proc.status == "timeout":
            info["note"] = f"--version did not finish within {timeout:g}s"
        elif proc.status == "start_failed":
            info["note"] = f"could not start: {proc.start_error}"
        elif proc.exit_code not in (0, None):
            info["note"] = f"--version exited with code {proc.exit_code}"
        else:
            info["version"] = parse_version("\n".join(out))
            info["ok"] = info["version"] is not None
            if not info["ok"]:
                info["note"] = "--version printed no recognizable version number"
    except Exception as e:  # a failed check must never stop the program
        info["note"] = f"version check failed: {type(e).__name__}: {e}"
    if info["ok"]:
        info["status"], info["note"] = assess(cli, info["version"])
    info["took"] = round(time.monotonic() - t0, 2)
    return info


def startup_notice(info: Dict[str, Any]) -> Optional[str]:
    """Text for the chat room, or None when nothing needs saying (tested/partial versions stay quiet; see /status)."""
    cli, status = info["cli"], info["status"]
    if status == "unverified":
        return f"{cli} {info['version']} 不在已測試版本清單內，仍會照常使用；若出現格式錯誤請回報版本。"
    if status == "unknown":
        return f"無法讀取 {cli} 版本（{info['note']}），不影響使用。"
    return None
