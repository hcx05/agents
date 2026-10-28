#!/usr/bin/env python3
"""Fake `agy` emitting the official stream-json shape (init -> step_update{step_update:{...}} -> result).
Behaviour via env FAKE_AGY_MODE; argv logged as JSON lines to FAKE_AGY_LOG."""
import json
import os
import sys

mode = os.environ.get("FAKE_AGY_MODE", "ok")
argv = sys.argv[1:]
stdin_text = None
if "--input-format" in argv:
    stdin_text = sys.stdin.read()
with open(os.environ["FAKE_AGY_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "stdin": stdin_text}, ensure_ascii=False) + "\n")


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


def out(o):
    sys.stdout.write(json.dumps(o, ensure_ascii=False) + "\n")
    sys.stdout.flush()


conv = opt("--conversation") or "conv-new-1"
reply = json.dumps({"status": "done", "task_id": "t1", "text": "ok", "files_touched": []})
if mode == "other_conv":
    conv = "conv-DIFFERENT"
if mode == "effort_err_stderr" and opt("--effort"):
    sys.stderr.write(f"error: invalid effort level '{opt('--effort')}' for this model\n")
    sys.exit(2)
if mode == "model_err_stderr" and opt("--model"):
    sys.stderr.write(f"error: unknown model '{opt('--model')}'\n")
    sys.exit(2)
out({"event": "init", "conversation_id": conv})
if mode == "denial_stderr":
    sys.stderr.write("tool run_command auto-denied in headless mode (soft denial)\n")
for part in (reply[:10], reply[10:]):
    out({"event": "step_update", "step_update": {"conversation_id": conv, "step_type": "agent_response",
                                                 "text_delta": part}})
if mode in ("tool_denial", "tool_denial_done"):      # denial only inside nested tool_info.error; nothing on stderr; result SUCCESS
    out({"event": "step_update", "step_update": {"conversation_id": conv, "step_type": "tool_call",
                                                 "tool_info": {"name": "write_file",
                                                               "error": "Tool call denied by policy: requires approval"}}})
if mode == "tool_error":
    out({"event": "step_update", "step_update": {"conversation_id": conv, "step_type": "tool_call",
                                                 "tool_info": {"name": "read_file", "error": "ENOENT: no such file"}}})
    out({"event": "step_update", "step_update": {"conversation_id": conv, "step_type": "tool_call",
                                                 "tool_info": {"name": "read_file", "error": "EACCES: permission denied"}}})
if mode == "no_result":
    sys.exit(0)
if mode == "no_response":      # SUCCESS without result.response -> parser must use the streamed deltas
    out({"event": "result", "result": {"conversation_id": conv, "status": "SUCCESS"}})
    sys.exit(0)
if mode == "effort_err_result":
    out({"event": "result", "result": {"conversation_id": conv, "status": "ERROR",
                                       "error": "Model does not support effort 'high'"}})
    sys.exit(0)
if mode == "model_err_result":
    out({"event": "result", "result": {"conversation_id": conv, "status": "ERROR", "error": "Unknown model: nope"}})
    sys.exit(0)
if mode == "error_status":
    out({"event": "result", "result": {"conversation_id": conv, "status": "ERROR", "response": reply,
                                       "error": "backend failed"}})
    sys.exit(0)
if mode == "sleep":
    import time
    time.sleep(60)
out({"event": "result", "result": {"conversation_id": conv, "status": "SUCCESS", "response": reply}})
