"""Orchestrator: explicit state machine + mailboxes. One thread owns all state (the engine loop);
agent CLI turns run in short-lived worker threads that only post results back to the loop."""
from __future__ import annotations

import json
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import compat, importer, protocol, runner
from .adapters import get_adapter
from .adapters.base import Adapter, TurnRequest, TurnResult
from .config import AgentCfg, Config
from .snapshot import FileDiff, diff_snapshots, take_snapshot
from .store import STATE_VERSION, Store, redact
from .ui import UI


class State(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_WORKER = "waiting_worker"
    WAITING_MANAGER = "waiting_manager"
    WAITING_USER = "waiting_user"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    TIMEOUT = "timeout"


_S = State
_TERMINAL = {_S.DONE, _S.BLOCKED, _S.FAILED, _S.INTERRUPTED, _S.TIMEOUT}
TRANSITIONS: Dict[State, Set[State]] = {
    _S.IDLE: {_S.RUNNING},
    _S.RUNNING: {_S.WAITING_WORKER, _S.WAITING_MANAGER, _S.WAITING_USER, _S.IDLE} | _TERMINAL,
    _S.WAITING_WORKER: {_S.RUNNING, _S.WAITING_MANAGER, _S.WAITING_USER, _S.IDLE} | (_TERMINAL - {_S.DONE}),
    _S.WAITING_MANAGER: {_S.RUNNING, _S.WAITING_USER, _S.WAITING_WORKER, _S.IDLE} | (_TERMINAL - {_S.DONE}),
    _S.WAITING_USER: {_S.RUNNING, _S.WAITING_MANAGER, _S.WAITING_WORKER, _S.IDLE, _S.INTERRUPTED, _S.FAILED},
}
for _t in _TERMINAL:
    TRANSITIONS[_t] = {_S.IDLE, _S.RUNNING, _S.WAITING_WORKER, _S.WAITING_MANAGER, _S.WAITING_USER}


class IllegalTransition(Exception):
    pass


def check_transition(cur: State, new: State) -> None:
    if cur != new and new not in TRANSITIONS[cur]:
        raise IllegalTransition(f"{cur.value} -> {new.value}")


@dataclass
class Running:
    thread: threading.Thread
    cancel: threading.Event
    epoch: int
    started: float = field(default_factory=time.time)    # wall clock, display only
    t0: float = 0.0              # engine clock (monotonic) at turn start
    last_output: float = 0.0     # engine clock of the newest raw CLI line; written by the CLI reader threads
    lines: int = 0               # raw lines seen (display / tests)
    warn_basis: float = -1.0     # last_output value the silence timer was armed for
    next_warn: float = 0.0       # engine clock at which the next "no new output" notice is due
    req_model: Optional[str] = None     # model WE asked the CLI for in this turn (not necessarily what ran)
    req_effort: Optional[str] = None
    turn_id: str = ""
    sent_pre: bool = False       # this turn's prompt carried the team preamble


@dataclass
class AgentRT:
    id: str
    cfg: AgentCfg
    adapter: Adapter
    session_id: Optional[str] = None
    inbox: List[Dict[str, Any]] = field(default_factory=list)
    running: Optional[Running] = None
    preamble_sent: bool = False
    reinject: bool = False         # the CLI compacted the conversation: repeat the preamble in the next turn
    repair_used: bool = False
    active_task: Optional[str] = None
    turns: int = 0
    inflight: List[Dict[str, Any]] = field(default_factory=list)   # mail handed to the CLI turn that is running now
    last_turn: Optional[Dict[str, Any]] = None                     # turn info (model facts) of the newest finished turn

    @property
    def role(self) -> str:
        return self.cfg.role


@dataclass
class TurnDone:
    agent_id: str
    epoch: int
    res: TurnResult
    diff: Optional[FileDiff]


def fmt_elapsed(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m{sec % 60:02d}s"


def fmt_quiet(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec} 秒" if sec < 120 else f"{sec // 60} 分 {sec % 60:02d} 秒"


def turn_info(a: "AgentRT", turn_id: str, requested: Optional[str], effort: Optional[str],
              res: Optional[TurnResult]) -> Dict[str, Any]:
    """Model facts of ONE turn. `reported` = the CLI said so; `requested` = we asked for it; neither = unknown.
    Requested is never promoted to reported."""
    reported = res.model_reported if res else None
    models = list(res.models_all) if res else []
    source = "reported" if reported else ("requested" if requested else "unknown")
    return {"turn": turn_id, "agent": a.id, "cli": a.cfg.cli, "requested": requested, "effort": effort,
            "reported": reported, "models": models, "source": source}


def model_label(info: Dict[str, Any]) -> str:
    if info["source"] == "reported":
        extra = f" +{len(info['models']) - 1}" if len(info.get("models") or []) > 1 else ""
        return f"{info['reported']}{extra}（CLI 回報）"
    if info["source"] == "requested":
        return f"要求使用 {info['requested']}（CLI 未確認）"
    return "模型未知（CLI 未回報）"


ERROR_HINTS = {
    "session_lost": "該 Session 已不存在或與要求的不一致（可能超過 CLI 保留期限、被刪除，或 CLI 改用了別的 Session）；"
                    "本工具不會自動改用新 Session，也不會覆寫已保存的 Session ID",
    "session_busy": "該 Claude Session 正在背景或另一個程序中執行；請先在原生 Claude 結束或 attach 它，本工具不會強行寫入同一個 Session",
    "fork_failed": "Fork 沒有取得預定的 Fork ID，結果已捨棄，原 Session 未被使用",
    "fork_exists": "Claude CLI 拒絕建立這個 Fork ID（已存在）；本工具不會換用其他 ID",
}


def _mail_text(m: Dict[str, Any]) -> str:
    return str(m.get("text", ""))


def _redact_obj(o: Any) -> Any:
    """Secret-redact every string inside a JSON-able structure (state.json is not a log, but it must not keep tokens)."""
    if isinstance(o, str):
        return redact(o)
    if isinstance(o, list):
        return [_redact_obj(x) for x in o]
    if isinstance(o, dict):
        return {k: _redact_obj(v) for k, v in o.items()}
    return o


def _clean_mail(lst: Any) -> List[Dict[str, Any]]:
    return [dict(m) for m in (lst if isinstance(lst, list) else []) if isinstance(m, dict) and isinstance(m.get("kind"), str)]


class Engine:
    def __init__(self, cfg: Config, store: Store, ui: UI, cwd: str,
                 adapter_factory: Callable[[AgentCfg], Adapter] = get_adapter,
                 clock: Callable[[], float] = time.monotonic, watch_tick: float = 1.0, resume: bool = False):
        self.cfg, self.store, self.ui, self.cwd = cfg, store, ui, cwd
        self.clock = clock                 # elapsed-time source; tests inject a fake one
        self._tick = watch_tick            # how often the idle loop re-checks for silent turns
        self.agents: Dict[str, AgentRT] = {
            aid: AgentRT(aid, ac, adapter_factory(ac)) for aid, ac in cfg.agents.items()}
        self.mgr = self.agents[cfg.manager]
        self.worker_ids = [a.id for a in self.agents.values() if a.role == "worker"]
        self.state = State.IDLE
        self.tasks: Dict[str, Dict[str, Any]] = store.state["tasks"]
        self.awaiting_user: Optional[Dict[str, Any]] = None
        self.held: List[Dict[str, Any]] = []          # worker mail held while the user is being asked
        self.pending_notes: List[str] = []             # system notes for the manager's next turn
        self.manager_turns = 0
        self.epoch = 0
        self._events: "queue.Queue" = queue.Queue()
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stopped = False
        self.transitions_log: List[str] = []
        self.cli_versions: Dict[str, Dict[str, Any]] = {}
        self.on_tick: Optional[Callable[[], None]] = None   # called on every idle tick (the session-lock heartbeat)
        self.hold = False                              # after a resume: nothing starts until the user says so
        self.restore_report: List[str] = []            # what the resume found; the caller prints it through its UI
        self._cur_turn: Optional[Dict[str, Any]] = None
        self._model_seen: Dict[str, str] = {}
        self._persisted = ""
        imp = store.state.get("import")
        self.imp: Optional[Dict[str, Any]] = imp if isinstance(imp, dict) else None   # same object as in state.json
        if self.imp is not None:
            problems = importer.validate_import_state(self.imp)
            if problems:
                raise ValueError("damaged import record: " + "; ".join(problems))
        store.update(cwd=cwd, version=STATE_VERSION,
                     config={"manager": cfg.manager,
                             "agents": {aid: {"role": a.role, "cli": a.cfg.cli, "model": a.cfg.model,
                                              "effort": a.cfg.effort} for aid, a in self.agents.items()}})
        if resume:
            self._restore()
        else:
            store.update(state=self.state.value)
            for a in self.agents.values():
                store.set_agent(a.id, role=a.role, cli=a.cfg.cli, model=a.cfg.model, effort=a.cfg.effort,
                                session_id=None, preamble_sent=False)

    # ------------------------------------------------------------ persistence / resume
    def _persist(self) -> None:
        """Write the transient queues to state.json so a restart cannot lose them. Called after every event that
        the loop handled (low frequency, never per CLI line) and only writes when something changed."""
        snap = {"agents": {aid: {"inbox": a.inbox, "inflight": a.inflight, "active_task": a.active_task,
                                 "repair_used": a.repair_used} for aid, a in self.agents.items()},
                "held": self.held, "pending_notes": self.pending_notes}
        snap = _redact_obj(snap)
        key = json.dumps(snap, sort_keys=True, ensure_ascii=False)
        if key != self._persisted:
            self._persisted = key
            self.store.update(mail=snap)

    def _agents_with_ok_turn(self) -> Set[str]:
        return {r.get("agent") for r in Store.iter_events(self.store.dir)
                if r.get("type") == "turn_end" and r.get("ok") is True}

    def _restore(self) -> None:
        """Rebuild runtime state from a saved session (see Store.open_existing). Rules:
        - session ids and preamble_sent come back as saved, so a successful preamble is never sent twice;
        - a turn that was running when the old process ended has an UNKNOWN outcome: it is never treated as done and
          never re-run. Worker tasks become `interrupted`, the user is told which of their messages were in flight;
        - queued messages / worker reports / notes come back and wait (`hold`) until the user says /continue or types;
        - a pending ask_user question comes back with the held mail behind it."""
        st = self.store.state
        rep: List[Tuple[str, str]] = self.restore_report
        legacy = (self.store.loaded_version or 1) < 2
        mail = st.get("mail") if isinstance(st.get("mail"), dict) else {}
        magents = mail.get("agents") if isinstance(mail.get("agents"), dict) else {}
        saved = st.get("agents") if isinstance(st.get("agents"), dict) else {}
        ok_turns: Optional[Set[str]] = None
        for aid, a in self.agents.items():
            info = saved.get(aid) if isinstance(saved.get(aid), dict) else {}
            a.session_id = info["session_id"] if isinstance(info.get("session_id"), str) else None
            if isinstance(info.get("preamble_sent"), bool):
                a.preamble_sent = info["preamble_sent"]
            else:                                       # version 1 never saved it: infer from the turn log
                if ok_turns is None:
                    ok_turns = self._agents_with_ok_turn()
                a.preamble_sent = bool(a.session_id) and aid in ok_turns
            a.turns = info["turns"] if isinstance(info.get("turns"), int) else 0
            a.reinject = bool(info.get("reinject"))
            if isinstance(info.get("last_model"), str):
                self._model_seen[aid] = info["last_model"]
            if isinstance(info.get("last_turn"), dict):
                a.last_turn = info["last_turn"]
            am = magents.get(aid) if isinstance(magents.get(aid), dict) else {}
            a.inbox = _clean_mail(am.get("inbox"))
            a.inflight = _clean_mail(am.get("inflight"))
            a.active_task = am["active_task"] if isinstance(am.get("active_task"), str) else None
            a.repair_used = bool(am.get("repair_used"))
        self.held = _clean_mail(mail.get("held"))
        self.pending_notes = [str(x) for x in (mail.get("pending_notes") or []) if isinstance(x, str)]
        au = st.get("awaiting_user")
        self.awaiting_user = au if isinstance(au, dict) and au.get("text") else None
        self.store.update(awaiting_user=self.awaiting_user)

        rep.append(("system", f"已恢復 Session {self.store.id}（上次狀態：{st.get('state') or '未知'}）。"))
        if legacy:
            rep.append(("system", "這是舊版（v1）Session：排隊中的暫存訊息當時沒有保存，無法恢復；"
                                  "preamble 狀態由事件紀錄推測。"))
        if self.imp is not None:
            self._restore_import(rep)
        # 1. turns that were running when the old process ended
        lost = [_mail_text(m) for m in self.mgr.inflight if m["kind"] in ("user", "user_answer")]
        if self.mgr.inflight:
            self.pending_notes.append(
                "The agent program was stopped while you were handling the previous input. How far you got is UNKNOWN; "
                "nothing was re-run automatically and files were NOT rolled back. Do not assume it completed; "
                "if it matters, check the actual state or ask the user.")
        if lost:
            rep.append(("system", "上次中斷時 Manager 正在處理你的下列訊息，結果未知，不會自動重做；"
                                  "需要的話請重新送出：\n" + "\n".join("  - " + t[:120] for t in lost)))
        elif self.mgr.inflight:
            rep.append(("system", "上次中斷時 Manager 正在處理一則系統通知，結果未知，不會自動重做。"))
        self.mgr.inflight = []
        broken: List[str] = []
        for wid in self.worker_ids:
            w = self.agents[wid]
            if w.inflight:
                tid = w.active_task or next((m.get("task_id") for m in w.inflight if m.get("task_id")), None)
                if tid and tid in self.tasks and self.tasks[tid].get("status") in protocol.OPEN_TASK_STATES:
                    self.store.set_task(tid, status="interrupted")
                    broken.append(f"{tid}（{wid}）")
                w.inflight = []
                w.active_task = None
        # 2. open tasks nothing in the saved queues can still drive: unfinished with unknown outcome
        backed = {m.get("task_id") for wid in self.worker_ids for m in self.agents[wid].inbox if m.get("task_id")}
        backed |= {m.get("task_id") for m in self.mgr.inbox + self.held if m.get("kind") == "worker"}
        for tid, t in self.tasks.items():
            if t.get("status") in protocol.OPEN_TASK_STATES and tid not in backed:
                self.store.set_task(tid, status="interrupted")
                broken.append(f"{tid}（{t.get('worker')}）")
        for wid in self.worker_ids:
            if self.agents[wid].active_task and self.tasks.get(self.agents[wid].active_task, {}).get("status") == "interrupted":
                self.agents[wid].active_task = None
        if broken:
            rep.append(("system", "這些任務在上次結束時尚未完成，已標記為 interrupted；檔案不會自動還原，也不會自動重跑："
                                  + "、".join(broken)))
            self.pending_notes.append(
                "After a restart of the agent program these tasks were found unfinished and are now marked interrupted: "
                + ", ".join(broken) + ". Workers were stopped; files were NOT rolled back; nothing was re-run. "
                "Do not assume any of it completed.")
        # 3. what waits for the user
        queued = [_mail_text(m) for a in self.agents.values() for m in a.inbox if m["kind"] in ("user", "user_answer")]
        n_mail = sum(len(a.inbox) for a in self.agents.values())
        if n_mail:
            self.hold = True
            rep.append(("system", f"保留了 {n_mail} 項尚未處理的訊息／任務／通知"
                                  + ("（含你的訊息：" + " | ".join(t[:40] for t in queued[:3]) + "）" if queued else "")
                                  + "，目前暫停。輸入 /continue 開始處理，直接輸入新訊息（會排在它們之後），或 /interrupt 丟棄。"))
        if self.awaiting_user:
            rep.append(("ask_user", self.awaiting_user["text"]))
            if self.held:
                rep.append(("system", f"另有 {len(self.held)} 項訊息在這個問題之後等待，會在你回答後依序送給 Manager。"))
        if self.pending_notes and not (n_mail or self.awaiting_user):
            rep.append(("system", "下一則你輸入的訊息會附帶上述中斷說明給 Manager。"))
        self._settle_restore()
        self.store.log("session_restored", legacy=legacy, hold=self.hold, interrupted=broken, lost_user=lost,
                       awaiting=bool(self.awaiting_user))
        self._persist()

    def _restore_import(self, rep: List[Tuple[str, str]]) -> None:
        """The imported Manager works in the fork only. Its session id comes from the import record, never from anything
        else, and the original id is never touched. Nothing is re-run and no id is replaced here."""
        imp = self.imp
        assert imp is not None
        problems = importer.validate_import_state(imp, self.mgr.session_id)
        if problems:
            raise ValueError("; ".join(problems))
        stage, src, fork = imp["stage"], imp["source_id"], imp["fork_id"]
        if stage == "ready":
            self.mgr.session_id = fork
        else:
            self.mgr.session_id, self.mgr.preamble_sent = None, False
        if stage == "prepared":
            rep.append(("system", f"這是匯入的 Claude Session {src}：尚未 Fork（Fork ID {fork} 已保存）。第一則訊息才會建立 Fork。"))
        elif stage == "forking":
            rep.append(("system", f"匯入的 Claude Session {src}：上次 Fork 的結果不明（Fork {fork} 可能已建立）。"
                                  "Fork ID 不會更換、也不會自動重做；你的下一則訊息會用同一個 ID 重試。"
                                  "若 CLI 回報該 ID 已存在，會保留訊息並改以 --resume 該 Fork 繼續（需要 /continue）。"))
        elif stage == "forked":
            rep.append(("system", f"匯入的 Claude Session {src}：Fork {fork} 已存在，但 Manager 尚未成功回覆過。"
                                  "下一次會直接 --resume 這個 Fork（不會再 Fork）。"))
        elif stage == "blocked":
            rep.append(("system", f"匯入的 Claude Session {src} 無法安全繼續（Fork {fork} 在從未嘗試過之前就已存在）。"
                                  "請用 `agent import` 重新匯入。"))

    def _settle_restore(self) -> None:
        """Pick the starting state from what was restored (the normal transition table starts at idle)."""
        busy_worker = bool([t for t in self.tasks.values() if t["status"] in protocol.OPEN_TASK_STATES])
        if self.awaiting_user:
            target = State.WAITING_USER
        elif self.mgr.inbox:
            target = State.WAITING_MANAGER
        elif busy_worker or any(self.agents[w].inbox for w in self.worker_ids):
            target = State.WAITING_WORKER
        else:
            target = State.IDLE
        self.state = target
        self.store.update(state=target.value)

    def release(self) -> None:
        """/continue: let the work that a resume kept on hold start."""
        self._events.put(("release",))

    # ------------------------------------------------------------ public API (thread-safe)
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="engine", daemon=True)
        self._thread.start()

    def submit(self, text: str) -> None:
        self._events.put(("user", text))

    def interrupt(self) -> None:
        self._events.put(("interrupt",))

    def shutdown(self) -> None:
        if self._stopped or (self._thread is not None and not self._thread.is_alive()):
            return                                       # already shut down: calling it again must not wait
        done = threading.Event()
        self._events.put(("stop", done))
        done.wait(30)
        if self._thread:
            self._thread.join(timeout=5)

    def is_quiescent(self) -> bool:
        with self._lock:
            return (self._events.unfinished_tasks == 0 and all(a.running is None for a in self.agents.values())
                    and (self.hold or not any(a.inbox for a in self.agents.values())))

    def wait_idle(self, timeout: float = 60.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.is_quiescent():
                time.sleep(0.05)
                if self.is_quiescent():
                    return True
            time.sleep(0.03)
        return False

    def check_cli_versions(self, timeout: float = 8.0, wait: bool = False) -> Optional[threading.Thread]:
        """Record each agent's CLI version in the background (official `--version`, no model call). Failures only
        produce a log line / soft notice; they never block or alter a chat."""
        def work() -> None:
            for a in list(self.agents.values()):
                try:
                    if not a.adapter.version_args_supported():
                        continue
                    info = compat.check_version(a.cfg.cli, a.adapter.command_prefix(), self.cwd, timeout)
                except Exception as e:
                    info = {"cli": a.cfg.cli, "version": None, "raw": "", "ok": False, "status": "unknown",
                            "note": f"{type(e).__name__}: {e}"}
                with self._lock:
                    self.cli_versions[a.id] = info
                self.store.log("cli_version", agent=a.id, **info)
                self.store.set_agent(a.id, cli_version=info.get("version"), cli_version_status=info["status"])
                note = compat.startup_notice(info)
                if note:
                    self.ui.system(note)
        th = threading.Thread(target=work, name="version-check", daemon=True)
        th.start()
        if wait:
            th.join(timeout * 3 * max(1, len(self.agents)))
        return th

    def _activity(self, a: AgentRT) -> str:
        r = a.running
        if not r:
            return "idle"
        now = self.clock()
        return (f"running {fmt_elapsed(now - r.t0)} (since {time.strftime('%H:%M:%S', time.localtime(r.started))}), "
                f"last output {fmt_elapsed(now - r.last_output)} ago" + ("" if r.lines else " (none yet)"))

    def status_text(self) -> str:
        with self._lock:
            lines = [f"state: {self.state.value}   session: {self.store.id}",
                     f"folder: {self.cwd}",
                     f"manager turns this input: {self.manager_turns}/{self.cfg.max_manager_turns_per_input}"]
            for a in self.agents.values():
                task = f", task={a.active_task}" if a.role == "worker" and a.active_task else ""
                lt = a.last_turn
                last = (f", last turn {lt['turn']}: model {model_label(lt)}" if lt else "")
                cm = (self.store.state.get("agents", {}).get(a.id) or {}).get("cli_meta")
                if isinstance(cm, dict) and cm:
                    last += ", cli " + " ".join(f"{k}={v}" for k, v in cm.items() if k != "capabilities")
                lines.append(f"  {a.id} ({a.role}, {a.cfg.cli}) {self._activity(a)}{task}, queued={len(a.inbox)}, "
                             f"model={a.cfg.model or 'default'}, effort={a.cfg.effort or 'default'}, "
                             f"session={a.session_id or '-'}{last}")
            if self.imp:
                lines.append(f"  imported Claude session: source={self.imp['source_id']} (never resumed), "
                             f"fork={self.imp['fork_id']}, stage={self.imp['stage']}, fork attempts={self.imp.get('attempts', 0)}")
            if self.hold:
                n = sum(len(a.inbox) for a in self.agents.values())
                lines.append(f"  on hold after resume: {n} queued item(s). /continue processes them, "
                             "typing a message queues behind them, /interrupt discards them")
            for aid, v in self.cli_versions.items():
                lines.append(f"  cli {aid}: {v['cli']} {v['version'] or '?'} [{v['status']}] {v['note']}".rstrip())
            open_t = [f"{tid}:{t['status']}@{t['worker']}" for tid, t in self.tasks.items()
                      if t["status"] in protocol.OPEN_TASK_STATES]
            lines.append("  open tasks: " + (", ".join(open_t) or "none"))
            if self.awaiting_user:
                lines.append("  waiting for your answer to: " + self.awaiting_user["text"][:80])
            elif not self.mgr.running and open_t:
                waiting = ", ".join(w for w in self.worker_ids if self.agents[w].running or self.agents[w].inbox)
                lines.append("  Manager is waiting for: " + (waiting or "a worker report"))
            return "\n".join(lines)

    def check_silence(self) -> None:
        """Print at most one 'no new output' notice per agent per interval. It only informs: nothing is interrupted
        and the turn timeout is untouched. Safe to call from any thread; the loop calls it on every idle tick."""
        try:
            with self._lock:
                now = self.clock()
                for a in self.agents.values():
                    r = a.running
                    if not r:
                        continue
                    if r.warn_basis != r.last_output:        # new output since last look -> restart the quiet timer
                        r.warn_basis = r.last_output
                        r.next_warn = r.last_output + self.cfg.silence_warn_sec
                    if now >= r.next_warn:
                        quiet = now - r.last_output
                        r.next_warn = now + self.cfg.silence_repeat_sec
                        self.store.log("silence", agent=a.id, quiet_sec=round(quiet, 1))
                        self.ui.system(f"⚠ {self._label(a, short=True)} 已經 {fmt_quiet(quiet)}沒有新輸出。\n"
                                       "執行流程尚未結束，可使用 /status 或 /interrupt。")
        except Exception:
            self.store.log("internal_error", trace=traceback.format_exc())

    @staticmethod
    def _display(a: AgentRT) -> str:
        return "Manager" if a.role == "manager" else a.id

    @staticmethod
    def _label(a: AgentRT, short: bool = False) -> str:
        name = "Manager" if a.role == "manager" else a.id
        return name if short else f"{name} ({a.cfg.cli})"

    # ------------------------------------------------------------ loop
    def _loop(self) -> None:
        while True:
            try:
                evt = self._events.get(timeout=self._tick)
            except queue.Empty:
                self.check_silence()
                if self.on_tick:
                    try:
                        self.on_tick()
                    except Exception:
                        pass
                continue
            try:
                with self._lock:
                    kind = evt[0]
                    if kind == "stop":
                        self._do_shutdown()
                        evt[1].set()
                        return
                    if kind == "user":
                        self._on_user(evt[1])
                    elif kind == "interrupt":
                        self._on_interrupt()
                    elif kind == "turn_done":
                        self._on_turn_done(evt[1])
                    elif kind == "release":
                        self._on_release()
                    self._pump()
                    self._settle()
                    self._cur_turn = None
                    self._persist()
            except Exception:  # never let the loop die silently
                tb = traceback.format_exc()
                self.store.log("internal_error", trace=tb)
                self.ui.error("internal error (see events.jsonl):\n" + tb.strip().splitlines()[-1])
                try:
                    self._abort_workflow("internal error")
                    self._go(State.FAILED)
                except Exception:
                    pass
            finally:
                self._events.task_done()

    # ------------------------------------------------------------ state machine
    def _go(self, new: State) -> None:
        if new == self.state:
            return
        try:
            check_transition(self.state, new)
        except IllegalTransition as e:
            self.store.log("illegal_transition", detail=str(e))
        self.transitions_log.append(f"{self.state.value}->{new.value}")
        self.store.log("state", old=self.state.value, new=new.value)
        self.state = new
        self.store.update(state=new.value)

    def _settle(self) -> None:
        busy_worker = any(self.agents[w].running for w in self.worker_ids) or bool(
            [t for t in self.tasks.values() if t["status"] in protocol.OPEN_TASK_STATES])
        if self.mgr.running:
            self._go(State.RUNNING)
        elif self.awaiting_user:
            self._go(State.WAITING_USER)
        elif self.mgr.inbox:
            self._go(State.WAITING_MANAGER)
        elif busy_worker:
            self._go(State.WAITING_WORKER)
        elif self.state not in _TERMINAL:
            self._go(State.IDLE)

    # ------------------------------------------------------------ helpers
    def _say(self, sender: str, text: str, to: Optional[str] = None) -> None:
        meta = self._cur_turn
        if meta is not None and sender != self._display(self.agents[meta["agent"]]):
            meta = None                                # e.g. "Me": only the turn's own agent speaks with its model
        if meta:
            self._announce_model(meta)
        self.store.log("chat", sender=sender, to=to, text=text, **({"turn": meta} if meta else {}))
        self.ui.chat(sender, text, to, meta)

    def _announce_model(self, info: Dict[str, Any]) -> None:
        """Say which model produced this turn - once per turn, and only when it differs from what that agent showed
        last time. The label comes from this turn's own facts, never from the current config."""
        if info.get("announced"):
            return
        info["announced"] = True
        a = self.agents[info["agent"]]
        label = model_label(info)
        prev = self._model_seen.get(a.id)
        if label != prev:
            self._model_seen[a.id] = label
            self.store.set_agent(a.id, last_model=label)
            self.ui.system(f"{self._display(a)} · {a.cfg.cli} · " + ("模型變更：" if prev else "") + label)

    def _team_view(self) -> protocol.TeamView:
        return protocol.TeamView(
            manager_id=self.mgr.id, workers=list(self.worker_ids),
            tasks={tid: {"worker": t["worker"], "status": t["status"]} for tid, t in self.tasks.items()})

    def _to_manager(self, mail: Dict[str, Any]) -> None:
        (self.held if self.awaiting_user else self.mgr.inbox).append(mail)

    def _on_session(self, agent_id: str, sid: str) -> None:
        a = self.agents[agent_id]
        a.session_id = sid
        self.store.set_agent(agent_id, session_id=sid)   # saved as soon as the CLI reports it
        self.store.log("session", agent=agent_id, session_id=sid)

    # ------------------------------------------------------------ events
    def _on_user(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.store.log("chat", sender="Me", to="Manager", text=text)
        self.hold = False                              # typing counts as "go ahead" after a resume
        self.manager_turns = 0
        mail: List[Dict[str, Any]] = []
        for n in self.pending_notes:
            mail.append({"kind": "system", "text": n})
        self.pending_notes.clear()
        if self.awaiting_user:
            mail.append({"kind": "user_answer", "text": text})
            mail.extend(self.held)
            self.held.clear()
            self.awaiting_user = None
            self.store.update(awaiting_user=None)
        else:
            if self.mgr.running or self.mgr.inbox:
                self.ui.system("Manager is busy; your message is queued.")
            mail.append({"kind": "user", "text": text})
        self.mgr.inbox.extend(mail)

    def _on_release(self) -> None:
        if not self.hold:
            self.ui.system("沒有被暫停的項目。")
            return
        self.hold = False
        if self.mgr.inbox and self.pending_notes:       # the Manager is about to run: tell it what happened first
            self.mgr.inbox[0:0] = [{"kind": "system", "text": n} for n in self.pending_notes]
            self.pending_notes.clear()
        self.ui.system("開始處理上次保留的項目。")

    def _cancel_all(self) -> None:
        self.epoch += 1
        running = [a for a in self.agents.values() if a.running]
        for a in running:
            a.running.cancel.set()
        for a in running:
            a.running.thread.join(timeout=20)
            a.running = None

    def _clear_work(self, task_status: str) -> List[str]:
        closed = []
        for tid, t in self.tasks.items():
            if t["status"] in protocol.OPEN_TASK_STATES:
                self.store.set_task(tid, status=task_status)
                closed.append(tid)
        self.hold = False
        for a in self.agents.values():
            a.inbox.clear()
            a.inflight = []
            a.repair_used = False
            if a.role == "worker":
                a.active_task = None
        self.held.clear()
        self.awaiting_user = None
        self.store.update(awaiting_user=None)
        return closed

    def _on_interrupt(self) -> None:
        busy = (any(a.running or a.inbox for a in self.agents.values()) or self.awaiting_user
                or any(t["status"] in protocol.OPEN_TASK_STATES for t in self.tasks.values()))
        if not busy:
            self.ui.system("Nothing is running.")
            return
        self._cancel_all()
        dropped = [m["text"] for m in self.mgr.inbox + self.held if m["kind"] in ("user", "user_answer")]
        closed = self._clear_work("interrupted")
        if dropped:
            self.store.log("interrupt_dropped_messages", texts=dropped)
            self.ui.system("Discarded %d queued message(s) of yours (resend if needed): %s"
                           % (len(dropped), " | ".join(t[:60] for t in dropped)))
        self.pending_notes.append(
            "The user INTERRUPTED the previous work with /interrupt. Tasks marked interrupted: "
            f"{closed or 'none'}. Workers were stopped; files were NOT rolled back. Do not assume anything completed.")
        self.store.log("interrupted", tasks=closed)
        self._go(State.INTERRUPTED)
        self.ui.system("Interrupted. Running agents were stopped; files were not reverted. You can type again.")

    def _abort_workflow(self, reason: str) -> None:
        self._cancel_all()
        closed = self._clear_work("cancelled")
        self.pending_notes.append(
            f"The previous workflow was aborted: {reason}. Open tasks cancelled: {closed or 'none'}. "
            "Files were NOT rolled back. Tell the user briefly if relevant, then handle their new input.")

    def _do_shutdown(self) -> None:
        self._cancel_all()
        runner.kill_all()
        self.store.log("shutdown", state=self.state.value)
        self.store.update(state=self.state.value, awaiting_user=self.awaiting_user)
        self._persist()              # queues + the turns that were cut off stay on disk for `agent resume`
        self._stopped = True

    # ------------------------------------------------------------ turn launching
    def _pump(self) -> None:
        if self._stopped or self.hold:
            return
        for a in self.agents.values():
            if a.running is None and a.inbox:
                if a is self.mgr and self.awaiting_user:
                    continue  # manager is paused until the user answers; nothing may start it early
                self._start_turn(a)

    def _team_status_line(self) -> str:
        parts = []
        for wid in self.worker_ids:
            open_t = [f"{tid}({t['status']})" for tid, t in self.tasks.items()
                      if t["worker"] == wid and t["status"] in protocol.OPEN_TASK_STATES]
            parts.append(f"{wid}: " + ("open " + ", ".join(open_t) if open_t else "free"))
        used = f"manager turns used {self.manager_turns}/{self.cfg.max_manager_turns_per_input}"
        return "[system] team status -> " + "; ".join(parts) + f"; {used}"

    def _start_turn(self, a: AgentRT) -> None:
        task: Optional[Dict[str, Any]] = None
        if a is self.mgr and self.imp and self.imp["stage"] == "blocked":
            self.ui.error("匯入無法安全繼續（Fork ID 在從未嘗試前就已存在）。這個 Session 不會建立空白 Manager；請用 `agent import` 重新匯入。")
            self._go(State.FAILED)
            self._abort_workflow("the imported session is blocked")
            return
        mails, a.inbox = a.inbox, []
        route: Tuple[Optional[str], bool, Optional[str]] = (None, False, None)
        if a.role == "manager":
            if self.manager_turns >= self.cfg.max_manager_turns_per_input:
                self.ui.error(f"Manager turn limit ({self.cfg.max_manager_turns_per_input}) reached for this input.")
                self._go(State.FAILED)
                self._abort_workflow("manager turn limit reached")
                return
            self.manager_turns += 1
            body = protocol.fmt_mail(mails) + "\n\n" + self._team_status_line()
            schema = protocol.MANAGER_SCHEMA
            pre = protocol.manager_preamble(a.id, [(w, self.agents[w].cfg.cli) for w in self.worker_ids])
            if self.imp and not a.preamble_sent:
                pre += "\n\n" + protocol.import_notice()
            elif a.reinject:
                pre += "\n\n" + protocol.reinject_note()
            route = self._manager_route(a)
        else:
            for m in mails:
                if m["kind"] in ("task", "answer"):
                    a.active_task = m["task_id"]
            task = self.tasks.get(a.active_task or "")
            if task:
                self.store.set_task(a.active_task, status="running")
            body = protocol.fmt_mail(mails)
            schema = protocol.WORKER_SCHEMA
            pre = protocol.worker_preamble(a.id, self.mgr.id)
            if a.reinject:
                pre += "\n\n" + protocol.reinject_note()
        send_pre = (not a.preamble_sent) or a.reinject
        prompt = (pre + "\n\n---\n" + body) if send_pre else body

        if self.imp and a is self.mgr:
            sid, fork, new_id = route                  # an imported Manager NEVER takes the "start a fresh session" path
        else:
            sid, fork, new_id = (a.session_id if a.preamble_sent else None), False, None
            if a.session_id and not a.preamble_sent:
                self.ui.system(f"{a.id}: earlier first turn never succeeded; starting a fresh CLI session.")
                self.store.log("session_reset", agent=a.id, old=a.session_id)
        if self.imp and sid == self.imp["source_id"] and not fork:
            raise RuntimeError("refusing to resume the ORIGINAL Claude session; only the fork may be resumed")
        if fork:                                       # persisted BEFORE the CLI is started: a crash cannot lose the fork id
            self.imp["stage"] = "forking"
            self.imp["attempts"] = self.imp.get("attempts", 0) + 1
            self.imp["last_attempt"] = time.time()
            self.store.save()
            self.store.log("import_fork_start", source=sid, fork=new_id, attempt=self.imp["attempts"])
        model = (task or {}).get("model") or a.cfg.model
        effort = (task or {}).get("effort") or a.cfg.effort
        req = TurnRequest(agent_id=a.id, prompt=prompt, cwd=self.cwd, session_id=sid, model=model,
                          effort=effort, schema=schema, timeout=self.cfg.turn_timeout_sec, fork=fork,
                          new_session_id=new_id)
        cancel = threading.Event()
        epoch = self.epoch
        th = threading.Thread(target=self._turn_thread, args=(a, req, cancel, epoch), daemon=True,
                              name=f"turn-{a.id}")
        kinds = {m["kind"] for m in mails}
        t0 = self.clock()
        a.turns += 1
        a.inflight = list(mails)
        a.running = Running(th, cancel, epoch, t0=t0, last_output=t0, req_model=model, req_effort=effort,
                            turn_id=f"{a.id}#{a.turns}", sent_pre=send_pre)
        self.store.log("turn_start", agent=a.id, epoch=epoch, session=sid, model=model, effort=effort,
                       turn=f"{a.id}#{a.turns}",
                       prompt_chars=len(prompt), resumed=sid is not None, fork=fork, fork_id=new_id)
        self._announce_start(a, kinds)
        th.start()

    def _manager_route(self, a: AgentRT) -> Tuple[Optional[str], bool, Optional[str]]:
        """(session to resume, fork it?, id the fork must get). The original Claude session is only ever the source of
        the one fork request; every other Manager turn resumes the fork."""
        if self.imp is None:
            return (a.session_id if a.preamble_sent else None), False, None
        if self.imp["stage"] in ("prepared", "forking"):
            return self.imp["source_id"], True, self.imp["fork_id"]
        return self.imp["fork_id"], False, None

    def _announce_start(self, a: AgentRT, kinds: Set[str]) -> None:
        """One short timeline line when a turn starts; a pure format-repair retry stays quiet (the rejection already said so)."""
        if a.role == "worker":
            if "answer" in kinds and "task" not in kinds:
                self.ui.system(f"{self._label(a)} 收到 Manager 的回答，繼續執行。")
            else:
                self.ui.system(f"{self._label(a)} 開始執行（任務 {a.active_task}）。")
        elif kinds <= {"system"} and a.repair_used:
            return
        elif "worker" in kinds:
            self.ui.system("Manager 正在整理結果。")
        elif "user_answer" in kinds:
            self.ui.system("Manager 收到你的回答，繼續執行。")
        else:
            self.ui.system(f"{self._label(a)} 開始執行。")

    def _turn_thread(self, a: AgentRT, req: TurnRequest, cancel: threading.Event, epoch: int) -> None:
        diff: Optional[FileDiff] = None
        run = a.running

        def on_raw(stream: str, line: str) -> None:
            if run is not None:                    # plain attribute writes: no event is queued per CLI line
                run.last_output = self.clock()
                run.lines += 1
            self.store.raw(a.id, stream, line)
        try:
            snap_cfg = self.cfg.snapshot
            use_snap = snap_cfg.get("enabled", True)
            before = None
            if use_snap:
                kw = dict(max_files=snap_cfg.get("max_files", 20000), max_seconds=snap_cfg.get("max_seconds", 5.0))
                before = take_snapshot(self.cwd, **kw)
            res = a.adapter.run(req, cancel, lambda sid: self._on_session(a.id, sid),
                                on_raw)
            if use_snap and before is not None:
                diff = diff_snapshots(before, take_snapshot(self.cwd, **kw))
        except Exception as e:  # adapter bug must not hang the engine
            res = TurnResult(False, error=f"internal adapter error: {type(e).__name__}: {e}", error_kind="internal")
            self.store.log("internal_error", agent=a.id, trace=traceback.format_exc())
        self._events.put(("turn_done", TurnDone(a.id, epoch, res, diff)))

    # ------------------------------------------------------------ turn results
    def _on_turn_done(self, d: TurnDone) -> None:
        if d.epoch != self.epoch:
            return  # belongs to work that was interrupted/aborted
        a = self.agents[d.agent_id]
        run, a.running = a.running, None
        mails, a.inflight = a.inflight, []
        res = d.res
        if res.ok and a is self.mgr and self.imp and res.session_id != self.imp["fork_id"]:
            res = TurnResult(False, session_id=None, error_kind="fork_failed", denials=res.denials,
                             error=f"the Manager answered in session {res.session_id!r}, not in the saved fork "
                                   f"{self.imp['fork_id']}; the result was discarded")
        info = turn_info(a, run.turn_id if run else f"{a.id}#{a.turns}", run.req_model if run else None,
                         run.req_effort if run else None, res)
        a.last_turn = info
        self._cur_turn = info
        self.store.log("turn_end", agent=a.id, ok=res.ok, error_kind=res.error_kind, error=res.error, notes=res.notes,
                       exit_code=res.exit_code, mode=res.mode, killed_with=res.killed_with,
                       denials=res.denials, files=d.diff.to_dict() if d.diff else None, turn=info, cli_meta=res.meta)
        if res.ok:
            a.preamble_sent = True
            if run is not None and run.sent_pre:
                a.reinject = False
            if res.compacted:
                a.reinject = True
                self.store.log("compaction", agent=a.id)
                self.ui.system(f"{self._display(a)} 的對話剛被 Claude 自動壓縮；下一個 turn 會重新提供團隊規則。")
            if a is self.mgr and self.imp:
                if self.imp["stage"] != "ready":
                    self.store.log("import_ready", fork=self.imp["fork_id"])
                self.imp["stage"] = "ready"
                a.session_id = self.imp["fork_id"]
                self.store.set_agent(a.id, session_id=a.session_id)
        self.store.set_agent(a.id, preamble_sent=a.preamble_sent, reinject=a.reinject, turns=a.turns, last_turn=info,
                             **({"cli_meta": res.meta} if res.meta else {}))
        if not res.ok:
            if self._import_fork_exists(a, res, mails):
                return
            self._on_agent_error(a, res)
            return
        obj, perr = protocol.extract_json(res.text)
        if obj is None:
            errors = [perr or "no JSON"]
        elif a.role == "manager":
            errors = protocol.validate_manager_message(obj, self._team_view())
        else:
            errors = protocol.validate_worker_message(obj, a.active_task)
        if errors:
            self._reject(a, res.text, errors)
            return
        a.repair_used = False
        if a.role == "manager":
            if res.denials:
                self.ui.system("Manager's CLI refused tool call(s): " + "; ".join(res.denials[:3])
                               + ". This tool does not change CLI permissions.")
            self._apply_manager(obj)
        else:
            self._apply_worker(a, obj, d)

    def _import_fork_exists(self, a: AgentRT, res: TurnResult, mails: List[Dict[str, Any]]) -> bool:
        """The CLI refused our fork id as "already in use". After an earlier attempt of ours that is expected (the
        crashed/cancelled attempt did create it): keep the id, do not fork again, hold the message for /continue. On a
        first attempt it means the id existed before we ever tried: not ours, never adopted."""
        imp = self.imp
        if a is not self.mgr or imp is None or res.error_kind != "fork_exists" or imp["stage"] != "forking":
            return False
        if imp.get("attempts", 0) >= 2:
            imp["stage"] = "forked"
            self.store.save()
            self.mgr.inbox[0:0] = mails
            self.manager_turns = max(0, self.manager_turns - 1)
            self.hold = True
            self.store.log("import_fork_exists", fork=imp["fork_id"])
            self.ui.system(f"Fork {imp['fork_id']} 已存在（先前的嘗試已建立它）。不會再 Fork，也不會換用其他 ID。"
                           "你的訊息已保留；輸入 /continue 以 --resume 該 Fork 繼續。"
                           "注意：無法確認該副本是否完整；若之後出現 session_lost，請用 `agent import` 重新匯入。")
            return True
        imp["stage"] = "blocked"
        self.store.save()
        return False

    def _reject(self, a: AgentRT, raw: str, errors: List[str]) -> None:
        self.store.log("rejected", agent=a.id, errors=errors, raw=(raw or "")[:1500])
        label = a.id
        if not a.repair_used:
            a.repair_used = True
            self.ui.system(f"{label}: reply rejected ({errors[0]}); asking for one correction.")
            msg = ("Your previous reply was REJECTED by the program:\n- " + "\n- ".join(errors)
                   + "\nReply again with ONLY one corrected JSON object that follows the required format.")
            a.inbox.insert(0, {"kind": "system", "text": msg})
            return
        a.repair_used = False
        reason = "invalid output after one correction attempt: " + "; ".join(errors)
        if a.role == "manager":
            self.ui.error(f"Manager {reason}")
            self._go(State.FAILED)
            self._abort_workflow(reason)
        else:
            self._worker_problem(a, "failed", reason)

    def _worker_problem(self, a: AgentRT, status: str, text: str, evidence: str = "") -> None:
        tid = a.active_task
        if tid and tid in self.tasks:
            self.store.set_task(tid, status=status)
        a.active_task = None
        self.ui.error(f"{a.id} {status}: {text}")
        self._to_manager({"kind": "worker", "sender": a.id, "task_id": tid or "?", "status": status,
                          "text": text, "files_touched": [], "observed": "", "evidence": evidence})
        if not self.mgr.running and not self.awaiting_user:
            self._go({"timeout": State.TIMEOUT, "blocked": State.BLOCKED}.get(status, State.FAILED))

    def _on_agent_error(self, a: AgentRT, res: TurnResult) -> None:
        kind = res.error_kind or "error"
        if kind == "cancelled":
            return
        if kind == "unknown_format":
            v = self.cli_versions.get(a.id) or {}
            res.error = (f"{res.error} [CLI 輸出格式可能已更新；{a.cfg.cli} 版本 {v.get('version') or '未知'}。"
                         "請回報此錯誤與版本，程式不會猜測未知格式。]")
        hint = ERROR_HINTS.get(kind)
        if hint:
            res.error = f"{res.error} [{hint}]"
        if a is self.mgr and self.imp and self.imp["stage"] in ("forking", "forked"):
            res.error += (f" [匯入：Fork {self.imp['fork_id']} 尚未確認成功。Fork ID 已保存、不會更換，"
                          "也不會建立沒有歷史的 Manager；你的下一則訊息會用同一個 ID 重試。]")
        if a.role == "worker":
            ev = ""
            status = "timeout" if kind == "timeout" else "failed"
            if res.denials:
                status = "blocked"
                ev = "SYSTEM EVIDENCE: CLI refused tool call(s): " + "; ".join(res.denials[:5])
            self._worker_problem(a, status, f"[{kind}] {res.error}", ev)
            return
        self.ui.error(f"Manager {kind}: {res.error}")
        target = State.TIMEOUT if kind == "timeout" else (State.BLOCKED if res.denials else State.FAILED)
        self._go(target)
        self._abort_workflow(f"manager {kind}: {res.error}")

    # ------------------------------------------------------------ applying valid messages
    def _apply_manager(self, obj: Dict[str, Any]) -> None:
        actions = obj["actions"]
        final = next((x for x in actions if x["type"] == "final"), None)
        if final and any(m["kind"] == "worker" for m in self.mgr.inbox):
            self.store.log("final_held")
            self.mgr.inbox.append({"kind": "system", "text":
                                   "Your 'final' was held: new worker report(s) arrived while you were answering. "
                                   "Review them, then issue 'final' again if still appropriate."})
            return
        say = obj["say"].strip()
        closing = next((x for x in actions if x["type"] in ("final", "ask_user")), None)
        if say and closing and protocol.say_duplicates(say, closing["text"]):
            # 'say' only repeats the closing text: show it once, using the longer of the two
            if len(say) > len(closing["text"].strip()):
                actions = [dict(x, text=say) if x is closing else x for x in actions]
            self.store.log("say_deduplicated", action=closing["type"])
            say = ""
        if say:
            self._say("Manager", say)
        for x in actions:
            t = x["type"]
            if t == "dispatch":
                w = self.agents[x["to"]]
                task = {"worker": x["to"], "text": x["text"], "status": "pending", "model": x.get("model"),
                        "effort": x.get("effort"), "writes": x.get("writes", []), "created": time.time(),
                        "files_touched": []}
                self.store.set_task(x["task_id"], **task)
                self._say("Manager", x["text"], to=x["to"])
                w.inbox.append({"kind": "task", "task_id": x["task_id"], "text": x["text"],
                                "writes": x.get("writes", [])})
            elif t == "answer":
                self.store.set_task(x["task_id"], status="running")
                self._say("Manager", x["text"], to=x["to"])
                self.agents[x["to"]].inbox.append({"kind": "answer", "task_id": x["task_id"], "text": x["text"]})
            elif t == "ask_user":
                # Everything already queued for the manager (user messages sent while it was busy, worker
                # reports) must wait behind the question, in arrival order. Earlier user messages are NOT
                # the answer; they are delivered after it, marked as queued.
                for m in self.mgr.inbox:
                    if m["kind"] == "user":
                        m["queued"] = True
                self.held = self.mgr.inbox + self.held
                self.mgr.inbox = []
                self.awaiting_user = {"text": x["text"], "ts": time.time()}
                self.store.update(awaiting_user=self.awaiting_user)
                self.store.log("ask_user", text=x["text"])
                self.ui.ask_user(x["text"])
            elif t == "final":
                self._say("Manager", x["text"], to="Me")
                self._go(State.DONE)
                self.manager_turns = 0
                self.store.update(manager_turns=0)

    def _apply_worker(self, a: AgentRT, obj: Dict[str, Any], d: TurnDone) -> None:
        tid, status = obj["task_id"], obj["status"]
        denials = d.res.denials
        evidence = ""
        if denials:
            evidence = ("SYSTEM EVIDENCE: the CLI refused tool call(s): " + "; ".join(denials[:5])
                        + ". Permissions were NOT changed by this tool; the worker's own claim may be wrong.")
            if status == "done":
                status = "blocked"
        observed = ""
        if d.diff and (not d.diff.empty or not d.diff.complete):
            observed = "SYSTEM " + d.diff.summary()
            writes = self.tasks.get(tid, {}).get("writes") or []
            if writes:
                outside = [p for p in d.diff.added + d.diff.modified + d.diff.deleted
                           if not any(p.replace("\\", "/").startswith(w.replace("\\", "/").rstrip("/")) for w in writes)]
                if outside:
                    observed += f"; changes outside declared writes {writes}: {outside[:8]}"
        self.store.set_task(tid, status=status, files_touched=obj["files_touched"],
                            observed=d.diff.to_dict() if d.diff else None)
        if status != "question":
            a.active_task = None
        self._say(a.id, obj["text"], to="Manager")
        self._to_manager({"kind": "worker", "sender": a.id, "task_id": tid, "status": status,
                          "text": obj["text"], "files_touched": obj["files_touched"],
                          "observed": observed, "evidence": evidence})
        if not self.mgr.running and not self.awaiting_user:
            self._go(State.BLOCKED if status == "blocked" else State.WAITING_MANAGER)
