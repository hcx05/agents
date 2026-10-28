"""Command line entry: `agent`, `agent config`, in-chat /status /interrupt /exit."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__, importer
from .adapters import get_adapter
from .config import ConfigError, config_path, ensure_config, home_dir, load_config
from .orchestrator import Engine
from .resume import check_compat, format_sessions, replay, timeline
from .sessionlock import SessionLock, SessionLocked
from .store import Store, StoreError, safe_session_dir
from .ui import UI, ChatUI, RecordingUI

HELP_IN_CHAT = "Commands: /status  /interrupt  /exit.  Anything else is sent to the Manager."
HELP_RESUMED = ("Commands: /status  /continue  /interrupt  /exit.  /continue starts the work that was kept on hold; "
                "typing a message does too.")


IMPORT_BOUNDARY = ("── 已匯入 Claude Session {src} ──\n"
                   "Fork 副本 ID：{fork}（預先保存；副本於第一則訊息時才建立）。\n"
                   "匯入前的原生對話不在本聊天室紀錄中，可用 `claude --resume {src}` 在原生 Claude 查看；"
                   "以下只記錄匯入之後的 Agent 團隊對話。")
IMPORT_REPLAY_NOTE = ("匯入前的 Claude 原生歷史（Session {src}）：未顯示，也不在此紀錄中。"
                      "以下是匯入之後的 Agent 聊天室紀錄。")


def _setup_stdio() -> None:
    for s in (sys.stdout, sys.stderr, sys.stdin):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def cmd_config(args: argparse.Namespace) -> int:
    path = Path(args.config) if args.config else config_path()
    created = not path.exists()
    ensure_config(path)
    print(f"Config file: {path}" + ("  (created with defaults)" if created else ""))
    print("Edit it with any text editor (JSON). null = use the CLI's own default model/effort.")
    try:
        cfg = load_config(path, create=False)
        print("Status: valid.  manager=" + cfg.manager + "  workers=" + ", ".join(w.id for w in cfg.workers))
    except ConfigError as e:
        print(f"Status: INVALID - {e}")
        return 1
    if args.show:
        print(path.read_text(encoding="utf-8"))
    print(f"Sessions/logs are stored in: {home_dir() / 'sessions'}")
    return 0


def _tui_broken(e: BaseException) -> str:
    return (f"全螢幕聊天室模組載入失敗，這不是缺少套件，而是版本不相容或程式錯誤：{type(e).__name__}: {e}。"
            "已改用簡易模式（--plain 可不再顯示此訊息）；請回報這個錯誤，或確認 prompt_toolkit 版本 >= 3.0.36。")


def _tui_decision(args: argparse.Namespace) -> Tuple[bool, Optional[str]]:
    """Full-screen chat room only for a real interactive terminal. Anything else (pipe, redirect, --plain, AGENT_PLAIN,
    TERM=dumb, prompt_toolkit missing) gets the original line-based chat. Returns (use_tui, hint for stderr)."""
    if getattr(args, "plain", False) or os.environ.get("AGENT_PLAIN") or os.environ.get("TERM", "").lower() == "dumb":
        return False, None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False, None
    try:
        importlib.import_module(f"{__package__}.tui")
    except ModuleNotFoundError as e:
        if (e.name or "").split(".")[0] in ("prompt_toolkit", "wcwidth"):        # the dependency itself is missing
            return False, ("全螢幕聊天室需要 prompt_toolkit，目前沒有安裝。改用簡易模式；"
                           "要啟用全螢幕請執行：pip install \"prompt_toolkit>=3.0.36\"（或加 --plain 不再提示）")
        return False, _tui_broken(e)
    except Exception as e:                   # installed but unusable (old version, a bug in the TUI module): say so
        return False, _tui_broken(e)
    return True, None


def _run_slash(engine: Engine, ui: UI, cmd: str) -> bool:
    """Returns True when the chat should exit."""
    name = cmd.split()[0].lower()
    if name == "/exit":
        return True
    if name == "/status":
        ui.system(engine.status_text())
    elif name == "/interrupt":
        engine.interrupt()
        engine.wait_idle(30)
    elif name == "/continue":
        engine.release()
    else:
        ui.system(f"Unknown command {name}. {HELP_IN_CHAT}")
    return False


def _tui_line(holder: Dict[str, Any], s: str) -> None:
    """One submitted line from the full-screen input. Runs on the UI thread, so anything that can wait goes to a helper
    thread; a plain message is just a queue put."""
    engine, ui, app = holder["engine"], holder["ui"], holder["app"]
    if not s.startswith("/"):
        engine.submit(s)
        return
    name = s.split()[0].lower()
    if name == "/exit":
        app.request_exit()
    elif name in ("/interrupt", "/status"):
        threading.Thread(target=_run_slash, args=(engine, ui, s), daemon=True, name="slash").start()
    else:
        _run_slash(engine, ui, s)


def _tui_ctrl_c(holder: Dict[str, Any]) -> bool:
    """Ctrl+C in the full screen: interrupt running work. True when there was something to interrupt."""
    snap = holder["status"]() or {}
    busy = any(a.get("running") or a.get("queued") for a in snap.get("agents") or []) or snap.get("open_tasks")
    if not busy:
        return False
    holder["engine"].interrupt()
    return True


def cmd_chat(args: argparse.Namespace) -> int:
    return _run_chat(args, None)


def cmd_sessions(args: argparse.Namespace) -> int:
    print(format_sessions(Store.summaries(home_dir() / "sessions"), args.limit))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    if importer.is_claude_id(args.session_id):
        print(f"{args.session_id} is a Claude session id, not an Agent session id.\n"
              f"  To import it:        agent {args.session_id}\n"
              "  To continue an Agent session: agent resume <id>   (ids are listed by `agent sessions`)", file=sys.stderr)
        return 2
    return _run_chat(args, args.session_id)


def cmd_import(args: argparse.Namespace) -> int:
    return _run_chat(args, None, import_id=args.claude_session_id)


def _confirm_import(pf: "importer.Preflight", args: argparse.Namespace) -> bool:
    """Show what an import does and get an explicit yes. Nothing has been created and no model has been called yet.
    A folder that merely LOOKS different (a guess) needs its own explicit yes: --here, or a second answer."""
    print(importer.confirmation_text(pf))
    yes, here = getattr(args, "yes", False), getattr(args, "here", False)
    if not sys.stdin.isatty():
        if not yes:
            print("Not a terminal: add --yes to confirm the import.", file=sys.stderr)
            return False
        if pf.folder_mismatch and not here:
            print("The folder looks different from the one the session was created in (a guess). "
                  "Add --here to import it anyway, or `cd` to the original folder.", file=sys.stderr)
            return False
        return True
    if not yes and not _ask_yes("確定匯入嗎？"):
        return False
    if pf.folder_mismatch and not here:
        return _ask_yes("資料夾疑似不同（推測）。仍要在目前資料夾匯入嗎？")
    return True


def _ask_yes(question: str) -> bool:
    try:
        return input(f"{question} 輸入 yes 繼續：").strip().lower() in ("yes", "y")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _run_chat(args: argparse.Namespace, resume_id: Optional[str], import_id: Optional[str] = None) -> int:
    resume_dir: Optional[Path] = None
    if resume_id is not None:
        try:
            resume_dir = safe_session_dir(home_dir() / "sessions", resume_id)      # before ANY file access
        except StoreError as e:
            print(f"Cannot resume: {e}", file=sys.stderr)
            return 2
        if not (resume_dir / "state.json").is_file():
            print(f"Session {resume_id!r} not found. `agent sessions` lists the saved ones.", file=sys.stderr)
            return 2
    path = Path(args.config) if args.config else None
    try:
        cfg = load_config(path)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    cwd = os.getcwd()
    adapters = {}
    problems = []
    for aid, ac in cfg.agents.items():
        ad = get_adapter(ac)
        adapters[aid] = ad
        msg = ad.check_available()
        if msg:
            problems.append(f"  {aid} ({ac.cli}): {msg}")
    if problems:
        print("Cannot start; CLI not available:\n" + "\n".join(problems), file=sys.stderr)
        print("Install/login the CLI, make sure it runs in this PowerShell, or set agents.<id>.command in "
              f"{path or config_path()}", file=sys.stderr)
        return 2

    root = home_dir() / "sessions"
    resumed = resume_id is not None
    imp_state = None
    if import_id is not None:
        pf = importer.preflight(import_id, cwd, root, cfg.agents[cfg.manager].cli)
        if pf.blockers:
            print("Cannot import:\n" + "\n".join("  - " + b for b in pf.blockers), file=sys.stderr)
            return 2
        if not _confirm_import(pf, args):
            print("Import cancelled. Nothing was created and the model was not called.")
            return 1
        imp_state = importer.new_import_state(import_id)
    if resumed:
        lock = SessionLock(resume_dir)
        try:
            lock.acquire()                       # before reading the state: only one writer per session
        except SessionLocked as e:
            print(f"Cannot resume {resume_id}: {e}", file=sys.stderr)
            return 2
        try:
            store = Store.open_existing(root, resume_id)
        except StoreError as e:
            lock.release()
            print(f"Cannot resume {resume_id}: {e}", file=sys.stderr)
            return 2
        errors, warns = check_compat(cfg, store.state, cwd, here=getattr(args, "here", False))
        if errors:
            lock.release()
            print(f"Cannot resume {resume_id}:\n" + "\n".join("  - " + e for e in errors), file=sys.stderr)
            return 2
    else:
        store = Store(root)
        lock = SessionLock(store.dir)
        try:
            lock.acquire()
        except SessionLocked as e:               # a brand-new directory should never be locked
            print(f"Cannot start: {e}", file=sys.stderr)
            return 2
        warns = []
        if imp_state is not None:
            store.update(**{"import": imp_state})          # the fork id is on disk before anything else happens
    # From here to the end every path - including a failure while the screen or the replay is being set up - passes
    # through the `finally`: the Engine (if started) is shut down and the Session lock is released.
    started = False
    tuiapp = None
    exit_code = 0
    try:
        use_tui, hint = _tui_decision(args)
        if hint:
            print(hint, file=sys.stderr)
        holder: Dict[str, Any] = {}
        if use_tui:
            try:
                from .tui import TuiApp
                tuiapp = TuiApp(lambda s: _tui_line(holder, s), lambda: holder["status"](), lambda: _tui_ctrl_c(holder))
            except Exception as e:               # e.g. a console prompt_toolkit cannot drive (mintty): chat as before
                print(f"全螢幕聊天室無法在這個終端啟動（{type(e).__name__}: {e}）。改用簡易模式（可加 --plain）。", file=sys.stderr)
                tuiapp = None
        plain = tuiapp.ui if tuiapp else ChatUI()
        ui = RecordingUI(plain, store)
        engine = Engine(cfg, store, ui, cwd, adapter_factory=lambda ac: adapters[ac.id], resume=resumed)
        engine.start()
        started = True
        holder.update(engine=engine, ui=ui, app=tuiapp, status=engine.activity_snapshot)
        mgr = cfg.agents[cfg.manager]
        ui.banner(["Agent Team",
                   f"Manager: {adapters[mgr.id].display_name}" + (f" ({mgr.model})" if mgr.model else ""),
                   *[f"Worker: {adapters[w.id].display_name} [{w.id}]" + (f" ({w.model})" if w.model else "")
                     for w in cfg.workers],
                   *([f"Imported Claude session: {store.state['import']['source_id']}  (copy: {store.state['import']['fork_id']})"]
                     if store.state.get("import") else []),
                   f"Folder: {cwd}", f"Session log: {store.dir}", HELP_RESUMED if resumed else HELP_IN_CHAT])
        if imp_state is not None:
            ui.system(IMPORT_BOUNDARY.format(src=imp_state["source_id"], fork=imp_state["fork_id"]))
        if resumed:
            if store.state.get("import"):
                plain.system(IMPORT_REPLAY_NOTE.format(src=store.state["import"]["source_id"]))
            shown = replay(tuiapp.ui.replay_view() if tuiapp else plain, timeline(Store.read_events(store.dir)),
                           getattr(args, "replay", 60))
            for w in warns:
                ui.system("注意：" + w)
            for kind, text in engine.restore_report:
                (ui.ask_user if kind == "ask_user" else ui.system)(text)
        engine.check_cli_versions()          # background; a slow or failing --version never delays or blocks the chat
        interactive = sys.stdin.isatty()
        try:
            if tuiapp is not None:
                try:
                    tuiapp.run()
                except Exception as e:           # the screen failed; the engine and the saved session are untouched
                    exit_code = 1
                    print(f"全螢幕聊天室發生錯誤：{type(e).__name__}: {e}\n工作狀態已保存，可用 `agent resume {store.id}` "
                          "繼續（加 --plain 使用簡易模式）。", file=sys.stderr)
                return exit_code
            while True:
                try:
                    if not interactive:
                        engine.wait_idle(cfg.turn_timeout_sec * (cfg.max_manager_turns_per_input + 2))
                    line = ui.read_line()
                except KeyboardInterrupt:
                    if engine.is_quiescent():
                        ui.system("Type /exit to quit.")
                    else:
                        engine.interrupt()
                    continue
                if line is None:
                    if not interactive:
                        engine.wait_idle(cfg.turn_timeout_sec * (cfg.max_manager_turns_per_input + 2))
                    break
                s = line.strip()
                if not s:
                    continue
                if s.startswith("/"):
                    if _run_slash(engine, ui, s):
                        break
                    continue
                engine.submit(s)
        except KeyboardInterrupt:
            pass
    finally:
        try:
            if started:                  # an Engine that never started has nothing to stop (shutdown() would wait)
                engine.shutdown()
                bye = f"Bye. Session saved: {store.dir}" + (f"  (resume with: agent resume {store.id})")
                ui.system(bye)
                if tuiapp is not None:
                    print(bye)           # the full screen is gone: say it on the normal terminal
        finally:
            lock.release()               # whatever failed above, the session lock is let go (release() is idempotent)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent",
        description="Hierarchical AI agent team (Manager + Worker) in a terminal chat room. "
                    "Run `agent` to start; `agent config` shows/creates the config file.")
    p.add_argument("--version", action="version", version=f"agent {__version__}")
    p.add_argument("--config", help="use this config file instead of %%APPDATA%%\\agent\\config.json")
    p.add_argument("--plain", action="store_true", default=False,
                   help="line-based chat instead of the full-screen chat room (also used when not on a terminal)")
    sub = p.add_subparsers(dest="command")
    r = sub.add_parser("sessions", help="list saved Agent sessions")
    r.add_argument("--limit", type=int, default=20, help="show at most this many (newest)")
    r.add_argument("--config", dest="config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    r = sub.add_parser("resume", help="continue a saved Agent session (its Manager/Worker CLI sessions and chat history)")
    r.add_argument("session_id", help="Agent session id (see `agent sessions`)")
    r.add_argument("--replay", type=int, default=60, help="show the last N earlier chat items first (0 = none)")
    r.add_argument("--here", action="store_true", help="continue even though the current folder differs from the saved one")
    r.add_argument("--plain", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    r.add_argument("--config", dest="config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    i = sub.add_parser("import", help="import a native Claude Code session (also: agent <Claude Session UUID>): "
                                      "the Manager continues a COPY of it; the original is never modified")
    i.add_argument("claude_session_id", help="the Claude Code session id (UUID)")
    i.add_argument("--yes", action="store_true", help="confirm the import without the interactive question")
    i.add_argument("--here", action="store_true",
                   help="import even though the session seems to come from another folder (a guess from Claude's storage)")
    i.add_argument("--config", dest="config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    i.add_argument("--plain", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    c = sub.add_parser("config", help="create the config file if missing and show its path")
    c.add_argument("--show", action="store_true", help="also print the file contents")
    c.add_argument("--config", dest="config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return p


COMMANDS = ("sessions", "resume", "import", "config")


def rewrite_argv(argv: List[str]) -> Optional[List[str]]:
    """`agent <Claude Session UUID>` means `agent import <uuid>`. Returns None (after printing why) when the first word is an
    Agent session id, which needs `resume`. Everything else is left exactly as typed."""
    i = 0
    while i < len(argv):
        t = argv[i]
        if t == "--config":
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        if t in COMMANDS:
            return argv
        if importer.is_claude_id(t):
            return argv[:i] + ["import"] + argv[i:]
        if importer.AGENT_ID_RE.match(t):
            print(f"{t} looks like an Agent session id. Continue it with:  agent resume {t}", file=sys.stderr)
            return None
        return argv
    return argv


def main(argv: Optional[List[str]] = None) -> int:
    _setup_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = rewrite_argv(argv)
    if argv is None:
        return 2
    args = build_parser().parse_args(argv)
    if args.command == "import":
        return cmd_import(args)
    if args.command == "config":
        return cmd_config(args)
    if args.command == "sessions":
        return cmd_sessions(args)
    if args.command == "resume":
        return cmd_resume(args)
    return cmd_chat(args)
