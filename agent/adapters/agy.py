"""Antigravity (agy) adapter.

NOT VERIFIED on a live agy in this repo's dev environment (agy was not installed there).
Facts taken from the product spec / official docs:
  * normal mode:  agy -p "<prompt>" --output-format stream-json [--conversation <id>]
  * stdin mode:   agy --input-format stream-json --output-format stream-json   (prompt ONLY via stdin as
                  {"event":"user","message":{"content":"..."}}; -p is ignored in this mode, so never mixed)
Unverified (run `python probe.py agy` on Windows to check): the exact event shapes, whether
--conversation / --json-schema work with stdin streaming, and the flag name for model/effort.
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
FINAL_TYPES = ("result", "final", "done", "complete", "completed", "response")
TEXT_TYPES = ("assistant", "model", "agent", "message", "text", "content", "assistant_message")
DENIAL_RE = re.compile(r"(soft[- ]?den|denied by|permission (denied|required)|requires? (approval|permission)|not (permitted|allowed)|approval required)", re.I)
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


class AgyParser(Parser):
    def __init__(self, req: TurnRequest, on_session: Callable[[str], None]):
        self.req = req
        self.on_session = on_session
        self.session_id: Optional[str] = None
        self.final: Optional[str] = None
        self.parts: List[str] = []
        self.plain: List[str] = []
        self.stderr: List[str] = []
        self.denials: List[str] = []
        self.error_event: Optional[str] = None

    def _scan_session(self, obj: Dict[str, Any]) -> None:
        for scope in (obj, obj.get("data"), obj.get("message"), obj.get("metadata")):
            if isinstance(scope, dict):
                for k in SESSION_KEYS:
                    v = scope.get(k)
                    if isinstance(v, str) and v and v != self.session_id:
                        self.session_id = v
                        self.on_session(v)
                        return

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
        self._scan_session(ev)
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

    def finish(self, proc: runner.ProcResult) -> TurnResult:
        err = "\n".join(self.stderr)
        base = dict(session_id=self.session_id, denials=self.denials)
        if proc.status in ("timeout", "cancelled"):
            return TurnResult(False, **base)
        if re.search(r"(conversation|session).*(not found|no such|unknown)", err, re.I):
            return TurnResult(False, error="session cannot be resumed: " + err.strip()[:200], error_kind="session_lost", **base)
        if re.search(r"(invalid|unknown|unsupported).{0,20}model|model.{0,30}(not (found|exist|available)|invalid)", err, re.I):
            return TurnResult(False, error="CLI rejected model: " + err.strip()[:200], error_kind="model_invalid", **base)
        text = self.final if self.final is not None else ("\n".join(self.parts) if self.parts else "\n".join(self.plain))
        if self.error_event and not text.strip():
            return TurnResult(False, error=self.error_event[:400], error_kind="cli_error", **base)
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
        if req.effort is not None:
            raise ValueError("effort is not supported by the agy adapter (flag unverified); set it to null")
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
