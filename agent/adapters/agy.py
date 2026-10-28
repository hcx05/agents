"""Antigravity (agy) adapter.

NOT VERIFIED on a live agy in this repo's dev environment (agy was not installed there).
Facts taken from the product spec / official docs:
  * normal mode:  agy -p "<prompt>" --output-format stream-json [--conversation <id>]
  * stdin mode:   agy --input-format stream-json --output-format stream-json   (prompt ONLY via stdin as
                  {"event":"user","message":{"content":"..."}}; -p is ignored in this mode, so never mixed)
Official stream-json events: init -> step_update (agent_response.text_delta) -> result{conversation_id,status,
response,structured_output?,error?}; only status SUCCESS is success. --effort <value> is passed through when set.
Unverified (run `python probe.py agy` on Windows to check): whether --conversation / --json-schema work with
stdin streaming, and exactly which stderr/tool_info texts agy uses for soft-denials.
Therefore: structured output is obtained by prompt-embedded JSON validated by this program
(--json-schema only if options.use_json_schema is true), and event parsing is deliberately tolerant.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from .. import runner
from .base import Adapter, Invocation, Parser, TurnRequest, TurnResult

SESSION_KEYS = ("conversation_id", "conversationId", "conversation", "session_id", "sessionId")
OFFICIAL_EVENTS = ("init", "step_update", "result")
# legacy/tolerant shapes (kept for compatibility with non-official event layouts)
FINAL_TYPES = ("result", "final", "done", "complete", "completed", "response")
TEXT_TYPES = ("assistant", "model", "agent", "message", "text", "content", "assistant_message")
# Explicit policy/approval refusals only. Generic OS errors ("permission denied", "operation not permitted")
# are ordinary tool failures, not CLI policy denials, and are deliberately NOT matched.
DENIAL_RE = re.compile(
    r"(soft[- ]?den|auto[- ]?den|denied by (the )?(user|policy|permissions?|approval|settings)"
    r"|(requires?|needs?) (user |explicit )?(approval|permission|confirmation)"
    r"|approval (is )?required|permission required"
    r"|(not|cannot be) (allowed|permitted|approved) in (headless|non-?interactive)"
    r"|headless.{0,40}(den|disallow|block))", re.I)
EFFORT_ERR_RE = re.compile(
    r"effort.{0,80}(invalid|unsupported|not support|unknown|unrecognized|must be|one of|supported values|not available)"
    r"|(invalid|unsupported|unknown|unrecognized).{0,40}effort"
    r"|(does not|doesn't) support.{0,40}effort", re.I)
MODEL_ERR_RE = re.compile(
    r"(invalid|unknown|unsupported|unrecognized).{0,20}model|model.{0,30}(not (found|exist|available|supported)|invalid)", re.I)
SESSION_ERR_RE = re.compile(r"(conversation|session).{0,60}(not found|no such|unknown|does not exist|cannot be resumed)", re.I)
ARGV_PROMPT_LIMIT = 6000  # stay far below Windows' 32k CreateProcess limit


def _text_of(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "".join(_text_of(x) for x in v)
    if isinstance(v, dict):
        for k in ("text", "content", "result", "message", "output"):
            if k in v:
                return _text_of(v[k])
    return ""


def _err_text(v: Any) -> str:
    if v is None or v is False:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("message", "error", "detail", "text"):
            if isinstance(v.get(k), str):
                return v[k]
    return json.dumps(v, ensure_ascii=False)[:400]


def classify_error(text: str) -> str:
    """Map real CLI error text to an error_kind. Effort errors win over model errors
    ('model X does not support effort high' is an effort problem)."""
    if SESSION_ERR_RE.search(text):
        return "session_lost"
    if EFFORT_ERR_RE.search(text):
        return "effort_invalid"
    if MODEL_ERR_RE.search(text):
        return "model_invalid"
    return "cli_error"


class AgyParser(Parser):
    """Parses `agy --output-format stream-json`: init -> step_update* -> result (official).

    Terminal `result` event: {"event":"result","result":{"conversation_id","status","response",
    "structured_output"?,"error"?}}. Only status SUCCESS counts as success. Text from
    step_update(agent_response).text_delta is only a fallback and is never mixed with the terminal response.
    """

    def __init__(self, req: TurnRequest, on_session: Callable[[str], None]):
        self.req = req
        self.on_session = on_session
        self.expected = req.session_id           # id we asked agy to continue (None on first turn)
        self.session_id: Optional[str] = req.session_id
        self.mismatch: Optional[str] = None      # id agy reported that differs from the expected one
        self.official = False                    # saw any official-shaped event
        self.terminal: Optional[Dict[str, Any]] = None
        self.deltas: List[str] = []
        # legacy-shape state
        self.final: Optional[str] = None
        self.parts: List[str] = []
        self.plain: List[str] = []
        self.error_event: Optional[str] = None
        self.stderr: List[str] = []
        self.denials: List[str] = []

    def _see_session(self, sid: Any) -> None:
        if not isinstance(sid, str) or not sid:
            return
        if self.expected:
            if sid != self.expected and self.mismatch is None:
                self.mismatch = sid               # never adopt it, never save it
            return
        if self.session_id is None:               # first id wins; later metadata cannot overwrite it
            self.session_id = sid
            self.on_session(sid)

    def _scan_session(self, scopes) -> None:
        for scope in scopes:
            if isinstance(scope, dict):
                for k in SESSION_KEYS:
                    if isinstance(scope.get(k), str):
                        self._see_session(scope[k])
                        break

    def feed(self, stream: str, line: str) -> None:
        if stream == "stderr":
            self.stderr.append(line)
            if DENIAL_RE.search(line):
                self.denials.append(line.strip()[:200])
            return
        if not line.strip():
            return
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            self.plain.append(line)
            return
        if not isinstance(ev, dict):
            return
        name = ev.get("event")
        if isinstance(name, str) and name.lower() in OFFICIAL_EVENTS:
            self._feed_official(name.lower(), ev)
        else:
            self._feed_legacy(ev)

    def _feed_official(self, name: str, ev: Dict[str, Any]) -> None:
        self.official = True
        res = ev.get("result") if isinstance(ev.get("result"), dict) else None
        self._scan_session((ev, ev.get("data"), res))
        if name == "step_update":
            if ev.get("step_type") == "agent_response":
                d = ev.get("text_delta")
                if isinstance(d, str):
                    self.deltas.append(d)            # raw concatenation: deltas carry their own whitespace
            ti = ev.get("tool_info")
            if isinstance(ti, dict):
                e = _err_text(ti.get("error"))
                if e and DENIAL_RE.search(e):
                    tool = ti.get("tool_name") or ti.get("name") or "tool"
                    self.denials.append(f"{tool}: {e.strip()[:180]}")
        elif name == "result":
            self.terminal = res if res is not None else {"_raw": ev.get("result")}

    def _feed_legacy(self, ev: Dict[str, Any]) -> None:
        self._scan_session((ev, ev.get("data"), ev.get("message"), ev.get("metadata")))
        kind = str(ev.get("type") or ev.get("event") or "").lower()
        if ev.get("is_error") or kind == "error":
            self.error_event = _text_of(ev.get("error") or ev.get("message") or ev) or "error event"
        if kind in FINAL_TYPES:
            t = _text_of(ev.get("result") if "result" in ev else ev)
            if t:
                self.final = t
        elif kind in TEXT_TYPES:
            t = _text_of(ev)
            if t:
                self.parts.append(t)

    # ------------------------------------------------------------ result
    def finish(self, proc: runner.ProcResult) -> TurnResult:
        err = "\n".join(self.stderr)
        base = dict(session_id=self.session_id, denials=list(self.denials))
        if proc.status in ("timeout", "cancelled"):
            return TurnResult(False, **base)
        if self.mismatch:
            return TurnResult(False, session_id=self.expected, denials=list(self.denials), error_kind="session_lost",
                              error=f"agy reported conversation {self.mismatch!r} but {self.expected!r} was requested; "
                                    "refusing to treat it as the same session")
        if self.official:
            return self._finish_official(proc, err, base)
        return self._finish_legacy(proc, err, base)

    def _finish_official(self, proc, err: str, base: Dict[str, Any]) -> TurnResult:
        t = self.terminal
        if t is None:
            if SESSION_ERR_RE.search(err):
                return TurnResult(False, error="session cannot be resumed: " + err.strip()[:200], error_kind="session_lost", **base)
            kind = classify_error(err) if err.strip() else "no_output"
            return TurnResult(False, error=f"agy stream ended without a terminal result event (exit code {proc.exit_code}); "
                                           f"partial output is not accepted. stderr: {err[-300:] or '<empty>'}",
                              error_kind=kind if kind != "cli_error" else "no_output", **base)
        status = t.get("status")
        status_s = status.upper() if isinstance(status, str) else ""
        e = _err_text(t.get("error"))
        if status_s != "SUCCESS":
            detail = e or err.strip()[:300] or "no error text"
            shown = status if isinstance(status, str) and status else "<missing>"
            kind = classify_error(f"{e}\n{err}")
            return TurnResult(False, error=f"agy turn status {shown}: {detail}"[:600], error_kind=kind, **base)
        if proc.exit_code not in (0, None):
            return TurnResult(False, error=f"agy reported SUCCESS but exited with code {proc.exit_code}: {err[-300:]}",
                              error_kind="exit", **base)
        so = t.get("structured_output")
        if isinstance(so, str):
            try:
                so = json.loads(so)
            except json.JSONDecodeError:
                so = None
        if isinstance(so, (dict, list)):
            return TurnResult(True, text=json.dumps(so, ensure_ascii=False), **base)
        resp = t.get("response")
        text = resp if isinstance(resp, str) and resp.strip() else "".join(self.deltas)
        if not text.strip():
            return TurnResult(False, error="agy returned SUCCESS but no response text", error_kind="no_output", **base)
        return TurnResult(True, text=text, **base)

    def _finish_legacy(self, proc, err: str, base: Dict[str, Any]) -> TurnResult:
        if SESSION_ERR_RE.search(err):
            return TurnResult(False, error="session cannot be resumed: " + err.strip()[:200], error_kind="session_lost", **base)
        k = classify_error(err)
        if k in ("effort_invalid", "model_invalid"):
            what = "effort" if k == "effort_invalid" else "model"
            return TurnResult(False, error=f"CLI rejected {what}: " + err.strip()[:200], error_kind=k, **base)
        text = self.final if self.final is not None else ("\n".join(self.parts) if self.parts else "\n".join(self.plain))
        if self.error_event and not text.strip():
            return TurnResult(False, error=self.error_event[:400], error_kind=classify_error(self.error_event), **base)
        if proc.exit_code not in (0, None):
            return TurnResult(False, error=f"CLI exit code {proc.exit_code}: {(err or text)[-300:]}", error_kind="exit", **base)
        if not text.strip():
            return TurnResult(False, error=f"CLI produced no assistant text. stderr: {err[-300:] or '<empty>'}", error_kind="no_output", **base)
        return TurnResult(True, text=text, **base)


class AgyAdapter(Adapter):
    name = "agy"
    display_name = "Antigravity"

    def default_command(self) -> str:
        return "agy"

    def build(self, req: TurnRequest, prefix: List[str]) -> Invocation:
        mode_opt = self.cfg.options.get("stdin_mode", "auto")   # auto | always | never
        if mode_opt not in ("auto", "always", "never"):
            raise ValueError("options.stdin_mode must be auto|always|never")
        shim = runner.is_cmd_shim(prefix)
        use_stdin = mode_opt == "always" or (mode_opt == "auto" and (shim or len(req.prompt) > ARGV_PROMPT_LIMIT))
        common: List[str] = ["--output-format", "stream-json"]
        if req.session_id:
            common += ["--conversation", req.session_id]
        if req.model:
            common += ["--model", req.model]
        if req.effort:                      # no effort -> flag omitted -> CLI default; values are the CLI's to judge
            common += ["--effort", req.effort]
        if self.cfg.options.get("use_json_schema") and req.schema is not None and not shim:
            common += ["--json-schema", json.dumps(req.schema, separators=(",", ":"))]
        extra = self.extra_args()
        if use_stdin:
            msg = json.dumps({"event": "user", "message": {"content": req.prompt}}, ensure_ascii=False)
            # prompt travels ONLY on stdin; no -p (it would be ignored in this mode)
            return Invocation(["--input-format", "stream-json"] + common + extra, msg + "\n", "stdin-stream-json")
        return Invocation(["-p", req.prompt] + common + extra, None, "argv-prompt")

    def make_parser(self, req: TurnRequest, on_session: Callable[[str], None]) -> Parser:
        return AgyParser(req, on_session)
