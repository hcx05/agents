"""What the user typed in an earlier run of a session, for the Up-arrow history of the full-screen chat room.

Read-only and tied to nothing but the event list that `resume` has already loaded: no file is written, nothing is
read from the CLIs' own transcripts or from the raw logs. Only the user's own `chat` events count (`sender == "Me"`).
Manager / Worker / system lines, `ask_user` questions and tool output are never history."""
from __future__ import annotations

from collections import deque
from typing import Any, Iterable, List

HISTORY_LIMIT = 200
# Store.log() redacts secrets before anything reaches events.jsonl. A line that was changed that way is no longer
# what the user typed, and sending it again would send the placeholder: such lines are left out, never "repaired".
REDACTED_MARK = "[REDACTED]"


def user_inputs(events: Iterable[Any], limit: int = HISTORY_LIMIT) -> List[str]:
    """The last `limit` messages the user sent, oldest first. Memory is bounded by `limit`, whatever the session size."""
    keep: "deque[str]" = deque(maxlen=max(0, limit))
    for e in events:
        if not isinstance(e, dict) or e.get("type") != "chat" or e.get("sender") != "Me":
            continue
        text = e.get("text")
        if not isinstance(text, str) or not text.strip() or REDACTED_MARK in text:
            continue
        keep.append(text)
    return list(keep)
