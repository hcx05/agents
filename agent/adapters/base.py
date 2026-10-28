"""Adapter interface: build a CLI invocation, parse its event stream, classify failures."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .. import runner
from ..config import AgentCfg


@dataclass
class TurnRequest:
    agent_id: str
    prompt: str
    cwd: str
    session_id: Optional[str]          # None on the very first turn
    model: Optional[str] = None
    effort: Optional[str] = None
    schema: Optional[Dict[str, Any]] = None
    timeout: float = 1800
    fork: bool = False                 # resume session_id as a COPY (--fork-session); only the first turn of an import
    new_session_id: Optional[str] = None   # with fork: the UUID the fork MUST get (--session-id). Strict: any other id = fork_failed


@dataclass
class Invocation:
    args: List[str]
    stdin: Optional[str]
    note: str = ""                     # e.g. which mode was used (for logs)


@dataclass
class TurnResult:
    ok: bool
    text: str = ""                     # final assistant payload (JSON text expected)
    session_id: Optional[str] = None
    error: Optional[str] = None
    # not_found|start_failed|timeout|cancelled|session_lost|model_invalid|effort_invalid|
    # unsupported|cli_error|no_output|exit|unknown_format|session_busy|auth_error|fork_failed|fork_exists
    # (unknown_format: the CLI's output no longer looks like what this adapter understands; never guessed at)
    error_kind: Optional[str] = None
    denials: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    mode: str = ""
    killed_with: Optional[str] = None
    notes: List[str] = field(default_factory=list)   # harmless oddities seen (unfamiliar event types...); logged only
    model_reported: Optional[str] = None             # model the CLI itself said it used in THIS turn (None = not reported)
    models_all: List[str] = field(default_factory=list)   # every distinct model id the CLI reported this turn
    meta: Dict[str, Any] = field(default_factory=dict)    # optional CLI metadata (version, auth source...), never required
    compacted: bool = False                          # the CLI said it compacted the conversation during this turn


class Parser:
    """Incremental parser for one turn. feed() sees every line; finish() builds the result."""

    def feed(self, stream: str, line: str) -> None:
        raise NotImplementedError

    def finish(self, proc: runner.ProcResult) -> TurnResult:
        raise NotImplementedError

    def diagnostics(self) -> List[str]:
        return []

    def reported(self) -> Dict[str, Any]:
        """Facts the CLI itself reported this turn: {"model": str|None, "models": [...], "meta": {...}}."""
        return {}


class Adapter:
    name = "base"
    display_name = "Base"

    def __init__(self, cfg: AgentCfg):
        self.cfg = cfg

    # ---- to implement
    def command_prefix(self) -> List[str]:
        override = self.cfg.command
        return runner.resolve_command(override or self.default_command())

    def default_command(self) -> str:
        raise NotImplementedError

    def build(self, req: TurnRequest, prefix: List[str]) -> Invocation:
        raise NotImplementedError

    def make_parser(self, req: TurnRequest, on_session: Callable[[str], None]) -> Parser:
        raise NotImplementedError

    # ---- shared
    def version_args_supported(self) -> bool:
        """True when `<cmd> --version` is a real, free, official call (not the test fake)."""
        return True

    def check_available(self) -> Optional[str]:
        try:
            self.command_prefix()
        except runner.CommandNotFound as e:
            return str(e)
        return None

    def extra_args(self) -> List[str]:
        extra = self.cfg.options.get("extra_args", [])
        if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
            raise ValueError("options.extra_args must be a list of strings")
        return list(extra)

    def run(self, req: TurnRequest, cancel: threading.Event,
            on_session: Callable[[str], None],
            on_raw: Optional[Callable[[str, str], None]] = None) -> TurnResult:
        try:
            prefix = self.command_prefix()
            inv = self.build(req, prefix)
            if runner.is_cmd_shim(prefix):
                runner.check_shim_args(inv.args)
        except runner.CommandNotFound as e:
            return TurnResult(False, error=str(e), error_kind="not_found")
        except (runner.UnsafeArgument, ValueError) as e:
            return TurnResult(False, error=str(e), error_kind="unsupported")
        parser = self.make_parser(req, on_session)

        def on_line(stream: str, line: str) -> None:
            if on_raw:
                on_raw(stream, line)
            parser.feed(stream, line)

        proc = runner.run_process(prefix + inv.args, inv.stdin, req.cwd, None,
                                  req.timeout, cancel, on_line)
        res = self._classify_process(proc, parser)
        res.mode = inv.note
        res.killed_with = proc.killed_with
        return res

    @staticmethod
    def _classify_process(proc: runner.ProcResult, parser: Parser) -> TurnResult:
        if proc.status == "start_failed":
            return TurnResult(False, error=f"could not start CLI: {proc.start_error}", error_kind="start_failed")
        res = parser.finish(proc)
        res.notes = parser.diagnostics()
        rep = parser.reported()
        res.model_reported = rep.get("model")
        res.models_all = list(rep.get("models") or [])
        res.meta = dict(rep.get("meta") or {})
        res.compacted = bool(getattr(parser, "compacted", False))
        res.exit_code = proc.exit_code
        if proc.status == "timeout":
            res.ok, res.error_kind = False, "timeout"
            res.error = "turn timed out (CLI killed)"
        elif proc.status == "cancelled":
            res.ok, res.error_kind = False, "cancelled"
            res.error = "turn cancelled (CLI killed)"
        return res
