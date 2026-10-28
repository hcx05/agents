"""Message protocol: JSON Schemas, a small schema validator, semantic checks, prompts."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")

ACTION_TYPES = ["dispatch", "answer", "ask_user", "final"]
WORKER_STATUSES = ["done", "question", "blocked", "failed"]

_NULLABLE_STR = {"type": ["string", "null"]}

MANAGER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "say": {"type": "string"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ACTION_TYPES},
                    "to": _NULLABLE_STR,
                    "task_id": _NULLABLE_STR,
                    "text": {"type": "string"},
                    "writes": {"type": "array", "items": {"type": "string"}},
                    "model": _NULLABLE_STR,
                    "effort": _NULLABLE_STR,
                },
                "required": ["type", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["say", "actions"],
    "additionalProperties": False,
}

WORKER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": WORKER_STATUSES},
        "task_id": {"type": "string"},
        "text": {"type": "string"},
        "files_touched": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "task_id", "text", "files_touched"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- validator
_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate_schema(value: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    """Validate against the JSON Schema subset used here. Returns error strings."""
    errs: List[str] = []
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_TYPE_CHECKS[x](value) for x in types):
            return [f"{path}: expected {'|'.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not one of {schema['enum']}")
    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        errs.append(f"{path}: shorter than {schema['minLength']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}: missing required field '{req}'")
        for k, v in value.items():
            if k in props:
                errs.extend(validate_schema(v, props[k], f"{path}.{k}"))
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected field '{k}'")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errs.extend(validate_schema(item, schema["items"], f"{path}[{i}]"))
    return errs


# ---------------------------------------------------------------- JSON extraction
def extract_json(text: str) -> Tuple[Optional[Any], Optional[str]]:
    """Pull one JSON object out of model text (plain, fenced, or embedded)."""
    if text is None or not text.strip():
        return None, "empty output"
    s = text.strip()
    try:
        return json.loads(s), None
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S | re.I)
    if fence:
        try:
            return json.loads(fence.group(1).strip()), None
        except json.JSONDecodeError:
            pass
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", s):
        try:
            obj, _ = dec.raw_decode(s[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj, None
    return None, "output is not valid JSON"


# ---------------------------------------------------------------- semantic checks
OPEN_TASK_STATES = {"pending", "running", "question"}


@dataclass
class TeamView:
    """Read-only view of orchestrator state used for validating agent output."""
    manager_id: str
    workers: List[str]
    tasks: Dict[str, Dict[str, Any]]  # task_id -> {worker, status}
    unseen_worker_mail: bool = False

    def open_tasks(self, worker: Optional[str] = None) -> List[str]:
        return [tid for tid, t in self.tasks.items()
                if t["status"] in OPEN_TASK_STATES and (worker is None or t["worker"] == worker)]


def _opt_str_ok(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() != "")


def validate_manager_message(msg: Any, team: TeamView) -> List[str]:
    """Schema + program-level validation. Empty list means acceptable."""
    errs = validate_schema(msg, MANAGER_SCHEMA)
    if errs:
        return errs
    actions = msg["actions"]
    new_tasks: Dict[str, str] = {}   # task ids dispatched in this message
    answered: set = set()
    types = [a["type"] for a in actions]
    busy_workers = {w for w in team.workers if team.open_tasks(w)}
    for i, a in enumerate(actions):
        where = f"actions[{i}]({a['type']})"
        t = a["type"]
        text = a.get("text", "")
        if not isinstance(text, str) or not text.strip():
            errs.append(f"{where}: 'text' must be non-empty")
        if t in ("dispatch", "answer"):
            to = a.get("to")
            if to == team.manager_id:
                errs.append(f"{where}: manager cannot address itself")
            elif to not in team.workers:
                errs.append(f"{where}: unknown worker {to!r}; valid workers: {team.workers}")
            tid = a.get("task_id")
            if not isinstance(tid, str) or not TASK_ID_RE.match(tid or ""):
                errs.append(f"{where}: 'task_id' must match {TASK_ID_RE.pattern}")
            for k in ("model", "effort"):
                if not _opt_str_ok(a.get(k)):
                    errs.append(f"{where}: '{k}' must be null or non-empty string")
            if errs and errs[-1].startswith(where):
                continue
            if t == "dispatch":
                if tid in team.tasks or tid in new_tasks:
                    errs.append(f"{where}: task_id {tid!r} already used")
                elif to in busy_workers:
                    errs.append(f"{where}: worker {to!r} still has open task(s) {team.open_tasks(to)}; wait for its report")
                elif to in {v for v in new_tasks.values()}:
                    errs.append(f"{where}: only one task per worker per message")
                else:
                    new_tasks[tid] = to
            else:  # answer
                task = team.tasks.get(tid)
                if task is None:
                    errs.append(f"{where}: unknown task_id {tid!r}")
                elif task["worker"] != to:
                    errs.append(f"{where}: task {tid!r} belongs to {task['worker']!r}, not {to!r}")
                elif task["status"] != "question":
                    errs.append(f"{where}: task {tid!r} is {task['status']!r}, not awaiting an answer")
                elif tid in answered:
                    errs.append(f"{where}: task {tid!r} answered twice")
                else:
                    answered.add(tid)
        elif t in ("ask_user", "final"):
            if a.get("to") not in (None, "Me", "me", "user"):
                errs.append(f"{where}: 'to' must be null/'Me'")
    if "ask_user" in types and len(actions) > 1:
        errs.append("ask_user must be the only action in a message (pause work until user answers)")
    if "final" in types:
        if len(actions) > 1:
            errs.append("final must be the only action in a message")
        else:
            open_now = [t for t in team.open_tasks()]
            if open_now:
                errs.append(f"final rejected: tasks still open {open_now}; wait for worker reports first")
    if not actions and not team.open_tasks():
        errs.append("actions is empty but no task is open; use 'final' to answer the user, "
                    "'ask_user' to ask, or 'dispatch' to delegate")
    return errs


def validate_worker_message(msg: Any, active_task: Optional[str]) -> List[str]:
    errs = validate_schema(msg, WORKER_SCHEMA)
    if errs:
        return errs
    if active_task is None:
        return ["no active task for this worker"]
    if msg["task_id"] != active_task:
        errs.append(f"task_id {msg['task_id']!r} does not match the active task {active_task!r}")
    if not msg["text"].strip():
        errs.append("'text' must be non-empty")
    return errs


def say_duplicates(say: str, text: str) -> bool:
    """True when 'say' only repeats (or is contained in / contains) the final/ask_user text, ignoring spacing and case."""
    a, b = " ".join(say.split()).lower(), " ".join(text.split()).lower()
    return bool(a) and bool(b) and (a == b or a in b or b in a)


# ---------------------------------------------------------------- prompts
def manager_preamble(manager_id: str, workers: List[Tuple[str, str]]) -> str:
    roster = "\n".join(f"  - {wid} (runs on {cli})" for wid, cli in workers)
    return f"""You are the MANAGER in a hierarchical agent team. Only you talk to the human user ("Me").
Your workers (they cannot talk to the user, only to you):
{roster}

You may do work yourself, or delegate to a worker. Workers reply only to you.
Every message you send MUST be ONE JSON object and nothing else:
{{"say": "<short status text shown in the chat room>",
  "actions": [ {{"type": "dispatch|answer|ask_user|final", ...}} ]}}

Action types:
  dispatch : {{"type":"dispatch","to":"<worker id>","task_id":"<new unique id, e.g. t1>","text":"<instructions>","writes":["<paths the worker may modify>"],"model":null,"effort":null}}
             model/effort: null = use the worker's default. Only set them if really needed.
  answer   : {{"type":"answer","to":"<worker id>","task_id":"<id of the task whose worker asked>","text":"<answer>"}}
  ask_user : {{"type":"ask_user","text":"<question for the human>"}}   (must be the ONLY action in the message)
  final    : {{"type":"final","text":"<final report for the human>"}}  (must be the ONLY action in the message)

Delegation principles (you decide; delegating is optional):
  - Do simple, short, direct tasks yourself and answer with 'final'; no worker is needed.
  - If the user explicitly asks YOU to do something, do it yourself.
  - Dispatch only when a worker brings real capability, quality or speed. Each dispatch costs CLI start-up latency, model usage and context.
  - Having a worker is not a reason to use it. You may work yourself while a worker handles another part.
  - You stay responsible for verifying worker results and for the final report.

Rules:
  - Input arrives as tagged blocks: [from user], [user answer], [from <worker> | task <id> | status <s>], [system].
  - After a dispatch, return actions=[] (or more dispatch/answer) and wait; the worker's report arrives as your next input.
  - Never send 'final' while a task is open (running or asking a question). Read each report, then decide.
  - Worker status 'question' needs an 'answer' (or an ask_user if only the human knows).
  - Worker status 'blocked' usually means the CLI lacked a permission: tell the human what they must do; never claim success.
  - Before destructive or irreversible operations (deleting important data, overwriting unrecoverable content, deploying, pushing to a remote, payments) use ask_user FIRST and wait for the answer.
  - Never dispatch to yourself ('{manager_id}') or to unknown ids. Do not invent task ids that already exist.
  - File-change notes marked 'observed' are filesystem snapshots, not proof of who changed what.
  - 'say' is for a short progress note. Never repeat the text of 'final' or 'ask_user' in 'say' (it would be shown twice); leave 'say' empty instead.
  - Reply in the language the user uses. Output JSON only: no prose outside the JSON object."""


def worker_preamble(worker_id: str, manager_id: str) -> str:
    return f"""You are worker "{worker_id}" in a hierarchical agent team. You talk ONLY to the manager ("{manager_id}").
You receive tasks as [task <id> from manager] blocks. Do the work with your normal tools and permissions.
Every reply MUST be ONE JSON object and nothing else:
{{"status": "done|question|blocked|failed", "task_id": "<the task id>", "text": "<your message to the manager>", "files_touched": ["<paths you changed>"]}}

  - done     : the task is complete; summarize the result in 'text'.
  - question : you need information from the manager; ask precisely in 'text'. You will get the answer in the next message.
  - blocked  : you cannot proceed (e.g. a tool/permission was refused); say exactly what was refused. Do not pretend success.
  - failed   : the task cannot be done for another reason; explain.
Rules: use the exact task_id you were given; do not address the human; do not work outside the task; list changed files honestly (empty list if none). Reply in the manager's language. Output JSON only."""


def fmt_mail(mails: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for m in mails:
        k = m["kind"]
        if k == "user":
            tag = "[from user | sent earlier while you were busy]" if m.get("queued") else "[from user]"
            parts.append(f"{tag}\n{m['text']}")
        elif k == "user_answer":
            parts.append(f"[user answer to your ask_user]\n{m['text']}")
        elif k == "worker":
            head = f"[from {m['sender']} | task {m['task_id']} | status {m['status']}]"
            body = m["text"]
            extra = ""
            if m.get("files_touched"):
                extra += f"\nfiles_touched (self-reported): {', '.join(m['files_touched'])}"
            if m.get("observed"):
                extra += f"\n{m['observed']}"
            if m.get("evidence"):
                extra += f"\n{m['evidence']}"
            parts.append(f"{head}\n{body}{extra}")
        elif k == "task":
            head = f"[task {m['task_id']} from manager]"
            extra = ""
            if m.get("writes"):
                extra = f"\nallowed to modify: {', '.join(m['writes'])}"
            parts.append(f"{head}\n{m['text']}{extra}")
        elif k == "answer":
            parts.append(f"[manager answer | task {m['task_id']}]\n{m['text']}")
        else:
            parts.append(f"[system]\n{m['text']}")
    return "\n\n".join(parts)
