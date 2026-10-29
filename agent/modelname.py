"""Display names for the chat header. Pure functions, no I/O.

Only a string that CAME from somewhere (the CLI reported it, or the config requested it) is ever formatted, and only
by rewriting its spelling: "claude-sonnet-5-5" -> "Claude Sonnet 5.5". Nothing is inferred: no version is added, an
unrecognised id is shown as it was, and the three evidence levels of orchestrator.turn_info stay apart:
  reported   the CLI said so            -> "Claude Sonnet 5.5 (Mgr)"
  requested  we asked, CLI did not say  -> "Gemini 3 Flash (w1) · 未確認"
  unknown    nothing                    -> "Antigravity (w2) · 模型未知"
A list of models an agent COULD use is not an input here, on purpose."""
from __future__ import annotations

import re
from typing import Any, Dict, NamedTuple, Optional

CLI_NAMES = {"claude": "Claude", "agy": "Antigravity"}
MANAGER_TAG = "Mgr"
EVIDENCE_REQUESTED = "未確認"
EVIDENCE_UNKNOWN = "模型未知"

_DATE = r"(?:-\d{8})?"
# claude-sonnet-5-5, claude-opus-4-1-20250805, claude-sonnet-4-5-20250929[1m]
_CLAUDE_NEW = re.compile(r"^claude-(opus|sonnet|haiku|fable)-(\d{1,2})(?:-(\d{1,2}))?" + _DATE + r"(\[[\w]+\])?$", re.I)
# claude-3-5-sonnet-20241022, claude-3-opus-20240229
_CLAUDE_OLD = re.compile(r"^claude-(\d{1,2})(?:-(\d{1,2}))?-(opus|sonnet|haiku)" + _DATE + r"(\[[\w]+\])?$", re.I)
# gemini-3-flash, gemini-2.5-pro, gemini-3-flash-preview, gemini-2.5-flash-lite
_GEMINI = re.compile(r"^gemini-(\d{1,2}(?:\.\d{1,2})?)-([a-z]+)(?:-(preview|lite|exp|latest))?$", re.I)


def _ver(major: str, minor: Optional[str]) -> str:
    return f"{major}.{minor}" if minor is not None else major


def pretty_model(raw: Any) -> str:
    """Readable spelling of a model id, or the id unchanged when it is not a known pattern. Never raises."""
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    m = _CLAUDE_NEW.match(s)
    if m:
        return f"Claude {m.group(1).capitalize()} {_ver(m.group(2), m.group(3))}"
    m = _CLAUDE_OLD.match(s)
    if m:
        return f"Claude {m.group(3).capitalize()} {_ver(m.group(1), m.group(2))}"
    m = _GEMINI.match(s)
    if m:
        out = f"Gemini {m.group(1)} {m.group(2).capitalize()}"
        return out + (f" {m.group(3).capitalize()}" if m.group(3) else "")
    return s


def cli_name(cli: Any) -> str:
    c = str(cli or "").strip()
    return CLI_NAMES.get(c.lower(), c or "Agent")


def role_tag(sender: str, role: Optional[str] = None) -> str:
    """Short tag for the parenthesis: the manager is "Mgr", a worker keeps its id (w1)."""
    if role == "manager" or sender == "Manager":
        return MANAGER_TAG
    return sender


def shorten(text: str, limit: int) -> str:
    """Cap by characters with "..." (plain dots: the ellipsis character is ambiguous-width in CJK consoles)."""
    if len(text) <= limit:
        return text
    if limit < 4:
        return text[:max(limit, 0)]
    return text[:limit - 3].rstrip() + "..."


class Header(NamedTuple):
    name: str          # "Claude Sonnet 5.5", or the CLI's name when no model is known
    tag: str           # "Mgr" / "w1"
    evidence: str      # "" for reported, else a short note
    extra: str         # "+1" when the CLI reported several models in one turn


def header_parts(sender: str, meta: Optional[Dict[str, Any]], max_name: int = 36) -> Header:
    """Everything the header needs from a speaker name and the turn metadata (turn_info / model_label's dict).
    Bad or missing metadata degrades to "unknown"; it never raises and never guesses."""
    info = meta if isinstance(meta, dict) else {}
    tag = role_tag(sender, "manager" if info.get("agent") == "manager" else None)
    source = info.get("source")
    reported, requested = info.get("reported"), info.get("requested")
    if source == "reported" and isinstance(reported, str) and reported.strip():
        models = info.get("models")
        extra = f"+{len(models) - 1}" if isinstance(models, list) and len(models) > 1 else ""
        return Header(shorten(pretty_model(reported), max_name), tag, "", extra)
    if source == "requested" and isinstance(requested, str) and requested.strip():
        return Header(shorten(pretty_model(requested), max_name), tag, EVIDENCE_REQUESTED, "")
    return Header(shorten(cli_name(info.get("cli")), max_name), tag, EVIDENCE_UNKNOWN, "")
