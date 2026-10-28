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


KNOWN_TYPES = ("system", "stream_event", "assistant", "user", "result", "rate_limit_event", "control_request",
               "control_response", "tool_progress", "keep_alive")
RESULT_KEYS = ("result", "structured_output")
# Documented `system/api_retry.error` categories (code.claude.com headless docs) that mean "this login/account cannot be used".
AUTH_RETRY_ERRORS = ("authentication_failed", "oauth_org_not_allowed", "account_on_hold", "billing_error")
AUTH_TEXT_RE = re.compile(r"not logged in|please run /login|invalid api key|authentication[_ ]failed|oauth", re.I)
# Documented behaviour of `claude -p --resume <id>` on a session that is still running in the background:
# exit status 1, "the session is running in the background" plus the `claude attach <id>` / `claude agents` hint.
# Claude Code refuses to create a session under an id that already exists ("Error: Session ID <id> is already in use.").
# Seen in Claude Code 2.1.312 on Linux; NOT in the official docs, so it is only used as one more way to say "no".
IN_USE_RE = re.compile(r"Session ID \S+ is already in use", re.I)
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
BUSY_RE = re.compile(r"running in the background|claude attach|your prompt was not sent to it", re.I)


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
        self.session_id: Optional[str] = None        # id we adopted (and told the engine about)
        self.expected = req.session_id               # id we asked to resume (None on a first turn)
        self.fork = req.fork
        self.chosen = req.new_session_id if req.fork else None   # strict fork: the ONLY id the new session may have
        self.mismatch: Optional[str] = None          # id the CLI used although we asked for another one
        self.ids_seen: List[str] = []
        self.init_model: Optional[str] = None
        self.assistant_models: List[str] = []
        self.usage_models: List[str] = []
        self.cli_meta: Dict[str, Any] = {}
        self.retry_errors: List[str] = []
        self.result: Optional[Dict[str, Any]] = None
        self.assistant_text: List[str] = []
        self.denials: List[str] = []
        self.stderr: List[str] = []
        self.bad_lines = 0
        self.json_lines = 0
        self.unknown_types: Dict[str, int] = {}      # unfamiliar event types: ignored, but reported
        self.tool_denial_texts: List[str] = []
        self.compacted = False                       # documented `system/compact_boundary` event seen this turn

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
        self.json_lines += 1
        t0 = ev.get("type")
        if not isinstance(t0, str) or t0 not in KNOWN_TYPES:
            k = t0 if isinstance(t0, str) else "<no type>"
            self.unknown_types[k] = self.unknown_types.get(k, 0) + 1
        sid = ev.get("session_id")
        if isinstance(sid, str) and sid:
            self._see_session(sid)
        t = ev.get("type")
        if t == "system":
            self._see_system(ev)
        elif t == "result":
            self.result = ev
            usage = ev.get("modelUsage")
            if isinstance(usage, dict):
                self.usage_models = [k for k in usage if isinstance(k, str) and k and not k.startswith("<")]
        elif t == "assistant":
            m = (ev.get("message") or {}).get("model") if isinstance(ev.get("message"), dict) else None
            if isinstance(m, str) and m and not m.startswith("<") and m not in self.assistant_models:
                self.assistant_models.append(m)
            for blk in (ev.get("message") or {}).get("content") or []:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    self.assistant_text.append(blk.get("text", ""))
                elif isinstance(blk, dict) and blk.get("type") == "tool_use" and self.req.on_activity:
                    try:                                     # name only: tool inputs may hold secrets and are never shown
                        self.req.on_activity(str(blk.get("name") or "?")[:40])
                    except Exception:
                        pass
        elif t == "user":
            for blk in (ev.get("message") or {}).get("content") or []:
                if (isinstance(blk, dict) and blk.get("type") == "tool_result" and blk.get("is_error")
                        and re.search(r"permission|haven't granted|not allowed|denied", str(blk.get("content")), re.I)):
                    self.tool_denial_texts.append(str(blk.get("content"))[:160])

    def _see_session(self, sid: str) -> None:
        """Adopt a session id only when it is the one we asked for (resume), the first one we get (new session),
        or - for an explicit fork - the first id that is NOT the original. A resume must never silently move to
        another session, and a fork must never be accepted while it still points at the original."""
        if sid not in self.ids_seen:
            self.ids_seen.append(sid)
        if self.chosen is not None:
            # Strict fork: we chose the new id before starting the CLI, so the engine already has it and is never
            # told about any id from here. The original id may still appear (hook events); anything else is refused.
            if sid == self.chosen:
                self.session_id = sid
            elif sid != self.expected and self.mismatch is None:
                self.mismatch = sid
            return
        if self.expected is None:
            if sid != self.session_id:
                self.session_id = sid
                self.on_session(sid)
        elif self.fork:
            if sid != self.expected and self.session_id is None:
                self.session_id = sid
                self.on_session(sid)
            elif sid != self.expected and sid != self.session_id and self.mismatch is None:
                self.mismatch = sid                  # a second, different new id: refuse to guess
        elif sid != self.expected and self.mismatch is None:
            self.mismatch = sid                      # never adopted, never saved

    def _see_system(self, ev: Dict[str, Any]) -> None:
        sub = ev.get("subtype")
        if sub == "init":
            m = ev.get("model")
            if isinstance(m, str) and m and not m.startswith("<"):
                self.init_model = m
            # optional, not guaranteed by any contract: kept only when present and well-typed
            if isinstance(ev.get("claude_code_version"), str):
                self.cli_meta["cli_version"] = ev["claude_code_version"]
            if isinstance(ev.get("apiKeySource"), str):
                self.cli_meta["api_key_source"] = ev["apiKeySource"]
            caps = ev.get("capabilities")
            if isinstance(caps, list):
                self.cli_meta["capabilities"] = [c for c in caps if isinstance(c, str)][:20]
        elif sub == "compact_boundary":
            self.compacted = True
        elif sub == "api_retry" and isinstance(ev.get("error"), str):
            self.retry_errors.append(ev["error"])

    def reported(self) -> Dict[str, Any]:
        seen: List[str] = []
        for m in self.assistant_models + ([self.init_model] if self.init_model else []) + self.usage_models:
            if m not in seen:
                seen.append(m)
        model = (self.assistant_models[-1] if self.assistant_models else self.init_model
                 or (self.usage_models[0] if len(self.usage_models) == 1 else None))
        return {"model": model, "models": seen, "meta": dict(self.cli_meta)}

    def finish(self, proc: runner.ProcResult) -> TurnResult:
        err_txt = "\n".join(self.stderr)
        res = self.result
        sid = (res or {}).get("session_id") or self.session_id or (None if self.fork else self.expected)
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
        if self.chosen is not None and IN_USE_RE.search(err_txt):
            return TurnResult(False, session_id=None, denials=denials, error_kind="fork_exists",
                              error=f"the CLI refused to create session {self.chosen}: " + _first_line(err_txt, "already in use"))
        if proc.status in ("timeout", "cancelled"):
            return TurnResult(False, **base)
        if self.chosen is not None and (self.mismatch or self.session_id != self.chosen):
            return TurnResult(False, session_id=None, denials=denials, error_kind="fork_failed",
                              error=f"the fork did not get the requested session id {self.chosen} "
                                    f"(ids seen: {', '.join(self.ids_seen) or 'none'}); the result was discarded")
        if self.mismatch:
            return TurnResult(False, session_id=self.expected, denials=denials, error_kind="session_lost",
                              error=f"CLI used session {self.mismatch!r} although {self.expected!r} was requested; "
                                    "refusing to treat it as the same session")
        if self.fork and self.session_id is None:
            return TurnResult(False, session_id=None, denials=denials, error_kind="fork_failed",
                              error="the CLI did not create a new session for --fork-session (it kept the original id); "
                                    "the result was discarded so the original session is not used")
        if res is None and proc.exit_code not in (0, None) and BUSY_RE.search(err_txt):
            return TurnResult(False, error="that Claude session is still running in the background (or in another "
                                           "process): " + _first_line(err_txt, "background"),
                              error_kind="session_busy", **base)
        if self._auth_problem(res, err_txt):
            return TurnResult(False, error=self._auth_message(res, err_txt), error_kind="auth_error", **base)
        if res is None:
            msg = f"CLI produced no result event (exit code {proc.exit_code}). stderr: {err_txt[-400:] or '<empty>'}"
            if proc.exit_code == 0 and (self.unknown_types or (self.bad_lines and not self.json_lines)):
                what = ("unfamiliar event types: " + ", ".join(sorted(self.unknown_types))) if self.unknown_types \
                    else "stdout was not stream-json"
                return TurnResult(False, error=f"CLI exited normally but sent no 'result' event; {what}. "
                                               "Its output format may have changed. " + msg,
                                  error_kind="unknown_format", **base)
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
        if structured is None and not text and not self.assistant_text and not any(k in res for k in RESULT_KEYS):
            return TurnResult(False, error="'result' event has none of the expected fields "
                                           f"(keys: {', '.join(sorted(map(str, res))[:12])}). Its output format may have changed.",
                              error_kind="unknown_format", **base)
        if structured is not None:
            return TurnResult(True, text=json.dumps(structured, ensure_ascii=False), **base)
        # No structured_output: do NOT pretend. Return raw text; orchestrator validates it as JSON.
        if not text and self.assistant_text:
            text = "\n".join(self.assistant_text)
        if proc.exit_code not in (0, None) and proc.status == "ok":
            return TurnResult(False, error=f"CLI exit code {proc.exit_code}: {err_txt[-300:]}", error_kind="exit", **base)
        return TurnResult(True, text=text, **base)


def _auth_problem(self, res: Optional[Dict[str, Any]], err_txt: str) -> bool:
    if any(e in AUTH_RETRY_ERRORS for e in self.retry_errors):
        return True
    if res is not None and (res.get("is_error") or res.get("subtype") not in (None, "success")):
        text = res.get("result") if isinstance(res.get("result"), str) else ""
        return bool(AUTH_TEXT_RE.search(text or "")) or res.get("api_error_status") in (401, 403)
    return res is None and bool(AUTH_TEXT_RE.search(err_txt))


def _auth_message(self, res: Optional[Dict[str, Any]], err_txt: str) -> str:
    detail = next((e for e in self.retry_errors if e in AUTH_RETRY_ERRORS), None)
    if detail is None and res is not None:
        detail = (res.get("result") if isinstance(res.get("result"), str) else "")[:120] or f"HTTP {res.get('api_error_status')}"
    if detail is None:
        detail = _first_line(err_txt, "login") if err_txt else "login required"
    return (f"Claude CLI 無法使用目前的登入或帳號（{detail}）。本工具不會自動改用 API Key 計費、不會修改 Claude 設定，"
            "也不會加入 --bare。請在原生 Claude 執行 /login 或檢查帳號／組織狀態後重試。")


ClaudeParser._auth_problem = _auth_problem  # type: ignore[attr-defined]
ClaudeParser._auth_message = _auth_message  # type: ignore[attr-defined]


def _diag_unknown(self) -> List[str]:
    out = [f"unfamiliar event type {k!r} x{n} (ignored)" for k, n in sorted(self.unknown_types.items())]
    if self.bad_lines:
        out.append(f"{self.bad_lines} non-JSON stdout line(s)")
    return out


ClaudeParser.diagnostics = _diag_unknown  # type: ignore[attr-defined]


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
        if req.new_session_id is not None:
            if not (req.fork and req.session_id and UUID_RE.match(req.new_session_id)
                    and req.new_session_id.lower() != req.session_id.lower()):
                raise ValueError("new_session_id needs fork=True, a source session_id and a UUID different from it")
        if req.session_id:
            args += ["--resume", req.session_id]
            if req.fork:
                args += ["--fork-session"]        # official: copy the history into a NEW session, original untouched
                if req.new_session_id:
                    args += ["--session-id", req.new_session_id]   # not in the CLI reference table: Windows-verified by probe.py
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
