"""Command line entry: `agent`, `agent config`, in-chat /status /interrupt /exit."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .adapters import get_adapter
from .config import ConfigError, config_path, ensure_config, home_dir, load_config
from .orchestrator import Engine
from .resume import check_compat, format_sessions, replay, timeline
from .sessionlock import SessionLock, SessionLocked
from .store import Store, StoreError, safe_session_dir
from .ui import ChatUI, RecordingUI

HELP_IN_CHAT = "Commands: /status  /interrupt  /exit.  Anything else is sent to the Manager."
HELP_RESUMED = ("Commands: /status  /continue  /interrupt  /exit.  /continue starts the work that was kept on hold; "
                "typing a message does too.")


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


def _run_slash(engine: Engine, ui: ChatUI, cmd: str) -> bool:
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


def cmd_chat(args: argparse.Namespace) -> int:
    return _run_chat(args, None)


def cmd_sessions(args: argparse.Namespace) -> int:
    print(format_sessions(Store.summaries(home_dir() / "sessions"), args.limit))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    return _run_chat(args, args.session_id)


def _run_chat(args: argparse.Namespace, resume_id: Optional[str]) -> int:
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
    plain = ChatUI()
    ui = RecordingUI(plain, store)
    try:
        engine = Engine(cfg, store, ui, cwd, adapter_factory=lambda ac: adapters[ac.id], resume=resumed)
    except Exception:
        lock.release()
        raise
    engine.start()
    mgr = cfg.agents[cfg.manager]
    ui.banner(["Agent Team",
               f"Manager: {adapters[mgr.id].display_name}" + (f" ({mgr.model})" if mgr.model else ""),
               *[f"Worker: {adapters[w.id].display_name} [{w.id}]" + (f" ({w.model})" if w.model else "")
                 for w in cfg.workers],
               f"Folder: {cwd}", f"Session log: {store.dir}", HELP_RESUMED if resumed else HELP_IN_CHAT])
    if resumed:
        shown = replay(plain, timeline(Store.read_events(store.dir)), getattr(args, "replay", 60))
        for w in warns:
            ui.system("注意：" + w)
        for kind, text in engine.restore_report:
            (ui.ask_user if kind == "ask_user" else ui.system)(text)
    engine.check_cli_versions()          # background; a slow or failing --version never delays or blocks the chat
    interactive = sys.stdin.isatty()
    exit_code = 0
    try:
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
        engine.shutdown()
        ui.system(f"Bye. Session saved: {store.dir}" + (f"  (resume with: agent resume {store.id})"))
        lock.release()
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent",
        description="Hierarchical AI agent team (Manager + Worker) in a terminal chat room. "
                    "Run `agent` to start; `agent config` shows/creates the config file.")
    p.add_argument("--version", action="version", version=f"agent {__version__}")
    p.add_argument("--config", help="use this config file instead of %%APPDATA%%\\agent\\config.json")
    sub = p.add_subparsers(dest="command")
    r = sub.add_parser("sessions", help="list saved Agent sessions")
    r.add_argument("--limit", type=int, default=20, help="show at most this many (newest)")
    r.add_argument("--config", dest="config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    r = sub.add_parser("resume", help="continue a saved Agent session (its Manager/Worker CLI sessions and chat history)")
    r.add_argument("session_id", help="Agent session id (see `agent sessions`)")
    r.add_argument("--replay", type=int, default=60, help="show the last N earlier chat items first (0 = none)")
    r.add_argument("--here", action="store_true", help="continue even though the current folder differs from the saved one")
    r.add_argument("--config", dest="config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    c = sub.add_parser("config", help="create the config file if missing and show its path")
    c.add_argument("--show", action="store_true", help="also print the file contents")
    c.add_argument("--config", dest="config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    _setup_stdio()
    args = build_parser().parse_args(argv)
    if args.command == "config":
        return cmd_config(args)
    if args.command == "sessions":
        return cmd_sessions(args)
    if args.command == "resume":
        return cmd_resume(args)
    return cmd_chat(args)
