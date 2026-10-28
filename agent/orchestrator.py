"""Orchestrator: explicit state machine + mailboxes. One thread owns all state (the engine loop);
agent CLI turns run in short-lived worker threads that only post results back to the loop."""
from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from . import protocol, runner
from .adapters import get_adapter
from .adapters.base import Adapter, TurnRequest, TurnResult
from .config import AgentCfg, Config
from .snapshot import FileDiff, diff_snapshots, take_snapshot
from .store import Store
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
    started: float = field(default_factory=time.time)


@dataclass
class AgentRT:
    id: str
    cfg: AgentCfg
    adapter: Adapter
    session_id: Optional[str] = None
    inbox: List[Dict[str, Any]] = field(default_factory=list)
    running: Optional[Running] = None
    preamble_sent: bool = False
    repair_used: bool = False
    active_task: Optional[str] = None
    turns: int = 0

    @property
    def role(self) -> str:
        return self.cfg.role


@dataclass
class TurnDone:
    agent_id: str
    epoch: int
    res: TurnResult
    diff: Optional[FileDiff]


class Engine:
    def __init__(self, cfg: Config, store: Store, ui: UI, cwd: str,
                 adapter_factory: Callable[[AgentCfg], Adapter] = get_adapter):
        self.cfg, self.store, self.ui, self.cwd = cfg, store, ui, cwd
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
        store.update(cwd=cwd, state=self.state.value)
        for a in self.agents.values():
            store.set_agent(a.id, role=a.role, cli=a.cfg.cli, model=a.cfg.model, effort=a.cfg.effort,
                            session_id=None)

    # ------------------------------------------------------------ public API (thread-safe)
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="engine", daemon=True)
        self._thread.start()

    def submit(self, text: str) -> None:
        self._events.put(("user", text))

    def interrupt(self) -> None:
        self._events.put(("interrupt",))

    def shutdown(self) -> None:
        done = threading.Event()
        self._events.put(("stop", done))
        done.wait(30)
        if self._thread:
            self._thread.join(timeout=5)

    def is_quiescent(self) -> bool:
        with self._lock:
            return (self._events.unfinished_tasks == 0 and all(a.running is None for a in self.agents.values())
                    and not any(a.inbox for a in self.agents.values()))

    def wait_idle(self, timeout: float = 60.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.is_quiescent():
                time.sleep(0.05)
                if self.is_quiescent():
                    return True
            time.sleep(0.03)
        return False

    def status_text(self) -> str:
        with self._lock:
            lines = [f"state: {self.state.value}   session: {self.store.id}",
                     f"folder: {self.cwd}",
                     f"manager turns this input: {self.manager_turns}/{self.cfg.max_manager_turns_per_input}"]
            for a in self.agents.values():
                busy = "running" if a.running else "idle"
                lines.append(f"  {a.id} ({a.role}, {a.cfg.cli}) {busy}, queued={len(a.inbox)}, "
                             f"model={a.cfg.model or 'default'}, effort={a.cfg.effort or 'default'}, "
                             f"session={a.session_id or '-'}")
            open_t = [f"{tid}:{t['status']}@{t['worker']}" for tid, t in self.tasks.items()
                      if t["status"] in protocol.OPEN_TASK_STATES]
            lines.append("  open tasks: " + (", ".join(open_t) or "none"))
            if self.awaiting_user:
                lines.append("  waiting for your answer to: " + self.awaiting_user["text"][:80])
            return "\n".join(lines)

    # ------------------------------------------------------------ loop
    def _loop(self) -> None:
        while True:
            evt = self._events.get()
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
                    self._pump()
                    self._settle()
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
        self.store.log("chat", sender=sender, to=to, text=text)
        self.ui.chat(sender, text, to)

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
        for a in self.agents.values():
            a.inbox.clear()
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
        self._stopped = True

    # ------------------------------------------------------------ turn launching
    def _pump(self) -> None:
        if self._stopped:
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
        mails, a.inbox = a.inbox, []
        task: Optional[Dict[str, Any]] = None
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
        prompt = (pre + "\n\n---\n" + body) if not a.preamble_sent else body

        sid = a.session_id if a.preamble_sent else None
        if a.session_id and not a.preamble_sent:
            self.ui.system(f"{a.id}: earlier first turn never succeeded; starting a fresh CLI session.")
            self.store.log("session_reset", agent=a.id, old=a.session_id)
        model = (task or {}).get("model") or a.cfg.model
        effort = (task or {}).get("effort") or a.cfg.effort
        req = TurnRequest(agent_id=a.id, prompt=prompt, cwd=self.cwd, session_id=sid, model=model,
                          effort=effort, schema=schema, timeout=self.cfg.turn_timeout_sec)
        cancel = threading.Event()
        epoch = self.epoch
        th = threading.Thread(target=self._turn_thread, args=(a, req, cancel, epoch), daemon=True,
                              name=f"turn-{a.id}")
        a.running = Running(th, cancel, epoch)
        a.turns += 1
        self.store.log("turn_start", agent=a.id, epoch=epoch, session=sid, model=model, effort=effort,
                       prompt_chars=len(prompt), resumed=sid is not None)
        if a.role == "worker":
            self.ui.system(f"{a.id} is working on task {a.active_task}...")
        th.start()

    def _turn_thread(self, a: AgentRT, req: TurnRequest, cancel: threading.Event, epoch: int) -> None:
        diff: Optional[FileDiff] = None
        try:
            snap_cfg = self.cfg.snapshot
            use_snap = snap_cfg.get("enabled", True)
            before = None
            if use_snap:
                kw = dict(max_files=snap_cfg.get("max_files", 20000), max_seconds=snap_cfg.get("max_seconds", 5.0))
                before = take_snapshot(self.cwd, **kw)
            res = a.adapter.run(req, cancel, lambda sid: self._on_session(a.id, sid),
                                lambda s, l: self.store.raw(a.id, s, l))
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
        a.running = None
        res = d.res
        self.store.log("turn_end", agent=a.id, ok=res.ok, error_kind=res.error_kind, error=res.error,
                       exit_code=res.exit_code, mode=res.mode, killed_with=res.killed_with,
                       denials=res.denials, files=d.diff.to_dict() if d.diff else None)
        if res.ok:
            a.preamble_sent = True
        if not res.ok:
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
