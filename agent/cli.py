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
from .store import Store
from .ui import ChatUI

HELP_IN_CHAT = "Commands: /status  /interrupt  /exit.  Anything else is sent to the Manager."


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
    else:
        ui.system(f"Unknown command {name}. {HELP_IN_CHAT}")
    return False


def cmd_chat(args: argparse.Namespace) -> int:
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

    store = Store(home_dir() / "sessions")
    ui = ChatUI()
    engine = Engine(cfg, store, ui, cwd, adapter_factory=lambda ac: adapters[ac.id])
    engine.start()
    mgr = cfg.agents[cfg.manager]
    ui.banner(["Agent Team",
               f"Manager: {adapters[mgr.id].display_name}" + (f" ({mgr.model})" if mgr.model else ""),
               *[f"Worker: {adapters[w.id].display_name} [{w.id}]" + (f" ({w.model})" if w.model else "")
                 for w in cfg.workers],
               f"Folder: {cwd}", f"Session log: {store.dir}", HELP_IN_CHAT])
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
        ui.system(f"Bye. Session saved: {store.dir}")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent",
        description="Hierarchical AI agent team (Manager + Worker) in a terminal chat room. "
                    "Run `agent` to start; `agent config` shows/creates the config file.")
    p.add_argument("--version", action="version", version=f"agent {__version__}")
    p.add_argument("--config", help="use this config file instead of %%APPDATA%%\\agent\\config.json")
    sub = p.add_subparsers(dest="command")
    c = sub.add_parser("config", help="create the config file if missing and show its path")
    c.add_argument("--show", action="store_true", help="also print the file contents")
    c.add_argument("--config", dest="config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    _setup_stdio()
    args = build_parser().parse_args(argv)
    if args.command == "config":
        return cmd_config(args)
    return cmd_chat(args)
