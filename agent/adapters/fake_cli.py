#!/usr/bin/env python3
"""Fake agent CLI for tests (no model quota). Standalone: stdlib only.

Mimics `claude -p --output-format stream-json --verbose` closely enough for the adapter.
Env: FAKE_SCENARIO (json: {"<agent id>": [step, ...]}), FAKE_STATE (dir for counters/sessions/calls).
Step keys: reply(obj) | text(str) | sleep(sec) | exit(code) | stderr(str) | raw_stdout([str]) |
           denial([{tool_name,tool_input}]) | expect_prompt_contains([str]) | expect_turn(int) |
           write_file({path,content}) | is_error(str) | forget_sessions_of(agent id) | wait_for_file(path relative to cwd)
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
    o = {"session_id": None, "resume": None, "model": None, "effort": None, "agent": "?", "schema": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--session-id", "--resume", "--model", "--effort", "--fake-agent", "--json-schema", "--output-format"):
            key = {"--session-id": "session_id", "--resume": "resume", "--model": "model", "--effort": "effort",
                   "--fake-agent": "agent", "--json-schema": "schema", "--output-format": "fmt"}[a]
            o[key] = argv[i + 1]
            i += 2
        else:
            i += 1
    return o


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
    if opts["resume"]:
        if not os.path.exists(spath):
            print(f"No conversation found with session ID: {sid}")
            out({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": sid,
                 "errors": [f"No conversation found with session ID: {sid}"]})
            return 1
        sess = json.load(open(spath, encoding="utf-8"))
    else:
        sess = {"turns": 0, "agent": agent}
    sess["turns"] += 1
    json.dump(sess, open(spath, "w", encoding="utf-8"))

    with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"agent": agent, "session": sid, "resume": bool(opts["resume"]), "turn": sess["turns"],
                            "model": opts["model"], "effort": opts["effort"], "cwd": os.getcwd(),
                            "has_schema": opts["schema"] is not None, "prompt": prompt,
                            "argv": sys.argv[1:]}, ensure_ascii=False) + "\n")

    out({"type": "system", "subtype": "init", "session_id": sid, "model": opts["model"] or "fake-default"})
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
        out({"type": "assistant", "session_id": sid, "message": {"content": [{"type": "text", "text": json.dumps(reply, ensure_ascii=False)}]}})
        out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
             "result": json.dumps(reply, ensure_ascii=False), "structured_output": reply, "permission_denials": denial})
    else:
        text = step.get("text", "")
        out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": text,
             "permission_denials": denial})
    return int(step.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
