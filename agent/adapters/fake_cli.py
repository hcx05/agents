#!/usr/bin/env python3
"""Fake agent CLI for tests (no model quota). Standalone: stdlib only.

Mimics `claude -p --output-format stream-json --verbose` closely enough for the adapter.
Env: FAKE_SCENARIO (json: {"<agent id>": [step, ...]}), FAKE_STATE (dir for counters/sessions/calls).
Step keys: reply(obj) | text(str) | sleep(sec) | exit(code) | stderr(str) | raw_stdout([str]) |
           denial([{tool_name,tool_input}]) | expect_prompt_contains([str]) | expect_turn(int) |
           write_file({path,content}) | is_error(str) | forget_sessions_of(agent id) | wait_for_file(path relative to cwd) |
           report_model(str: init.model) | omit_model(bool) | assistant_model(str) | api_retry_error(str) |
           fork_keeps_id(bool: --fork-session "forgets" to create a new session) |
           fork_ignores_session_id(bool: --fork-session ignores the requested --session-id and picks a random id)
Like the real CLI (2.1.312): --session-id with --resume needs --fork-session, and an id that already exists is refused
("Error: Session ID X is already in use.", exit 1, before any scenario step is consumed).
"""
import json
import os
import sys
import time
import uuid

EFFORTS = ("low", "medium", "high", "xhigh", "max")


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def parse(argv):
    o = {"session_id": None, "resume": None, "model": None, "effort": None, "agent": "?", "schema": None, "fork": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--session-id", "--resume", "--model", "--effort", "--fake-agent", "--json-schema", "--output-format"):
            key = {"--session-id": "session_id", "--resume": "resume", "--model": "model", "--effort": "effort",
                   "--fake-agent": "agent", "--json-schema": "schema", "--output-format": "fmt"}[a]
            o[key] = argv[i + 1]
            i += 2
        elif a == "--fork-session":
            o["fork"] = True
            i += 1
        else:
            i += 1
    return o


def peek_step(state, agent):
    """The scenario step the NEXT call of this agent will use (without consuming it); {} when none."""
    try:
        scen = json.load(open(os.environ["FAKE_SCENARIO"], encoding="utf-8"))
        cpath = os.path.join(state, f"counter_{agent}.txt")
        n = int(open(cpath).read()) if os.path.exists(cpath) else 0
        steps = scen.get(agent, [])
        return steps[n] if n < len(steps) else {}
    except (OSError, ValueError, KeyError):
        return {}


def main():
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    opts = parse(sys.argv[1:])
    prompt = sys.stdin.read()
    state = os.environ["FAKE_STATE"]
    os.makedirs(os.path.join(state, "sessions"), exist_ok=True)
    agent = opts["agent"]

    sid = opts["resume"] or opts["session_id"] or str(uuid.uuid4())
    spath = os.path.join(state, "sessions", sid + ".json")
    pstep = peek_step(state, agent)
    reject = None
    if opts["resume"] and opts["session_id"] and not opts["fork"]:
        reject = "Error: --session-id can only be used with --continue or --resume if --fork-session is also specified."
    elif opts["session_id"] and not opts["resume"] and os.path.exists(spath):
        reject = f"Error: Session ID {sid} is already in use."
    elif opts["resume"] and opts["fork"] and opts["session_id"] and not pstep.get("fork_keeps_id") \
            and not pstep.get("fork_ignores_session_id") \
            and os.path.exists(os.path.join(state, "sessions", opts["session_id"] + ".json")):
        reject = f"Error: Session ID {opts['session_id']} is already in use."
    if reject:
        with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"agent": agent, "session": sid, "resume": bool(opts["resume"]), "rejected": reject,
                                "turn": 0, "model": opts["model"], "effort": opts["effort"], "cwd": os.getcwd(),
                                "has_schema": opts["schema"] is not None, "prompt": prompt,
                                "argv": sys.argv[1:]}, ensure_ascii=False) + "\n")
        sys.stderr.write(reject + "\n")
        return 1
    if opts["resume"]:
        if not os.path.exists(spath):
            print(f"No conversation found with session ID: {sid}")
            out({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": sid,
                 "errors": [f"No conversation found with session ID: {sid}"]})
            return 1
        sess = json.load(open(spath, encoding="utf-8"))
        if opts["fork"] and not pstep.get("fork_keeps_id"):
            # official behaviour: the history is COPIED into a new session, the original file stays untouched
            chosen = opts["session_id"] if not pstep.get("fork_ignores_session_id") else None
            sid = chosen or str(uuid.uuid4())
            sess = dict(sess, forked_from=opts["resume"])
            spath = os.path.join(state, "sessions", sid + ".json")
    else:
        sess = {"turns": 0, "agent": agent}
    sess["turns"] += 1
    json.dump(sess, open(spath, "w", encoding="utf-8"))

    with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"agent": agent, "session": sid, "resume": bool(opts["resume"]), "turn": sess["turns"],
                            "model": opts["model"], "effort": opts["effort"], "cwd": os.getcwd(),
                            "has_schema": opts["schema"] is not None, "prompt": prompt,
                            "argv": sys.argv[1:]}, ensure_ascii=False) + "\n")

    init = {"type": "system", "subtype": "init", "session_id": sid}
    if not pstep.get("omit_model"):
        init["model"] = pstep.get("report_model") or opts["model"] or "fake-default"
    out(init)
    if pstep.get("api_retry_error"):
        out({"type": "system", "subtype": "api_retry", "session_id": sid, "attempt": 1, "max_retries": 3,
             "retry_delay_ms": 10, "error_status": 401, "error": pstep["api_retry_error"]})
    if opts["effort"] and opts["effort"] not in EFFORTS:
        sys.stderr.write(f"Warning: Unknown --effort value '{opts['effort']}' — ignoring it. Valid values: {', '.join(EFFORTS)}.\n")
        sys.stderr.flush()
    if opts["model"] and opts["model"].startswith("bad-"):
        out({"type": "result", "subtype": "success", "is_error": True, "session_id": sid, "api_error_status": 404,
             "result": f"There's an issue with the selected model ({opts['model']}). It may not exist or you may not have access to it."})
        return 1

    scen = json.load(open(os.environ["FAKE_SCENARIO"], encoding="utf-8"))
    steps = scen.get(agent, [])
    cpath = os.path.join(state, f"counter_{agent}.txt")
    n = int(open(cpath).read()) if os.path.exists(cpath) else 0
    open(cpath, "w").write(str(n + 1))
    if n >= len(steps):
        sys.stderr.write(f"fake_cli: scenario exhausted for {agent} (call #{n + 1})\n")
        return 7
    step = steps[n]

    if step.get("forget_sessions_of"):
        sdir = os.path.join(state, "sessions")
        for fn in os.listdir(sdir):
            fp = os.path.join(sdir, fn)
            try:
                if json.load(open(fp, encoding="utf-8")).get("agent") == step["forget_sessions_of"]:
                    os.remove(fp)
            except (OSError, ValueError):
                pass
    for k in step.get("raw_stdout", []):
        sys.stdout.write(k + "\n")
        sys.stdout.flush()
    if "write_file" in step:
        wf = step["write_file"]
        with open(wf["path"], "w", encoding="utf-8") as f:
            f.write(wf.get("content", ""))
    for needle in step.get("expect_prompt_contains", []):
        if needle not in prompt:
            sys.stderr.write(f"fake_cli: ASSERT prompt missing {needle!r}\n")
            return 9
    if "expect_turn" in step and step["expect_turn"] != sess["turns"]:
        sys.stderr.write(f"fake_cli: ASSERT turn {sess['turns']} != {step['expect_turn']}\n")
        return 9
    if step.get("wait_for_file"):   # deterministic ordering for tests: block until the test creates this file
        end = time.time() + 60
        while not os.path.exists(step["wait_for_file"]) and time.time() < end:
            time.sleep(0.02)
    for k in step.get("emit_after_wait", []):          # more CLI output after the first gate (activity-time tests)
        sys.stdout.write(k + "\n")
        sys.stdout.flush()
    if step.get("wait_for_file2"):
        end = time.time() + 60
        while not os.path.exists(step["wait_for_file2"]) and time.time() < end:
            time.sleep(0.02)
    if step.get("sleep"):
        end = time.time() + float(step["sleep"])
        while time.time() < end:
            time.sleep(0.05)
    if step.get("stderr"):
        sys.stderr.write(step["stderr"] + "\n")
        sys.stderr.flush()
    if "exit" in step and "reply" not in step and "text" not in step:
        return int(step["exit"])
    denial = step.get("denial", [])
    if step.get("is_error"):
        out({"type": "result", "subtype": "success", "is_error": True, "session_id": sid, "result": step["is_error"],
             "permission_denials": denial})
        return 1
    if "reply" in step:
        reply = step["reply"]
        amsg = {"content": [{"type": "text", "text": json.dumps(reply, ensure_ascii=False)}]}
        if step.get("assistant_model"):
            amsg["model"] = step["assistant_model"]
        out({"type": "assistant", "session_id": sid, "message": amsg})
        out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
             "result": json.dumps(reply, ensure_ascii=False), "structured_output": reply, "permission_denials": denial})
    else:
        text = step.get("text", "")
        out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": text,
             "permission_denials": denial})
    return int(step.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
