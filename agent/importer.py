"""Importing a native Claude Code session: pre-flight checks, the import record kept in state.json, and its validation.

Nothing here calls a model. The checks are read-only: the environment, the sessions folder of THIS program, and a
metadata-only lookup (names, sizes, times - never contents) of Claude's own storage, which is an internal, unofficial
layout and is therefore only ever used as a hint, never as a fact.

Three ids are kept apart for the whole life of an imported session:
  source_id   the user's native Claude session. Only ever the `--resume` source of the one `--fork-session` request.
  fork_id     the UUID WE choose for the copy, saved before any CLI call. The Manager works in this session only.
  Agent id    this program's own session id (YYYYMMDD-HHMMSS-xxxxxx).
"""
from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
AGENT_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
# CLIs whose adapter speaks Claude's --resume/--fork-session contract. "fake" is the test double of the Claude adapter.
FORK_CAPABLE = ("claude", "fake")
RECENT_SEC = 120          # a transcript touched this recently may still be written by a live Claude window

# prepared : fork id saved, no CLI call made yet
# forking  : a fork request was started (or failed); its outcome is not confirmed. Retried with the SAME fork id
# forked   : the fork id is known to exist (the CLI said "already in use" after an earlier attempt); Manager not yet confirmed
# ready    : the Manager answered in the fork. From here on only `--resume <fork_id>`
# blocked  : cannot continue safely (e.g. the fork id existed before we ever tried); start a new import
STAGES = ("prepared", "forking", "forked", "ready", "blocked")


def is_claude_id(text: Any) -> bool:
    return isinstance(text, str) and bool(UUID_RE.match(text))


def new_import_state(source_id: str) -> Dict[str, Any]:
    return {"source_id": source_id.lower(), "fork_id": str(uuid.uuid4()), "stage": "prepared", "attempts": 0,
            "created": time.time()}


def validate_import_state(imp: Any, manager_session_id: Optional[str] = None) -> List[str]:
    """Problems that make a saved import record unusable. The caller refuses to resume when this is not empty."""
    if not isinstance(imp, dict):
        return ["the import record in state.json is not an object"]
    out: List[str] = []
    src, fork = imp.get("source_id"), imp.get("fork_id")
    if not is_claude_id(src):
        out.append("import.source_id is not a UUID")
    if not is_claude_id(fork):
        out.append("import.fork_id is not a UUID")
    if is_claude_id(src) and is_claude_id(fork) and src.lower() == fork.lower():
        out.append("import.fork_id equals the source session id")
    if imp.get("stage") not in STAGES:
        out.append(f"import.stage {imp.get('stage')!r} is unknown")
    if manager_session_id and is_claude_id(src) and manager_session_id.lower() == src.lower():
        out.append("the Manager's working session is the ORIGINAL Claude session (refusing to write to it)")
    if imp.get("stage") == "ready" and manager_session_id and is_claude_id(fork) \
            and manager_session_id.lower() != fork.lower():
        out.append("the Manager's session is not the saved fork id")
    return out


def claude_config_dir(env: Optional[Dict[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def encode_project(cwd: str) -> str:
    """How Claude Code names a project folder after a working directory (official docs: non-alphanumerics become `-`).
    Used only to GUESS where a session was created."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def find_native_transcripts(source_id: str, claude_dir: Optional[Path] = None) -> List[Path]:
    """Metadata-only lookup of `<claude dir>/projects/*/<id>.jsonl`. The file is never opened."""
    base = (claude_dir or claude_config_dir()) / "projects"
    try:
        return sorted(base.glob(f"*/{source_id}.jsonl"))
    except OSError:
        return []


def existing_imports(sessions_root: Path, source_id: str) -> List[str]:
    """Agent sessions that already imported this Claude session. A directory scan, not an atomic guard: two processes
    importing the same id at the same moment can both pass it."""
    import json
    out: List[str] = []
    try:
        names = sorted(p.name for p in Path(sessions_root).iterdir() if (p / "state.json").is_file())
    except OSError:
        return out
    for name in names:
        try:
            st = json.loads((Path(sessions_root) / name / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        imp = st.get("import") if isinstance(st, dict) else None
        if isinstance(imp, dict) and str(imp.get("source_id", "")).lower() == source_id.lower():
            out.append(name)
    return out


def _size(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.1f} KB"


@dataclass
class Preflight:
    source_id: str
    cwd: str
    blockers: List[str] = field(default_factory=list)      # the import must not start
    info: List[str] = field(default_factory=list)          # shown before asking for confirmation
    warnings: List[str] = field(default_factory=list)
    folder_mismatch: bool = False                          # a GUESS from Claude's internal folder naming
    existing: List[str] = field(default_factory=list)


def preflight(source_id: str, cwd: str, sessions_root: Path, manager_cli: str,
              env: Optional[Dict[str, str]] = None, claude_dir: Optional[Path] = None,
              now: Optional[float] = None) -> Preflight:
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    pf = Preflight(source_id=source_id.lower(), cwd=cwd)
    if not is_claude_id(source_id):
        pf.blockers.append(f"{source_id!r} is not a Claude session id (a UUID such as "
                           "123e4567-e89b-12d3-a456-426614174000). Names and paths are not accepted.")
        return pf
    if env.get("CLAUDE_CODE_SESSION_ID"):
        pf.blockers.append("CLAUDE_CODE_SESSION_ID is set: this shell runs inside a Claude Code session, where a fork can "
                           "end up under the outer session's id. Open a plain PowerShell window and run it there.")
    if manager_cli not in FORK_CAPABLE:
        pf.blockers.append(f"the configured Manager runs on {manager_cli!r}; only a Claude Manager can continue a "
                           "Claude session (check `agent config`).")
    pf.existing = existing_imports(sessions_root, source_id)
    if pf.existing:
        pf.blockers.append("this Claude session was already imported as Agent session "
                           + ", ".join(pf.existing) + ". Continue it with: agent resume " + pf.existing[0])
    found = find_native_transcripts(source_id, claude_dir)
    if len(found) == 1:
        t = found[0]
        try:
            stt = t.stat()
            pf.info.append(f"本機找到該 Session 的紀錄檔（只讀取檔案資訊）：{_size(stt.st_size)}。"
                           "檔案大小不等於 token 數；實際用量取決於 Claude 載入的上下文（可能含已壓縮的摘要）。")
            if now - stt.st_mtime < RECENT_SEC:
                pf.warnings.append(f"該紀錄 {int(now - stt.st_mtime)} 秒前才被寫入，可能仍在某個 Claude 視窗中使用。"
                                   "建議先在那個視窗 /exit。副本不會影響原視窗，但複製的內容可能不是最新的。")
        except OSError:
            pass
        want = encode_project(cwd)
        if os.path.normcase(t.parent.name) != os.path.normcase(want):
            pf.folder_mismatch = True
            pf.warnings.append(
                "【推測，非官方保證】依 Claude 內部的資料夾命名，這個 Session 似乎是在另一個資料夾建立的"
                f"（紀錄位於 {t.parent.name!r}，目前資料夾對應 {want!r}）。Manager 的工具會在目前資料夾執行。")
    elif len(found) > 1:
        pf.warnings.append(f"在 {len(found)} 個專案資料夾都找到同名紀錄，無法判斷它來自哪個資料夾。")
    else:
        pf.warnings.append("在本機 Claude 儲存位置找不到這個 Session（可能在另一個 CLAUDE_CONFIG_DIR、已超過保留期限，"
                           "或 Claude 版本的儲存位置不同）。仍可嘗試：是否存在由 Claude CLI 判斷，失敗時不會建立任何東西。")
    return pf


def confirmation_text(pf: Preflight) -> str:
    lines = [
        "匯入 Claude 原生 Session",
        f"  來源 Session : {pf.source_id}",
        f"  工作資料夾   : {pf.cwd}",
        "  原始對話     : 保持不變（只作為 --resume --fork-session 的來源，之後不再被使用）",
        "  Fork         : 第一則訊息才會建立副本（此刻不呼叫模型）；每次 Manager 請求都會把副本的完整上下文送給模型，",
        "                 可能消耗大量額度，長對話尤其明顯",
        "  設定         : Claude 的 headless 模式可能載入此資料夾的專案 hooks、MCP 與設定",
        "  權限         : 以新的 -p 模式執行，不會帶入原互動 Session 的權限模式或「本 Session 允許」；本工具不會放寬權限",
        "  聊天紀錄     : 匯入前的原生對話不會顯示在聊天室中，只記錄匯入之後的 Agent 團隊對話",
    ]
    lines += ["  " + i for i in pf.info]
    lines += ["  注意：" + w for w in pf.warnings]
    return "\n".join(lines)
