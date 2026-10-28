import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import protocol, runner  # noqa: E402
from agent.adapters.agy import AgyAdapter  # noqa: E402
from agent.adapters.base import TurnRequest  # noqa: E402
from agent.adapters.claude import ClaudeAdapter  # noqa: E402
from agent.config import AgentCfg, ConfigError, DEFAULT_CONFIG, parse_config  # noqa: E402
from agent.orchestrator import State, TRANSITIONS, IllegalTransition, check_transition  # noqa: E402
from agent.snapshot import diff_snapshots, take_snapshot  # noqa: E402
from agent.store import Store, atomic_write_json, redact  # noqa: E402


class TestSchemaAndSemantics(unittest.TestCase):
    team = protocol.TeamView("manager", ["w1"], {"t1": {"worker": "w1", "status": "question"}})

    def test_schema_rejects_pipe_string_as_type(self):
        msg = {"say": "", "actions": [{"type": "dispatch|answer|ask_user|final", "text": "x"}]}
        errs = protocol.validate_schema(msg, protocol.MANAGER_SCHEMA)
        self.assertTrue(any("not one of" in e for e in errs), errs)

    def test_schema_types_and_extra_fields(self):
        errs = protocol.validate_schema({"say": 1, "actions": "x", "extra": 1}, protocol.MANAGER_SCHEMA)
        self.assertEqual(len(errs), 3, errs)
        errs = protocol.validate_schema({"status": "weird", "task_id": "t", "text": "", "files_touched": [1]},
                                        protocol.WORKER_SCHEMA)
        self.assertTrue(any("status" in e for e in errs) and any("files_touched[0]" in e for e in errs), errs)

    def test_manager_semantic_rules(self):
        ok = {"say": "", "actions": [{"type": "answer", "to": "w1", "task_id": "t1", "text": "a"}]}
        self.assertEqual(protocol.validate_manager_message(ok, self.team), [])
        bad_self = {"say": "", "actions": [{"type": "dispatch", "to": "manager", "task_id": "t2", "text": "a"}]}
        self.assertTrue(protocol.validate_manager_message(bad_self, self.team))
        bad_final = {"say": "", "actions": [{"type": "final", "text": "x"}]}
        self.assertTrue(any("still open" in e for e in protocol.validate_manager_message(bad_final, self.team)))
        ask_mix = {"say": "", "actions": [{"type": "ask_user", "text": "q"},
                                           {"type": "answer", "to": "w1", "task_id": "t1", "text": "a"}]}
        self.assertTrue(any("only action" in e for e in protocol.validate_manager_message(ask_mix, self.team)))
        empty_idle = protocol.TeamView("manager", ["w1"], {})
        self.assertTrue(protocol.validate_manager_message({"say": "hi", "actions": []}, empty_idle))
        self.assertEqual(protocol.validate_manager_message({"say": "wait", "actions": []}, self.team), [])
        dup = {"say": "", "actions": [{"type": "dispatch", "to": "w1", "task_id": "t1", "text": "a"}]}
        self.assertTrue(any("already used" in e or "open task" in e for e in protocol.validate_manager_message(dup, self.team)))
        badid = {"say": "", "actions": [{"type": "dispatch", "to": "w1", "task_id": "../x y", "text": "a"}]}
        self.assertTrue(protocol.validate_manager_message(badid, protocol.TeamView("manager", ["w1"], {})))

    def test_extract_json_variants(self):
        self.assertEqual(protocol.extract_json('{"a":1}')[0], {"a": 1})
        self.assertEqual(protocol.extract_json('結果：\n```json\n{"a": 2}\n```')[0], {"a": 2})
        self.assertEqual(protocol.extract_json('好的 {"a": {"b": 3}} 以上')[0], {"a": {"b": 3}})
        self.assertIsNone(protocol.extract_json("no json")[0])
        self.assertIsNone(protocol.extract_json("")[0])

    def test_schemas_are_valid_json_serialisable_and_real(self):
        s = json.loads(json.dumps(protocol.MANAGER_SCHEMA))
        enum = s["properties"]["actions"]["items"]["properties"]["type"]["enum"]
        self.assertEqual(enum, ["dispatch", "answer", "ask_user", "final"])


class TestStateMachine(unittest.TestCase):
    def test_every_state_has_transitions_and_checks_work(self):
        self.assertEqual(set(TRANSITIONS), set(State))
        check_transition(State.IDLE, State.RUNNING)
        check_transition(State.RUNNING, State.WAITING_WORKER)
        check_transition(State.INTERRUPTED, State.RUNNING)
        with self.assertRaises(IllegalTransition):
            check_transition(State.IDLE, State.DONE)
        with self.assertRaises(IllegalTransition):
            check_transition(State.WAITING_WORKER, State.DONE)
        with self.assertRaises(IllegalTransition):
            check_transition(State.DONE, State.TIMEOUT)


class TestRunner(unittest.TestCase):
    def _run(self, code, stdin=None, timeout=20, cancel=None):
        lines = []
        res = runner.run_process([sys.executable, "-c", code], stdin, None, None, timeout,
                                 cancel or threading.Event(), lambda s, l: lines.append((s, l)))
        return res, lines

    def test_no_deadlock_with_huge_stdout_and_stderr(self):
        code = ("import sys\nfor i in range(60000):\n sys.stdout.write('o'*100+'\\n'); sys.stderr.write('e'*100+'\\n')\n")
        t0 = time.time()
        res, lines = self._run(code)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(sum(1 for s, _ in lines if s == "stdout"), 60000)
        self.assertEqual(sum(1 for s, _ in lines if s == "stderr"), 60000)
        self.assertLess(time.time() - t0, 30)

    def test_large_stdin_and_utf8_roundtrip(self):
        big = ("中文行\n" * 200000)
        code = "import sys; d=sys.stdin.read(); print(len(d)); print(d[:3])"
        res, lines = self._run(code, stdin=big)
        self.assertEqual([l for s, l in lines if s == "stdout"], [str(len(big)), "中文行"[:3]])

    def test_timeout_kills_child(self):
        t0 = time.time()
        res, _ = self._run("import time; time.sleep(60)", timeout=1.0)
        self.assertEqual(res.status, "timeout")
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(len(runner._ACTIVE), 0)

    def test_cancel_kills_child_tree(self):
        cancel = threading.Event()
        code = ("import subprocess,sys,time\n"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                "print(p.pid, flush=True)\ntime.sleep(60)\n")
        pids = []
        def on_line(s, l):
            if s == "stdout":
                pids.append(int(l)); cancel.set()
        res = runner.run_process([sys.executable, "-c", code], None, None, None, 30, cancel, on_line)
        self.assertEqual(res.status, "cancelled")
        self.assertIn(res.killed_with, ("sigint", "sigkill", "ctrl_break", "taskkill"))

    def test_grandchild_holding_pipe_does_not_hang(self):
        code = ("import subprocess,sys\n"
                "subprocess.Popen([sys.executable,'-c','import time;time.sleep(8)'])\nprint('parent done', flush=True)\n")
        t0 = time.time()
        res, lines = self._run(code, timeout=30)
        self.assertLess(time.time() - t0, 7)
        self.assertIn(("stdout", "parent done"), lines)

    def test_nonexistent_executable(self):
        res = runner.run_process(["/definitely/not/here"], None, None, None, 5, threading.Event(), lambda s, l: None)
        self.assertEqual(res.status, "start_failed")
        with self.assertRaises(runner.CommandNotFound):
            runner.resolve_command("definitely-not-a-real-cli-xyz")

    def test_shim_argument_safety(self):
        runner.check_shim_args(["-p", "--output-format", "stream-json"])
        for bad in ['{"a":1}', "a&b", "100%", "x|y", "a\nb"]:
            with self.assertRaises(runner.UnsafeArgument):
                runner.check_shim_args([bad])

    def test_path_with_spaces_and_cjk(self):
        with tempfile.TemporaryDirectory(prefix="目錄 with space ") as d:
            script = Path(d) / "my script 腳本.py"
            script.write_text("import sys,os; print(os.getcwd()); print(sys.stdin.read())", encoding="utf-8")
            prefix = runner.resolve_command(str(script))
            lines = []
            runner.run_process(prefix, "多行\n輸入", d, None, 10, threading.Event(), lambda s, l: lines.append(l))
            self.assertEqual(lines[0], os.path.realpath(d) if os.path.realpath(d) == lines[0] else d)
            self.assertEqual(lines[1:], ["多行", "輸入"])


class TestAdaptersBuild(unittest.TestCase):
    def req(self, **kw):
        base = dict(agent_id="a", prompt="hello\n中文", cwd=".", session_id=None, schema=protocol.WORKER_SCHEMA)
        base.update(kw)
        return TurnRequest(**base)

    def test_claude_args_first_and_resume(self):
        ad = ClaudeAdapter(AgentCfg("a", "manager", "claude"))
        inv = ad.build(self.req(), ["claude"])
        self.assertIn("--session-id", inv.args)
        self.assertNotIn("--resume", inv.args)
        self.assertEqual(inv.stdin, "hello\n中文")                 # prompt never on argv
        self.assertNotIn("hello\n中文", inv.args)
        self.assertEqual(json.loads(inv.args[inv.args.index("--json-schema") + 1]), protocol.WORKER_SCHEMA)
        inv = ad.build(self.req(session_id="abc", model="m", effort="high"), ["claude"])
        self.assertEqual(inv.args[inv.args.index("--resume") + 1], "abc")
        self.assertEqual(inv.args[inv.args.index("--model") + 1], "m")
        self.assertEqual(inv.args[inv.args.index("--effort") + 1], "high")

    def test_claude_never_adds_permission_flags(self):
        ad = ClaudeAdapter(AgentCfg("a", "manager", "claude"))
        joined = " ".join(ad.build(self.req(model="m", effort="low"), ["claude"]).args)
        for flag in ("--dangerously-skip-permissions", "--permission-mode", "--allowedTools", "--sandbox", "--bare"):
            self.assertNotIn(flag, joined)

    def test_claude_cmd_shim_drops_schema_arg(self):
        ad = ClaudeAdapter(AgentCfg("a", "manager", "claude"))
        orig = runner.IS_WINDOWS
        runner.IS_WINDOWS = True
        try:
            inv = ad.build(self.req(), ["C:\\x\\claude.cmd"])
        finally:
            runner.IS_WINDOWS = orig
        self.assertNotIn("--json-schema", inv.args)
        self.assertIn("prompt-json", inv.note)

    def test_agy_modes_never_mix_p_and_stdin(self):
        ad = AgyAdapter(AgentCfg("a", "worker", "agy"))
        normal = ad.build(self.req(), ["agy"])
        self.assertEqual(normal.args[:2], ["-p", "hello\n中文"])
        self.assertIsNone(normal.stdin)
        self.assertNotIn("--input-format", normal.args)
        big = ad.build(self.req(prompt="x" * 7000, session_id="c1"), ["agy"])
        self.assertNotIn("-p", big.args)
        self.assertIn("--input-format", big.args)
        self.assertEqual(json.loads(big.stdin), {"event": "user", "message": {"content": "x" * 7000}})
        self.assertEqual(big.args[big.args.index("--conversation") + 1], "c1")
        forced = AgyAdapter(AgentCfg("a", "worker", "agy", options={"stdin_mode": "always"})).build(self.req(), ["agy"])
        self.assertNotIn("-p", forced.args)
        with self.assertRaises(ValueError):                       # effort unverified -> explicit error
            ad.build(self.req(effort="high"), ["agy"])

    def test_claude_parser_on_real_event_shapes(self):
        ad = ClaudeAdapter(AgentCfg("a", "manager", "claude"))
        seen = []
        p = ad.make_parser(self.req(), seen.append)
        for l in [
            '{"type":"system","subtype":"init","session_id":"S1"}',
            "garbage line that is not json",
            '{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"content":"Claude requested permissions to write to /x, but you haven\'t granted it yet."}]}}',
            '{"type":"result","subtype":"success","is_error":false,"session_id":"S1","result":"{}","structured_output":{"a":1},'
            '"permission_denials":[{"tool_name":"Write","tool_input":{"file_path":"/x"}}]}']:
            p.feed("stdout", l)
        res = p.finish(runner.ProcResult("ok", 0))
        self.assertTrue(res.ok)
        self.assertEqual(json.loads(res.text), {"a": 1})
        self.assertEqual(seen, ["S1"])
        self.assertEqual(res.denials, ["Write(/x)"])

    def test_claude_parser_missing_structured_output_is_not_faked(self):
        ad = ClaudeAdapter(AgentCfg("a", "manager", "claude"))
        p = ad.make_parser(self.req(), lambda s: None)
        p.feed("stdout", '{"type":"result","subtype":"success","is_error":false,"session_id":"S","result":"plain prose"}')
        res = p.finish(runner.ProcResult("ok", 0))
        self.assertEqual(res.text, "plain prose")                 # raw text goes on to JSON validation
        self.assertIsNone(protocol.extract_json(res.text)[0])

    def test_claude_parser_error_kinds(self):
        ad = ClaudeAdapter(AgentCfg("a", "manager", "claude"))
        p = ad.make_parser(self.req(), lambda s: None)
        p.feed("stdout", '{"type":"result","subtype":"success","is_error":true,"api_error_status":404,'
                         '"result":"There\'s an issue with the selected model (x). It may not exist"}')
        self.assertEqual(p.finish(runner.ProcResult("ok", 1)).error_kind, "model_invalid")
        p = ad.make_parser(self.req(), lambda s: None)
        p.feed("stdout", "No conversation found with session ID: abc")
        p.feed("stdout", '{"type":"result","subtype":"error_during_execution","is_error":true,"errors":["No conversation found with session ID: abc"]}')
        self.assertEqual(p.finish(runner.ProcResult("ok", 1)).error_kind, "session_lost")

    def test_agy_parser_tolerant(self):
        ad = AgyAdapter(AgentCfg("a", "worker", "agy"))
        seen = []
        p = ad.make_parser(self.req(), seen.append)
        p.feed("stdout", '{"type":"system","conversation_id":"C9"}')
        p.feed("stdout", '{"type":"assistant","message":{"content":[{"type":"text","text":"{\\"x\\":1}"}]}}')
        p.feed("stderr", "warning: tool call denied by policy (soft denial)")
        res = p.finish(runner.ProcResult("ok", 0))
        self.assertTrue(res.ok)
        self.assertEqual(seen, ["C9"])
        self.assertEqual(res.text, '{"x":1}')
        self.assertTrue(res.denials)


class TestConfig(unittest.TestCase):
    def test_default_config_valid(self):
        cfg = parse_config(json.loads(json.dumps(DEFAULT_CONFIG)))
        self.assertEqual(cfg.manager, "manager")
        self.assertEqual([w.id for w in cfg.workers], ["w1"])

    def test_rejections(self):
        def mod(f):
            d = json.loads(json.dumps(DEFAULT_CONFIG)); f(d); return d
        cases = [
            lambda d: d["agents"]["w1"].update(cli="codex"),
            lambda d: d["agents"]["w1"].update(role="boss"),
            lambda d: d["agents"]["w1"].update(model=3),
            lambda d: d["agents"]["w1"].update(effort=""),
            lambda d: d.update(manager="w1"),
            lambda d: d.update(turn_timeout_sec=0),
            lambda d: d.update(max_manager_turns_per_input=True),
            lambda d: d["agents"].update(w2=dict(d["agents"]["w1"])),      # MVP: one worker only
            lambda d: d["agents"].update({"me": dict(d["agents"]["w1"])}),
        ]
        for f in cases:
            with self.assertRaises(ConfigError, msg=str(f)):
                parse_config(mod(f))


class TestSnapshotStore(unittest.TestCase):
    def test_snapshot_diff_skips_and_symlinks(self):
        with tempfile.TemporaryDirectory(prefix="snap 目錄 ") as d, tempfile.TemporaryDirectory() as outside:
            root = Path(d)
            (root / "a.txt").write_text("1"); (root / "b.txt").write_text("2")
            (root / "node_modules").mkdir(); (root / "node_modules" / "x.js").write_text("x")
            (root / ".git").mkdir(); (root / ".git" / "HEAD").write_text("x")
            (Path(outside) / "secret.txt").write_text("s")
            try:
                os.symlink(outside, root / "link")
                linked = True
            except (OSError, NotImplementedError):
                linked = False
            s1 = take_snapshot(d)
            self.assertEqual(sorted(s1.files), ["a.txt", "b.txt"])
            (root / "a.txt").write_text("changed!"); (root / "c.txt").write_text("3"); (root / "b.txt").unlink()
            s2 = take_snapshot(d)
            diff = diff_snapshots(s1, s2)
            self.assertEqual((diff.added, diff.modified, diff.deleted), (["c.txt"], ["a.txt"], ["b.txt"]))
            self.assertTrue(diff.complete)
            if linked:
                self.assertNotIn("link/secret.txt", s2.files)

    def test_snapshot_limit_marks_incomplete(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(20):
                (Path(d) / f"f{i}").write_text("x")
            s = take_snapshot(d, max_files=5)
            self.assertFalse(s.complete)
            self.assertIn("INCOMPLETE", diff_snapshots(s, s).summary())

    def test_redaction(self):
        os.environ["MY_SECRET_TOKEN"] = "supersecretvalue123"
        try:
            t = redact('key sk-abcdefghijklmnopqrstuvwx and "api_key": "abc123456789" Bearer abcdefghijklmnopqrstuvwxyz '
                       'and supersecretvalue123 and ghp_abcdefghijklmnopqrstuvwxyz0123456789')
        finally:
            del os.environ["MY_SECRET_TOKEN"]
        for leaked in ("sk-abcdefgh", "abc123456789", "abcdefghijklmnopqrstuvwxyz ", "supersecretvalue123", "ghp_abcdef"):
            self.assertNotIn(leaked, t)

    def test_atomic_write_and_raw_cap(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            for i in range(50):
                atomic_write_json(p, {"i": i, "中文": "ok"})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["i"], 49)
            self.assertFalse((Path(d) / "s.json.tmp").exists())
            st = Store(Path(d) / "sessions")
            st.raw("a", "stdout", "x" * 10000)
            line = json.loads((st.dir / "raw" / "a.jsonl").read_text(encoding="utf-8"))
            self.assertLess(len(line["l"]), 2100)


if __name__ == "__main__":
    unittest.main()
