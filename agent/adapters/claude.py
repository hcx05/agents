"""Claude Code adapter: claude -p --output-format stream-json --verbose [--json-schema].

Verified against Claude Code 2.1.312 (Linux): prompt via stdin works; stream-json + json-schema
returns `structured_output` in the final `result` event; --session-id creates, --resume continues;
an invalid --model gives is_error result (api_error_status 404); an invalid --effort is only a
stderr warning (CLI ignores it) so we validate it ourselves; denied tools show up in
result.permission_denials.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable, Dict, List, Optional

from .. import runner
from .base import Adapter, Invocation, Parser, TurnRequest, TurnResult


def _brief_denial(d: Any) -> str:
    if not isinstance(d, dict):
        return str(d)[:120]
    inp = d.get("tool_input")
    detail = ""
    if isinstance(inp, dict):
        for k in ("file_path", "command", "path", "url"):
            if k in inp:
                detail = str(inp[k])[:100]
                break
    return f"{d.get('tool_name', '?')}({detail})"


class ClaudeParser(Parser):
    def __init__(self, req: TurnRequest, on_session: Callable[[str], None], schema_mode: bool):
        self.req = req
        self.on_session = on_session
        self.schema_mode = schema_mode
        self.session_id: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.assistant_text: List[str] = []
        self.denials: List[str] = []
        self.stderr: List[str] = []
        self.bad_lines = 0
        self.tool_denial_texts: List[str] = []

    def feed(self, stream: str, line: str) -> None:
        if stream == "stderr":
            self.stderr.append(line)
            return
        if not line.strip():
            return
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            self.bad_lines += 1
            self.stderr.append(line)  # e.g. 'No conversation found ...' goes to stdout in plain text
            return
        if not isinstance(ev, dict):
            return
        sid = ev.get("session_id")
        if isinstance(sid, str) and sid and sid != self.session_id:
            self.session_id = sid
            self.on_session(sid)
        t = ev.get("type")
        if t == "result":
            self.result = ev
        elif t == "assistant":
            for blk in (ev.get("message") or {}).get("content") or []:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    self.assistant_text.append(blk.get("text", ""))
        elif t == "user":
            for blk in (ev.get("message") or {}).get("content") or []:
                if (isinstance(blk, dict) and blk.get("type") == "tool_result" and blk.get("is_error")
                        and re.search(r"permission|haven't granted|not allowed|denied", str(blk.get("content")), re.I)):
                    self.tool_denial_texts.append(str(blk.get("content"))[:160])

    def finish(self, proc: runner.ProcResult) -> TurnResult:
        err_txt = "\n".join(self.stderr)
        res = self.result
        sid = (res or {}).get("session_id") or self.session_id
        denials = [_brief_denial(d) for d in (res or {}).get("permission_denials") or []]
        if not denials:
            denials = list(self.tool_denial_texts)
        base = dict(session_id=sid, denials=denials)
        if re.search(r"Unknown --effort value", err_txt):
            return TurnResult(False, error="CLI rejected effort: " + _first_line(err_txt, "Unknown --effort"),
                              error_kind="effort_invalid", **base)
        if re.search(r"No conversation found with session ID", err_txt) or any(
                "No conversation found" in str(e) for e in (res or {}).get("errors") or []):
            return TurnResult(False, error="session cannot be resumed: " + _first_line(err_txt, "No conversation found"),
                              error_kind="session_lost", **base)
        if res is None:
            if proc.status in ("timeout", "cancelled"):
                return TurnResult(False, **base)
            msg = f"CLI produced no result event (exit code {proc.exit_code}). stderr: {err_txt[-400:] or '<empty>'}"
            return TurnResult(False, error=msg, error_kind="no_output" if proc.exit_code == 0 else "exit", **base)
        text = res.get("result") if isinstance(res.get("result"), str) else ""
        if res.get("is_error") or res.get("subtype") not in (None, "success"):
            low = (text or "").lower()
            kind = "cli_error"
            if res.get("api_error_status") == 404 or "selected model" in low or "model" in low and "not exist" in low:
                kind = "model_invalid"
            msg = text or "; ".join(map(str, res.get("errors") or [])) or f"subtype={res.get('subtype')}"
            return TurnResult(False, error=msg, error_kind=kind, **base)
        structured = res.get("structured_output")
        if structured is not None:
            return TurnResult(True, text=json.dumps(structured, ensure_ascii=False), **base)
        # No structured_output: do NOT pretend. Return raw text; orchestrator validates it as JSON.
        if not text and self.assistant_text:
            text = "\n".join(self.assistant_text)
        if proc.exit_code not in (0, None) and proc.status == "ok":
            return TurnResult(False, error=f"CLI exit code {proc.exit_code}: {err_txt[-300:]}", error_kind="exit", **base)
        return TurnResult(True, text=text, **base)


def _first_line(blob: str, needle: str) -> str:
    for ln in blob.splitlines():
        if needle in ln:
            return ln.strip()
    return blob.strip()[:200]


class ClaudeAdapter(Adapter):
    name = "claude"
    display_name = "Claude"

    def default_command(self) -> str:
        return "claude"

    def new_session_id(self) -> str:
        return str(uuid.uuid4())

    def build(self, req: TurnRequest, prefix: List[str]) -> Invocation:
        args = ["-p", "--output-format", "stream-json", "--verbose"]
        if req.session_id:
            args += ["--resume", req.session_id]
        else:
            # we choose the id, so it is known (and saved) before the CLI even starts
            req_sid = self.new_session_id()
            args += ["--session-id", req_sid]
        if req.model:
            args += ["--model", req.model]
        if req.effort:
            args += ["--effort", req.effort]
        mode = "stream-json+schema"
        if req.schema is not None:
            if runner.is_cmd_shim(prefix):
                # cmd.exe can't carry JSON quotes safely -> prompt-embedded JSON, validated by us
                mode = "stream-json(prompt-json; .cmd shim)"
            else:
                args += ["--json-schema", json.dumps(req.schema, separators=(",", ":"))]
        else:
            mode = "stream-json"
        args += self.extra_args()
        return Invocation(args, req.prompt, mode)

    def make_parser(self, req: TurnRequest, on_session: Callable[[str], None]) -> Parser:
        return ClaudeParser(req, on_session, req.schema is not None)

    def run(self, req, cancel, on_session, on_raw=None):  # type: ignore[override]
        res = super().run(req, cancel, on_session, on_raw)
        if req.session_id and res.session_id and res.session_id != req.session_id and res.ok:
            return TurnResult(False, error=f"CLI continued a different session ({res.session_id}) than requested ({req.session_id})",
                              error_kind="session_lost", session_id=req.session_id)
        return res
