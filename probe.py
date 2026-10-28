#!/usr/bin/env python3
"""Live CLI probe. Spends a few model requests, always in a fresh temp folder.

    python probe.py claude                 # default small set
    python probe.py agy
    python probe.py claude --tests version,schema,resume,badmodel,effort,perm,interrupt
    python probe.py agy --tests version,help,plain,resume,stdin,stdin-conv,stdin-schema,badmodel,interrupt
    python probe.py agy --command "C:\\path\\to\\agy.exe"

Nothing dangerous is run: the only "write" test asks for a file in the temp folder and EXPECTS a
headless permission denial (it never changes any permission setting).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent import protocol, runner  # noqa: E402
from agent.adapters.agy import AgyAdapter  # noqa: E402
from agent.adapters.base import TurnRequest  # noqa: E402
from agent.adapters.claude import ClaudeAdapter  # noqa: E402
from agent.config import AgentCfg  # noqa: E402
from agent.store import redact  # noqa: E402

TINY = {"type": "object", "properties": {"say": {"type": "string"}}, "required": ["say"], "additionalProperties": False}
RESULTS = []


def record(name, status, detail=""):
    RESULTS.append((name, status, detail))
    print(f"[{status:7}] {name}" + (f" - {detail}" if detail else ""), flush=True)


def raw_run(prefix, args, stdin, cwd, timeout=180):
    out, err = [], []
    proc = runner.run_process(prefix + args, stdin, cwd, None, timeout, threading.Event(),
                              lambda s, l: (out if s == "stdout" else err).append(l))
    return proc, out, err


def events(lines):
    evs = []
    for l in lines:
        try:
            evs.append(json.loads(l))
        except json.JSONDecodeError:
            pass
    return evs


def show(lines, n=12):
    for l in lines[:n]:
        print("      | " + redact(l)[:220])
    if len(lines) > n:
        print(f"      | ... ({len(lines) - n} more lines)")


# ------------------------------------------------------------------ claude
def probe_claude(prefix, cwd, tests):
    ad = ClaudeAdapter(AgentCfg("probe", "manager", "claude"))
    shim = runner.is_cmd_shim(prefix)
    print(f"launcher: {prefix} (cmd shim: {shim})")
    sid = str(uuid.uuid4())
    if "version" in tests:
        p, o, e = raw_run(prefix, ["--version"], None, cwd, 60)
        record("claude version", "PASS" if p.exit_code == 0 else "FAIL", " ".join(o)[:80])
    if "schema" in tests:
        r = ad.run(TurnRequest("probe", "Return say=hello", cwd, None, schema=TINY, timeout=180),
                   threading.Event(), lambda s: None)
        ok = r.ok and json.loads(r.text).get("say") is not None
        record("claude stream-json + json-schema", "PASS" if ok else "FAIL", f"{r.mode} {r.error or r.text[:60]}")
        sid = r.session_id or sid
        if "resume" in tests and r.session_id:
            r2 = ad.run(TurnRequest("probe", "Repeat the word you returned earlier in say.", cwd, r.session_id,
                                    schema=TINY, timeout=180), threading.Event(), lambda s: None)
            record("claude session resume", "PASS" if r2.ok and "hello" in r2.text.lower() else "FAIL",
                   r2.error or r2.text[:60])
    if "badmodel" in tests:
        r = ad.run(TurnRequest("probe", "hi", cwd, None, model="no-such-model-xyz", schema=TINY, timeout=120),
                   threading.Event(), lambda s: None)
        record("claude invalid model reported (no fallback)", "PASS" if r.error_kind == "model_invalid" else "FAIL",
               f"{r.error_kind}: {(r.error or '')[:80]}")
    if "effort" in tests:
        r = ad.run(TurnRequest("probe", "hi", cwd, None, effort="not-an-effort", schema=TINY, timeout=120),
                   threading.Event(), lambda s: None)
        record("claude invalid effort reported", "PASS" if r.error_kind == "effort_invalid" else "FAIL",
               f"{r.error_kind}: {(r.error or '')[:80]}")
    if "perm" in tests:
        r = ad.run(TurnRequest("probe", "Use a tool to create a file named probe_out.txt containing hi, then reply say=done.",
                               cwd, None, schema=TINY, timeout=180), threading.Event(), lambda s: None)
        created = (Path(cwd) / "probe_out.txt").exists()
        record("claude permission denial detection", "PASS" if (r.denials or created) else "UNKNOWN",
               f"denials={r.denials} file_created={created} (a created file just means your CLI allows writes)")
    if "interrupt" in tests:
        probe_interrupt(ad, cwd)


# ------------------------------------------------------------------ agy
def probe_agy(prefix, cwd, tests):
    ad = AgyAdapter(AgentCfg("probe", "worker", "agy"))
    print(f"launcher: {prefix} (cmd shim: {runner.is_cmd_shim(prefix)})")
    if "version" in tests:
        p, o, e = raw_run(prefix, ["--version"], None, cwd, 60)
        record("agy version", "PASS" if p.exit_code == 0 else "FAIL", " ".join(o + e)[:80])
    if "help" in tests:
        p, o, e = raw_run(prefix, ["--help"], None, cwd, 60)
        text = "\n".join(o + e)
        for flag in ("--output-format", "--input-format", "--conversation", "--json-schema", "--model", "--effort", "-p"):
            record(f"agy help mentions {flag}", "PASS" if flag in text else "MISSING")
    sid = None
    if "plain" in tests:
        p, o, e = raw_run(prefix, ["-p", "Reply with exactly: PONG", "--output-format", "stream-json"], None, cwd)
        evs = events(o)
        print("   event types:", [str(x.get("type") or x.get("event")) for x in evs][:20])
        show(o, 6); show(e, 4)
        r = ad.run(TurnRequest("probe", "Reply with exactly: PONG", cwd, None, timeout=180), threading.Event(), lambda s: None)
        sid = r.session_id
        record("agy -p stream-json (adapter parse)", "PASS" if r.ok and "PONG" in r.text.upper() else "FAIL",
               f"session_id={sid} err={r.error}")
        record("agy session id captured from events", "PASS" if sid else "FAIL",
               "" if sid else "adapter could not find a conversation id; share the printed events so the parser can be fixed")
    if "resume" in tests and sid:
        r = ad.run(TurnRequest("probe", "What exact word did I ask you to reply with earlier?", cwd, sid, timeout=180),
                   threading.Event(), lambda s: None)
        record("agy --conversation resume", "PASS" if r.ok and "PONG" in r.text.upper() else "FAIL", r.error or r.text[:60])
    msg = json.dumps({"event": "user", "message": {"content": "Reply with exactly: PONG2"}}) + "\n"
    if "stdin" in tests:
        p, o, e = raw_run(prefix, ["--input-format", "stream-json", "--output-format", "stream-json"], msg, cwd)
        show(o, 6); show(e, 4)
        record("agy stdin streaming (no -p)", "PASS" if any("PONG2" in l.upper() for l in o) else "FAIL", f"exit={p.exit_code}")
    if "stdin-conv" in tests and sid:
        m2 = json.dumps({"event": "user", "message": {"content": "What exact word did I ask for first?"}}) + "\n"
        p, o, e = raw_run(prefix, ["--input-format", "stream-json", "--output-format", "stream-json",
                                    "--conversation", sid], m2, cwd)
        show(o, 6); show(e, 4)
        record("agy stdin streaming + --conversation", "PASS" if any("PONG" in l.upper() for l in o) else "FAIL",
               f"exit={p.exit_code}")
    if "stdin-schema" in tests:
        p, o, e = raw_run(prefix, ["--input-format", "stream-json", "--output-format", "stream-json", "--json-schema",
                                    json.dumps(TINY, separators=(",", ":"))],
                          json.dumps({"event": "user", "message": {"content": "Return say=hello"}}) + "\n", cwd)
        show(o, 6); show(e, 4)
        record("agy stdin streaming + --json-schema", "PASS" if p.exit_code == 0 and any("hello" in l for l in o) else "FAIL",
               f"exit={p.exit_code}")
    if "badmodel" in tests:
        r = ad.run(TurnRequest("probe", "hi", cwd, None, model="no-such-model-xyz", timeout=120), threading.Event(), lambda s: None)
        record("agy invalid model reported (no fallback)", "PASS" if not r.ok else "FAIL", f"{r.error_kind}: {(r.error or '')[:80]}")
    if "interrupt" in tests:
        probe_interrupt(ad, cwd)


def probe_interrupt(ad, cwd):
    cancel = threading.Event()
    started = threading.Event()
    box = {}

    def go():
        box["r"] = ad.run(TurnRequest("probe", "Write a 3000 word essay about the history of tea. Take your time.",
                                      cwd, None, timeout=300), cancel, lambda s: started.set(), None)

    t = threading.Thread(target=go)
    t.start()
    time.sleep(4)
    pids_before = {p.pid for p in list(runner._ACTIVE)}
    t0 = time.time()
    cancel.set()
    t.join(30)
    r = box.get("r")
    gone = all(not _alive(pid) for pid in pids_before)
    record(f"{ad.name} interrupt (Ctrl+Break, then taskkill if needed)",
           "PASS" if r and r.error_kind == "cancelled" and gone else "FAIL",
           f"killed_with={getattr(r, 'killed_with', None)} took={time.time() - t0:.1f}s pids_gone={gone}")


def _alive(pid):
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


DEFAULTS = {"claude": "version,schema,resume,badmodel,effort,perm,interrupt",
            "agy": "version,help,plain,resume,stdin,stdin-conv,stdin-schema,badmodel,interrupt"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cli", choices=["claude", "agy"])
    ap.add_argument("--tests", help="comma list; default = everything for that CLI")
    ap.add_argument("--command", help="explicit path to the CLI executable")
    a = ap.parse_args()
    tests = set((a.tests or DEFAULTS[a.cli]).split(","))
    try:
        prefix = runner.resolve_command(a.command or a.cli)
    except runner.CommandNotFound as e:
        print("ERROR:", e)
        return 2
    cwd = tempfile.mkdtemp(prefix=f"probe {a.cli} ")
    print(f"probing {a.cli} in temp folder {cwd}")
    try:
        (probe_claude if a.cli == "claude" else probe_agy)(prefix, cwd, tests)
    finally:
        runner.kill_all()
        shutil.rmtree(cwd, ignore_errors=True)
    out = Path(tempfile.gettempdir()) / f"agent-probe-{a.cli}.json"
    out.write_text(json.dumps([{"test": n, "status": s, "detail": redact(d)} for n, s, d in RESULTS],
                              indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nresults saved: {out}")
    return 0 if all(s in ("PASS", "UNKNOWN") for _, s, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
