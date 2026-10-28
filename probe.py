#!/usr/bin/env python3
"""Live CLI probe. Spends a few model requests, always in a fresh temp folder.

    python probe.py claude                 # default small set
    python probe.py agy
    python probe.py claude --tests version,schema,resume,badmodel,effort,perm,interrupt
    python probe.py agy --tests version,help,plain,resume,stdin,stdin-conv,stdin-schema,badmodel,effort,perm,interrupt
    python probe.py agy --command "C:\\path\\to\\agy.exe"
    python probe.py claude --tests fork-headless      # EXPERIMENT A (~3 small calls): fork a headless `claude -p` session
                                                      # this script creates itself. Proves the CLI mechanics only.
    python probe.py claude --tests fork-interactive --session-id <uuid> [--confirm-fork]
                                                      # EXPERIMENT B: fork a session YOU made in an interactive window
                                                      # (as a copy; the original is only read). See README for steps.
                                                      # Both stop (exit 3) inside a Claude Code shell (CLAUDE_CODE_SESSION_ID).
    python probe.py agy --tests keys       # EXPERIMENT (opt-in, 1 small call): which keys do agy's events carry
                                           # (is there a model field?) - prints key NAMES only

Nothing dangerous is run: the only "write" test asks for a file in the temp folder and EXPECTS a
headless permission denial (it never changes any permission setting).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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


def record_permission(cli, denials, created):
    """A denial must actually be observed to count as verified. A created file with no denial only shows that this
    CLI's own settings allowed the write, so denial detection stays UNVERIFIED (status NOT-OBS, not PASS)."""
    name = f"{cli} permission denial detection"
    if denials:
        record(name, "PASS", f"denial observed: {denials}")
    elif created:
        record(name, "NOT-OBS", "no denial occurred (file was created): detection is NOT verified; to verify, run in a "
                                "setting where this CLI really refuses the write and check denials=[...]")
    else:
        record(name, "UNKNOWN", "no file and no denial text matched; share the stderr/events so the matcher can be checked")


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
        record_permission("claude", r.denials, created)
    if "interrupt" in tests:
        probe_interrupt(ad, cwd)
    if "fork" in tests or "fork-headless" in tests:
        probe_fork_headless(ad, prefix, cwd)
    if "fork-interactive" in tests:
        probe_fork_interactive(ad, prefix, cwd)


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def find_transcript(sid: str):
    """Where Claude Code keeps a session's transcript (~/.claude/projects/<project>/<id>.jsonl). Read-only lookup; the
    format is internal, so only size + hash of the whole file are used."""
    hits = list((claude_config_dir() / "projects").glob(f"*/{sid}.jsonl"))
    return hits[0] if len(hits) == 1 else None


def file_fingerprint(path):
    if path is None or not path.is_file():
        return None
    data = path.read_bytes()
    return (len(data), hashlib.sha256(data).hexdigest())


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
RECENT_SEC = 120                      # a transcript touched this recently may still be written by a live Claude window
OPTS = {"session_id": None, "confirm_fork": False}


def fork_blocker():
    """Why the fork experiments must NOT run in this shell, or None. Inside a Claude Code session the CLI may reuse the
    outer session's id for new/forked sessions, which would make every id check meaningless AND could cross-link
    this experiment with the live session. Stop, do not warn-and-continue."""
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return ("CLAUDE_CODE_SESSION_ID is set: this shell is running inside a Claude Code session. Nothing was run. "
                "Open a plain PowerShell window (not a Claude Code terminal) and run the probe there.")
    return None


def stop(name, why):
    record(name, "STOPPED", why)


def transcript_entrypoint(path):
    """Informational only: the `entrypoint` value Claude Code wrote into the transcript (internal, unstable field)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(200), f):
                try:
                    v = json.loads(line).get("entrypoint")
                except (ValueError, AttributeError):
                    continue
                if isinstance(v, str):
                    return v
    except OSError:
        pass
    return None


def fork_and_check(ad, cwd, a_id, t_path, label, question, code_word=None):
    """Steps shared by both experiments. The ORIGINAL session is only ever passed to `--resume ... --fork-session`
    (the adapter also refuses a fork that stays on the original id); every later call uses the fork's own id."""
    fp_before = file_fingerprint(t_path)
    record(f"{label}: original transcript located (for the unchanged check)", "PASS" if fp_before else "UNKNOWN",
           str(t_path) if t_path else "not found under CLAUDE_CONFIG_DIR/projects; unchanged-check will be skipped")
    pre = protocol.manager_preamble("manager", [("w1", "agy")])
    saved_b = []
    rb = ad.run(TurnRequest("probe", pre + "\n\n---\n[from user]\n" + question + "\n\n[system] team status -> w1: free; "
                            "manager turns used 1/30", cwd, a_id, schema=protocol.MANAGER_SCHEMA, fork=True, timeout=600),
                threading.Event(), saved_b.append)
    record(f"{label}: fork turn succeeded", "PASS" if rb.ok else "FAIL", f"{rb.error_kind}: {rb.error}" if not rb.ok else rb.mode)
    b_id = saved_b[0] if saved_b else None
    record(f"{label}: new session id, original not used as the working session",
           "PASS" if (b_id and b_id != a_id and rb.session_id == b_id) else "FAIL", f"original={a_id} fork={b_id}")
    fp_after = file_fingerprint(t_path)
    if fp_before:
        record(f"{label}: original transcript unchanged", "PASS" if fp_after == fp_before else "FAIL",
               f"before={fp_before[0]}B after={fp_after[0] if fp_after else None}B")
    if rb.ok:
        obj, perr = protocol.extract_json(rb.text)
        errs = protocol.validate_manager_message(obj, protocol.TeamView("manager", ["w1"], {})) if obj else [perr or "no JSON"]
        record(f"{label}: Manager JSON protocol obeyed after fork", "PASS" if not errs else "FAIL", "; ".join(errs)[:160])
        fin = next((x for x in (obj or {}).get("actions", []) if x.get("type") == "final"), None)
        if code_word:
            record(f"{label}: earlier conversation context retained",
                   "PASS" if fin and code_word in fin.get("text", "").upper() else "FAIL", (fin or {}).get("text", rb.text)[:80])
        else:
            record(f"{label}: answer about the earlier conversation (judge it yourself)", "UNKNOWN",
                   redact((fin or {}).get("text", rb.text))[:300])
    if b_id:
        saved_c = []
        rc = ad.run(TurnRequest("probe", "[from user]\nDelegate to worker w1 the task with task_id t1 and text 'say hi'. "
                                "Use a dispatch action only.\n\n[system] team status -> w1: free; manager turns used 2/30",
                                cwd, b_id, schema=protocol.MANAGER_SCHEMA, timeout=600), threading.Event(), saved_c.append)
        obj, perr = protocol.extract_json(rc.text) if rc.ok else (None, rc.error)
        errs = protocol.validate_manager_message(obj, protocol.TeamView("manager", ["w1"], {})) if obj else [perr or "no JSON"]
        has_dispatch = bool(obj) and any(x.get("type") == "dispatch" and x.get("to") == "w1" for x in obj.get("actions", []))
        record(f"{label}: forked Manager can dispatch to the configured worker", "PASS" if (not errs and has_dispatch) else "FAIL",
               "; ".join(errs)[:120] or ("dispatch ok" if has_dispatch else "no dispatch action"))
        record(f"{label}: later resume of the fork keeps its id", "PASS" if (rc.ok and saved_c == [] and rc.session_id == b_id) else "FAIL",
               f"{rc.error_kind}: {rc.error}" if not rc.ok else f"saved={saved_c}")
        fp_end = file_fingerprint(t_path)
        if fp_before:
            record(f"{label}: original transcript still unchanged after all calls", "PASS" if fp_end == fp_before else "FAIL", "")
        print(f"   the fork copy is session {b_id}; it stays in Claude's own storage (your original is not modified)")


def probe_fork_headless(ad, prefix, cwd):
    """EXPERIMENT A (headless): fork a plain `claude -p` conversation that this script itself creates. This proves the
    CLI mechanics (fork flag, new id, JSON protocol after fork). It says NOTHING about sessions created in an
    interactive Claude window: use fork-interactive for that."""
    print("\n-- fork-headless: throwaway `claude -p` session created by this script --")
    why = fork_blocker()
    if why:
        return stop("fork-headless: refused to run", why)
    print(f"   launcher kind: {prefix[0]!r}  cmd-shim: {runner.is_cmd_shim(prefix)}")
    ra = ad.run(TurnRequest("probe", "Remember this code word: PINEAPPLE-42. Reply with one short sentence.", cwd, None,
                            timeout=240), threading.Event(), lambda s: None)
    record("fork-headless: free-form session created", "PASS" if ra.ok and ra.session_id else "FAIL", ra.error or ra.session_id or "")
    if not (ra.ok and ra.session_id):
        return
    fork_and_check(ad, cwd, ra.session_id, find_transcript(ra.session_id), "fork-headless",
                   "What code word did I give you earlier? Report it to me with a final action.", code_word="PINEAPPLE")
    print("   NOTE: a headless session is not an interactive one; this does not validate importing a real interactive session.")


def probe_fork_interactive(ad, prefix, cwd):
    """EXPERIMENT B (interactive): fork a session YOU created in a normal interactive `claude` window. The original is
    only read (size + hash) and only passed to --resume --fork-session. Required: --session-id and --confirm-fork."""
    print("\n-- fork-interactive: a real interactive Claude session of yours, forked as a COPY --")
    why = fork_blocker()
    if why:
        return stop("fork-interactive: refused to run", why)
    sid = OPTS["session_id"]
    if not sid or not UUID_RE.match(sid):
        return stop("fork-interactive: refused to run", "pass --session-id <uuid> (the file name of the session's .jsonl "
                                                         "under %USERPROFILE%\\.claude\\projects\\<project>\\)")
    t_path = find_transcript(sid)
    if t_path is None:
        return stop("fork-interactive: refused to run", f"no single transcript for {sid} under {claude_config_dir() / 'projects'} "
                                                        "(missing, or the id exists in more than one project folder)")
    age = time.time() - t_path.stat().st_mtime
    if age < RECENT_SEC:
        return stop("fork-interactive: refused to run", f"{t_path.name} was written {int(age)}s ago: it may still be open in a "
                                                        "Claude window. Exit that session (/exit), wait two minutes, retry.")
    print(f"   transcript: {t_path}  ({t_path.stat().st_size} bytes, entrypoint field: {transcript_entrypoint(t_path) or 'unknown'})")
    print("   The fork RESUMES the whole conversation as a copy, so the entire history is sent to the model: the quota used "
          "grows with its length. Your original session is not modified.")
    if not OPTS["confirm_fork"]:
        return stop("fork-interactive: waiting for confirmation", "nothing was sent. Re-run with --confirm-fork to proceed")
    print(f"   launcher kind: {prefix[0]!r}  cmd-shim: {runner.is_cmd_shim(prefix)}")
    fork_and_check(ad, cwd, sid, t_path, "fork-interactive",
                   "In one or two sentences, what were we working on in this conversation? Answer with a final action.")


def probe_agy_keys(prefix, cwd):
    p, o, e = raw_run(prefix, ["-p", "Reply with exactly: PONG", "--output-format", "stream-json"], None, cwd, 240)
    names, model_values = [], []
    for l in o:
        try:
            x = json.loads(l)
        except json.JSONDecodeError:
            continue
        if not isinstance(x, dict):
            continue
        row = {"event": x.get("event") or x.get("type"), "keys": sorted(x)}
        for k in ("step_update", "result", "data"):
            if isinstance(x.get(k), dict):
                row[k + ".keys"] = sorted(x[k])
        names.append(row)
        for scope in (x, x.get("step_update"), x.get("result")):
            if isinstance(scope, dict) and isinstance(scope.get("model"), str):
                model_values.append(scope["model"])
    for row in names[:12]:
        print("   ", json.dumps(row, ensure_ascii=False)[:230])
    record("agy event keys captured (names only)", "PASS" if names else "FAIL", f"{len(names)} event(s), exit={p.exit_code}")
    record("agy reports a model in its events", "PASS" if model_values else "NOT-OBS",
           f"model value(s): {sorted(set(model_values))}" if model_values else
           "no `model` key found in init/step_update/result: model for agy stays unknown/requested")


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
    if "effort" in tests:
        r = ad.run(TurnRequest("probe", "hi", cwd, None, effort="not-an-effort", timeout=120), threading.Event(), lambda s: None)
        record("agy invalid effort reported (no downgrade)", "PASS" if r.error_kind == "effort_invalid" else "FAIL",
               f"{r.error_kind}: {(r.error or '')[:100]}")
    if "perm" in tests:
        r = ad.run(TurnRequest("probe", "Use a tool to create a file named probe_out.txt containing hi, then reply done.",
                               cwd, None, timeout=180), threading.Event(), lambda s: None)
        created = (Path(cwd) / "probe_out.txt").exists()
        record_permission("agy", r.denials, created)
    if "interrupt" in tests:
        probe_interrupt(ad, cwd)
    if "keys" in tests:
        probe_agy_keys(prefix, cwd)


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
            "agy": "version,help,plain,resume,stdin,stdin-conv,stdin-schema,badmodel,effort,perm,interrupt"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cli", choices=["claude", "agy"])
    ap.add_argument("--tests", help="comma list; default = everything for that CLI")
    ap.add_argument("--command", help="explicit path to the CLI executable")
    ap.add_argument("--session-id", help="fork-interactive only: UUID of YOUR interactive Claude session to fork as a copy")
    ap.add_argument("--confirm-fork", action="store_true",
                    help="fork-interactive only: acknowledge that the whole conversation is sent to the model")
    a = ap.parse_args()
    OPTS["session_id"], OPTS["confirm_fork"] = a.session_id, a.confirm_fork
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
    if any(s == "STOPPED" for _, s, _ in RESULTS):
        return 3                                   # a safety stop is not a pass: the experiment did not run
    return 0 if all(s in ("PASS", "UNKNOWN", "NOT-OBS") for _, s, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
