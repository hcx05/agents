"""Test harness: real Engine + real subprocess runner + fake CLI scripted by a scenario file."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.config import parse_config  # noqa: E402
from agent.orchestrator import Engine  # noqa: E402
from agent.store import Store  # noqa: E402
from agent.ui import BufferUI  # noqa: E402


def M(actions: List[dict], say: str = "", **extra) -> dict:
    d = {"reply": {"say": say, "actions": actions}}
    d.update(extra)
    return d


def dispatch(to: str, tid: str, text: str, **kw) -> dict:
    a = {"type": "dispatch", "to": to, "task_id": tid, "text": text, "writes": [], "model": None, "effort": None}
    a.update(kw)
    return a


def answer(to: str, tid: str, text: str) -> dict:
    return {"type": "answer", "to": to, "task_id": tid, "text": text}


def ask(text: str) -> dict:
    return {"type": "ask_user", "text": text}


def final(text: str) -> dict:
    return {"type": "final", "text": text}


def W(status: str, tid: str, text: str, files: Optional[List[str]] = None, **extra) -> dict:
    d = {"reply": {"status": status, "task_id": tid, "text": text, "files_touched": files or []}}
    d.update(extra)
    return d


class Rig:
    def __init__(self, scenario: Dict[str, list], timeout: float = 30, max_turns: int = 30,
                 worker_command: Optional[str] = None, manager_command: Optional[str] = None,
                 worker_model: Optional[str] = None):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent test 測試 "))   # spaces + CJK on purpose
        self.cwd = self.tmp / "work dir 工作"
        self.cwd.mkdir()
        self.fake_state = self.tmp / "fake state"
        self.fake_state.mkdir()
        (self.tmp / "scenario.json").write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
        os.environ["FAKE_SCENARIO"] = str(self.tmp / "scenario.json")
        os.environ["FAKE_STATE"] = str(self.fake_state)
        mgr: Dict[str, Any] = {"role": "manager", "cli": "fake", "model": None, "effort": None}
        wrk: Dict[str, Any] = {"role": "worker", "cli": "fake", "model": worker_model, "effort": None}
        if manager_command:
            mgr["command"] = manager_command
        if worker_command:
            wrk["command"] = worker_command
        self.cfg = parse_config({"manager": "manager", "agents": {"manager": mgr, "w1": wrk},
                                 "turn_timeout_sec": timeout, "max_manager_turns_per_input": max_turns})
        self.store = Store(self.tmp / "sessions")
        self.ui = BufferUI()
        self.engine = Engine(self.cfg, self.store, self.ui, str(self.cwd))
        self.engine.start()

    def say(self, text: str, wait: float = 60) -> None:
        self.engine.submit(text)
        assert self.engine.wait_idle(wait), "engine did not become idle:\n" + self.dump()

    def wait_until(self, pred, timeout: float = 20) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(0.05)
        return False

    def calls(self, agent: Optional[str] = None) -> List[dict]:
        p = self.fake_state / "calls.jsonl"
        if not p.exists():
            return []
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        return [r for r in rows if agent is None or r["agent"] == agent]

    def events(self, type_: Optional[str] = None) -> List[dict]:
        rows = [json.loads(l) for l in self.store.events_path.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if type_ is None or r["type"] == type_]

    def chat_lines(self) -> List[str]:
        out = []
        for k, s, t, to in self.ui.lines:
            if k == "chat":
                out.append(f"{s}> " + (f"@{to} " if to else "") + t)
        return out

    def dump(self) -> str:
        return "\n".join(f"{k}|{s}|{t}|{to}" for k, s, t, to in self.ui.lines) + f"\nstate={self.engine.state}"

    def close(self) -> None:
        self.engine.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)
