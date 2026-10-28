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


def su(step_type, conv="conv-1", **inner):
    """Official nested step_update event: step fields live under the inner "step_update" object."""
    body = {"conversation_id": conv, "step_type": step_type}
    body.update(inner)
    return json.dumps({"event": "step_update", "step_update": body})


def delta(text, conv="conv-1"):
    return su("agent_response", conv, text_delta=text)


def result_ev(conv="conv-1", **fields):
    return json.dumps({"event": "result", "result": dict({"conversation_id": conv}, **fields)})


OFFICIAL_OK = [
    '{"event":"init","conversation_id":"conv-1"}',
    delta('{"status":'),
    su("tool_call", text_delta="IGNORE ME", tool_info={"name": "ls"}),
    delta(' "done"}'),
    '{"event":"result","result":{"conversation_id":"conv-1","status":"SUCCESS","response":"{\\"status\\": \\"done\\"}"}}',
]


def agy_parse(lines, stderr=(), exit_code=0, session_id=None, status="ok"):
    seen = []
    ad = AgyAdapter(AgentCfg("a", "worker", "agy"))
    p = ad.make_parser(TurnRequest("a", "x", ".", session_id), seen.append)
    for l in lines:
        p.feed("stdout", l)
    for l in stderr:
        p.feed("stderr", l)
    res = AgyAdapter._classify_process(runner.ProcResult(status, exit_code), p)
    return res, seen


class TestAgyOfficialParser(unittest.TestCase):
    def test_official_init_step_result_success(self):
        res, seen = agy_parse(OFFICIAL_OK)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.text, '{"status": "done"}')          # result.response, not an envelope
        self.assertEqual(protocol.extract_json(res.text)[0], {"status": "done"})
        self.assertEqual(res.session_id, "conv-1")
        self.assertEqual(seen, ["conv-1"])                         # saved exactly once

    def test_terminal_response_is_not_duplicated_with_deltas(self):
        res, _ = agy_parse(OFFICIAL_OK)
        self.assertEqual(res.text.count("done"), 1)

    def test_text_deltas_concatenate_without_inserted_newlines(self):
        lines = ['{"event":"init","conversation_id":"c"}'] + [
            delta(d, "c") for d in ['{"a":', ' "he', 'llo wor', 'ld"}']] + [
            '{"event":"result","result":{"conversation_id":"c","status":"SUCCESS"}}']
        res, _ = agy_parse(lines)                                  # no response -> deltas are the fallback
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.text, '{"a": "hello world"}')
        self.assertNotIn("\n", res.text)

    def test_non_agent_response_steps_are_ignored(self):
        lines = ['{"event":"init","conversation_id":"c"}',
                 su("tool_output", "c", text_delta="SECRET TOOL OUTPUT"),
                 su("usage", "c", text_delta="tokens=5"),
                 '{"event":"result","result":{"conversation_id":"c","status":"SUCCESS"}}']
        res, _ = agy_parse(lines)
        self.assertFalse(res.ok)                                   # nothing from the assistant -> not success
        self.assertEqual(res.error_kind, "no_output")

    def test_structured_output_preferred(self):
        lines = ['{"event":"init","conversation_id":"c"}',
                 '{"event":"result","result":{"conversation_id":"c","status":"SUCCESS","response":"prose","structured_output":{"say":"hi","n":1}}}']
        res, _ = agy_parse(lines)
        self.assertEqual(json.loads(res.text), {"say": "hi", "n": 1})
        lines[1] = lines[1].replace('{"say":"hi","n":1}', '"{\\"say\\": \\"str\\"}"')   # JSON string form
        res, _ = agy_parse(lines)
        self.assertEqual(json.loads(res.text), {"say": "str"})
        lines[1] = lines[1].replace('"structured_output":"{\\"say\\": \\"str\\"}"', '"structured_output":"not json"')
        res, _ = agy_parse(lines)
        self.assertEqual(res.text, "prose")                        # invalid structured_output -> response

    def test_error_status_with_exit_code_zero_is_failure_even_with_text(self):
        for status in ("ERROR", "CANCELED", "INTERRUPTED", "INVALID", "error", None):
            r = {"conversation_id": "c", "response": '{"status":"done"}', "error": "backend failed"}
            if status:
                r["status"] = status
            res, _ = agy_parse(['{"event":"init","conversation_id":"c"}',
                                json.dumps({"event": "result", "result": r})], exit_code=0)
            self.assertFalse(res.ok, status)
            self.assertIn("backend failed", res.error)
            self.assertEqual(res.text, "")

    def test_error_without_response(self):
        res, _ = agy_parse(['{"event":"init","conversation_id":"c"}',
                            '{"event":"result","result":{"conversation_id":"c","status":"ERROR","error":{"message":"quota exhausted"}}}'])
        self.assertFalse(res.ok)
        self.assertIn("quota exhausted", res.error)

    def test_missing_terminal_event_is_never_success(self):
        res, _ = agy_parse(OFFICIAL_OK[:-1], exit_code=0)          # partial stream, process exited 0
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, "no_output")
        self.assertIn("terminal result", res.error)

    def test_success_with_nonzero_exit_is_failure(self):
        res, _ = agy_parse(OFFICIAL_OK, exit_code=3)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, "exit")

    def test_session_id_sources_and_no_overwrite(self):
        res, seen = agy_parse([delta("x", "from-step"),
                               '{"event":"result","result":{"conversation_id":"from-result","status":"SUCCESS","response":"x"}}'])
        self.assertEqual(res.session_id, "from-step")              # first id wins, later metadata can't overwrite
        self.assertEqual(seen, ["from-step"])
        res, seen = agy_parse(['{"event":"result","result":{"conversation_id":"only-result","status":"SUCCESS","response":"x"}}'])
        self.assertEqual(seen, ["only-result"])

    def test_timeout_and_cancel_propagate(self):
        for st, kind in (("timeout", "timeout"), ("cancelled", "cancelled")):
            res, _ = agy_parse(OFFICIAL_OK[:2], exit_code=None, status=st)
            self.assertFalse(res.ok)
            self.assertEqual(res.error_kind, kind)
            self.assertTrue(res.error)


class TestAgySessionGuard(unittest.TestCase):
    def test_matching_conversation_is_fine_and_not_resaved(self):
        res, seen = agy_parse(OFFICIAL_OK, session_id="conv-1")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(seen, [])
        self.assertEqual(res.session_id, "conv-1")

    def test_different_conversation_is_session_lost_and_never_saved(self):
        res, seen = agy_parse([l.replace("conv-1", "conv-OTHER") for l in OFFICIAL_OK], session_id="conv-1")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, "session_lost")
        self.assertEqual(seen, [])                                  # wrong id never persisted as a resume id
        self.assertEqual(res.session_id, "conv-1")                  # expected id stays the recorded one
        self.assertIn("conv-OTHER", res.error)

    def test_mismatch_only_in_result_event_also_detected(self):
        lines = ['{"event":"init","conversation_id":"conv-1"}',
                 '{"event":"result","result":{"conversation_id":"conv-2","status":"SUCCESS","response":"x"}}']
        res, _ = agy_parse(lines, session_id="conv-1")
        self.assertEqual(res.error_kind, "session_lost")

    def test_resume_without_any_id_in_stream_is_not_flagged(self):
        res, _ = agy_parse(['{"event":"result","result":{"status":"SUCCESS","response":"x"}}'], session_id="conv-1")
        self.assertTrue(res.ok)


class TestAgyEffortAndErrors(unittest.TestCase):
    def req(self, **kw):
        base = dict(agent_id="a", prompt="p", cwd=".", session_id=None)
        base.update(kw)
        return TurnRequest(**base)

    def test_effort_none_adds_no_flag(self):
        inv = AgyAdapter(AgentCfg("a", "worker", "agy")).build(self.req(), ["agy"])
        self.assertNotIn("--effort", inv.args)

    def test_effort_high_adds_flag_both_modes(self):
        ad = AgyAdapter(AgentCfg("a", "worker", "agy"))
        inv = ad.build(self.req(effort="high"), ["agy"])
        self.assertEqual(inv.args[inv.args.index("--effort") + 1], "high")
        inv = ad.build(self.req(effort="high", prompt="x" * 7000), ["agy"])
        self.assertIn("--input-format", inv.args)
        self.assertEqual(inv.args[inv.args.index("--effort") + 1], "high")

    def test_error_classification_stderr_and_result_error(self):
        cases = [
            (dict(stderr=["error: invalid effort level 'zzz'"]), "effort_invalid"),
            (dict(stderr=["error: unknown model 'nope'"]), "model_invalid"),
            (dict(stderr=["Error: model gemini-x does not support effort high"]), "effort_invalid"),
        ]
        for kw, kind in cases:                                      # (a) stderr, no official events
            res, _ = agy_parse([], exit_code=2, **kw)
            self.assertEqual(res.error_kind, kind, kw)
            self.assertFalse(res.ok)
        for err, kind in (("Model does not support effort 'high'", "effort_invalid"),   # (b) result.error
                          ("Unknown model: nope", "model_invalid"),
                          ("something else broke", "cli_error")):
            res, _ = agy_parse(['{"event":"init","conversation_id":"c"}',
                                json.dumps({"event": "result", "result": {"conversation_id": "c", "status": "ERROR", "error": err}})])
            self.assertEqual(res.error_kind, kind, err)
            self.assertIn(err, res.error)                           # the real CLI message is passed on


class TestAgyNestedStepUpdate(unittest.TestCase):
    """Official layout: {"event":"step_update","step_update":{...}} (https://antigravity.google/docs/cli/headless/)."""

    def test_A_streaming_fallback_when_result_response_missing(self):
        lines = ['{"event":"init","conversation_id":"c"}'] + [
            delta(d, "c") for d in ['{"status": "do', 'ne", "task_id"', ': "t1", "text": "x",', ' "files_touched": []}']
        ] + [result_ev("c", status="SUCCESS")]
        res, _ = agy_parse(lines)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.text, '{"status": "done", "task_id": "t1", "text": "x", "files_touched": []}')
        self.assertEqual(protocol.extract_json(res.text)[0]["task_id"], "t1")
        self.assertNotIn("\n", res.text)

    def test_A2_terminal_response_wins_over_deltas_no_mixing(self):
        lines = ['{"event":"init","conversation_id":"c"}', delta("PARTIAL ", "c"), delta("DELTAS", "c"),
                 result_ev("c", status="SUCCESS", response="FINAL")]
        res, _ = agy_parse(lines)
        self.assertEqual(res.text, "FINAL")

    def test_B_nested_tool_info_policy_denial_with_success_exit0_no_stderr(self):
        lines = ['{"event":"init","conversation_id":"c"}',
                 su("tool_call", "c", tool_info={"name": "write_file", "error": "Tool call denied by policy: requires approval"}),
                 delta('{"status":"done"}', "c"), result_ev("c", status="SUCCESS")]
        res, _ = agy_parse(lines, stderr=[], exit_code=0)
        self.assertTrue(res.ok)
        self.assertEqual(len(res.denials), 1)
        self.assertIn("write_file", res.denials[0])
        self.assertIn("denied by policy", res.denials[0])

    def test_B2_denial_evidence_downgrades_worker_done_to_blocked(self):
        # end-to-end through the real Engine: nested tool_info denial + worker claims done -> task blocked
        from tests.rig import Rig, M, W, dispatch, final
        rig = Rig({"manager": [M([dispatch("w1", "t1", "寫檔")]), M([final("被擋住了")])],
                   "w1": [W("done", "t1", "完成了！", denial=[{"tool_name": "write_file", "tool_input": {}}])]})
        try:
            # swap the fake worker for fake_agy so the denial comes from agy's nested tool_info.error
            scen = Path(rig.tmp) / "agy.log"
            os.environ["FAKE_AGY_LOG"] = str(scen)
            os.environ["FAKE_AGY_MODE"] = "tool_denial_done"
            from agent.config import AgentCfg as AC
            from agent.adapters.agy import AgyAdapter as AA
            rig.engine.agents["w1"].adapter = AA(AC("w1", "worker", "agy",
                                                    command=str(Path(__file__).with_name("fake_agy.py"))))
            rig.say("go")
            self.assertEqual(rig.store.state["tasks"]["t1"]["status"], "blocked")
            p = rig.calls("manager")[1]["prompt"]
            self.assertIn("status blocked", p)
            self.assertIn("write_file", p)
            self.assertIn("denied by policy", p)
        finally:
            os.environ.pop("FAKE_AGY_MODE", None)
            rig.close()

    def test_C_ordinary_tool_errors_are_not_policy_denials(self):
        lines = ['{"event":"init","conversation_id":"c"}']
        for err in ("ENOENT: no such file or directory, open 'x'", "EACCES: permission denied, open 'y'",
                    "EPERM: operation not permitted", "command exited with code 1"):
            lines.append(su("tool_call", "c", tool_info={"name": "t", "error": err}))
        lines.append(result_ev("c", status="SUCCESS", response="r"))
        res, _ = agy_parse(lines, stderr=[], exit_code=0)
        self.assertEqual(res.denials, [])

    def test_D_inner_conversation_id_is_read_and_guarded(self):
        lines = [delta("x", "inner-id"), result_ev(status="SUCCESS", response="x", conv="inner-id")]
        res, seen = agy_parse(lines)
        self.assertEqual((res.session_id, seen), ("inner-id", ["inner-id"]))
        res, seen = agy_parse(lines, session_id="inner-id")
        self.assertTrue(res.ok)
        self.assertEqual(seen, [])
        res, seen = agy_parse([delta("x", "OTHER"), result_ev(status="SUCCESS", response="x", conv="OTHER")],
                              session_id="inner-id")
        self.assertEqual(res.error_kind, "session_lost")
        self.assertEqual((res.session_id, seen), ("inner-id", []))

    def test_inner_object_wins_over_stray_outer_fields(self):
        ev = json.dumps({"event": "step_update", "step_type": "agent_response", "text_delta": "OUTER",
                         "step_update": {"step_type": "agent_response", "text_delta": "INNER"}})
        res, _ = agy_parse(['{"event":"init","conversation_id":"c"}', ev, result_ev("c", status="SUCCESS")])
        self.assertEqual(res.text, "INNER")

    def test_legacy_flat_step_update_still_accepted(self):   # LEGACY layout, not the official one
        lines = ['{"event":"init","conversation_id":"c"}',
                 '{"event":"step_update","step_type":"agent_response","text_delta":"flat"}',
                 '{"event":"step_update","step_type":"tool_call","tool_info":{"name":"w","error":"auto-denied"}}',
                 result_ev("c", status="SUCCESS")]
        res, _ = agy_parse(lines)
        self.assertEqual(res.text, "flat")
        self.assertEqual(len(res.denials), 1)


class TestAgyDenials(unittest.TestCase):
    def test_soft_denial_on_stderr_with_exit_zero(self):
        res, _ = agy_parse(OFFICIAL_OK, stderr=["tool run_command auto-denied in headless mode (soft denial)"], exit_code=0)
        self.assertTrue(res.ok)
        self.assertEqual(len(res.denials), 1)
        self.assertIn("auto-denied", res.denials[0])

    def test_tool_info_error_with_denial_evidence(self):
        lines = ['{"event":"init","conversation_id":"c"}',
                 su("tool_call", "c", tool_info={"name": "write_file", "error": "Tool call denied by policy: requires approval"}),
                 '{"event":"result","result":{"conversation_id":"c","status":"SUCCESS","response":"claimed done"}}']
        res, _ = agy_parse(lines)
        self.assertTrue(res.ok)
        self.assertEqual(len(res.denials), 1)
        self.assertIn("write_file", res.denials[0])

    def test_ordinary_tool_errors_are_not_denials(self):
        lines = ['{"event":"init","conversation_id":"c"}',
                 su("tool_call", "c", tool_info={"name": "read_file", "error": "ENOENT: no such file"}),
                 su("tool_call", "c", tool_info={"name": "read_file", "error": "EACCES: permission denied, open x"}),
                 '{"event":"result","result":{"conversation_id":"c","status":"SUCCESS","response":"r"}}']
        res, _ = agy_parse(lines, stderr=["warn: operation not permitted on socket", "retrying after timeout"])
        self.assertEqual(res.denials, [])


class TestAgyErrorClassification(unittest.TestCase):
    CASES = [
        ('Invalid effort "turbo" (valid: low, medium, high, xhigh, max)', "effort_invalid"),
        ('Model has no "max" effort (available: low, medium, high)', "effort_invalid"),
        ("effort isn't adjustable for the current model", "effort_invalid"),
        ('invalid model selection (--model "does-not-exist-model" --effort ""): model does-not-exist-model is not recognized',
         "model_invalid"),
        ('invalid model selection (--model "gem" --effort "max"): Model has no "max" effort', "effort_invalid"),
        ("Unknown model: nope", "model_invalid"),
        ("backend exploded", "cli_error"),
        # real agy 1.4.9 (Windows): model echo AND the effort cause in one message -> effort wins
        ('agy turn status ERROR: invalid model selection (--model "" --effort "not-an-effort"): invalid --effort ...',
         "effort_invalid"),
        ('invalid model selection (--model "" --effort "turbo"): invalid --effort "turbo" (valid: low, high)',
         "effort_invalid"),
        # echoed `--effort ""` must not turn a bad model into an effort error
        ('invalid model selection (--model "zzz" --effort ""): model zzz is not recognized', "model_invalid"),
        ('invalid model selection (--model "zzz" --effort "high")', "model_invalid"),
        ('Model "gem" does not support effort "high"', "effort_invalid"),
        ("effort is not adjustable for this model", "effort_invalid"),
    ]

    def test_classify_error_table(self):
        from agent.adapters.agy import classify_error
        for text, kind in self.CASES:
            self.assertEqual(classify_error(text), kind, text)

    def test_from_stderr_and_from_result_error_keep_real_message(self):
        for text, kind in self.CASES:
            res, _ = agy_parse([], stderr=["error: " + text], exit_code=2)          # stderr, no events
            self.assertEqual(res.error_kind, "exit" if kind == "cli_error" else kind, ("stderr", text))
            res, _ = agy_parse(['{"event":"init","conversation_id":"c"}',
                                result_ev("c", status="ERROR", error=text)])         # result.error
            self.assertEqual(res.error_kind, kind, ("result.error", text))
            self.assertIn(text, res.error)                                           # message reaches the Manager


class TestAgyViaSubprocess(unittest.TestCase):
    """Real subprocess runner against tests/fake_agy.py (official-shaped events)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agy fake 測試 "))
        self.log = self.tmp / "log.jsonl"
        os.environ["FAKE_AGY_LOG"] = str(self.log)
        self.ad = AgyAdapter(AgentCfg("w1", "worker", "agy", command=str(Path(__file__).with_name("fake_agy.py"))))

    def tearDown(self):
        os.environ.pop("FAKE_AGY_MODE", None)

    def run_turn(self, mode="ok", **kw):
        os.environ["FAKE_AGY_MODE"] = mode
        base = dict(agent_id="w1", prompt="多行\n提示", cwd=str(self.tmp), session_id=None, timeout=20)
        base.update(kw)
        saved = []
        res = self.ad.run(TurnRequest(**base), threading.Event(), saved.append)
        calls = [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines()]
        return res, saved, calls

    def test_success_first_then_resume(self):
        res, saved, calls = self.run_turn()
        self.assertTrue(res.ok, res.error)
        self.assertEqual((saved, res.session_id), (["conv-new-1"], "conv-new-1"))
        self.assertEqual(calls[0]["argv"][:2], ["-p", "多行\n提示"])
        res, saved, calls = self.run_turn(session_id="conv-new-1", effort="high")
        self.assertTrue(res.ok)
        a = calls[-1]["argv"]
        self.assertEqual(a[a.index("--conversation") + 1], "conv-new-1")
        self.assertEqual(a[a.index("--effort") + 1], "high")
        self.assertEqual(saved, [])

    def test_stdin_mode_resume_sends_prompt_only_on_stdin(self):
        self.ad.cfg.options["stdin_mode"] = "always"
        res, _, calls = self.run_turn(session_id="conv-new-1")
        self.assertTrue(res.ok, res.error)
        self.assertNotIn("-p", calls[0]["argv"])
        self.assertEqual(json.loads(calls[0]["stdin"])["message"]["content"], "多行\n提示")

    def test_conversation_switch_is_session_lost(self):
        res, saved, _ = self.run_turn("other_conv", session_id="conv-new-1")
        self.assertEqual(res.error_kind, "session_lost")
        self.assertEqual(saved, [])

    def test_error_modes(self):
        for mode, kind in (("effort_err_stderr", "effort_invalid"), ("effort_err_result", "effort_invalid"),
                           ("real_effort_err_stderr", "effort_invalid"), ("real_effort_err_result", "effort_invalid"),
                           ("real_model_err_result", "model_invalid"),
                           ("model_err_stderr", "model_invalid"), ("model_err_result", "model_invalid"),
                           ("error_status", "cli_error"), ("no_result", "no_output")):
            res, _, _ = self.run_turn(mode, effort="high" if "effort" in mode else None,
                                      model="m" if ("model" in mode or mode.startswith("real_effort")) else None)
            self.assertFalse(res.ok, mode)
            self.assertEqual(res.error_kind, kind, mode + str(res.error))

    def test_nested_events_parse_through_real_subprocess(self):
        raw = Path(__file__).with_name("fake_agy.py").read_text(encoding="utf-8")
        self.assertIn('"step_update": {"conversation_id"', raw)      # fixture really emits the nested layout
        res, saved, _ = self.run_turn("no_response")                  # SUCCESS w/o response -> nested deltas used
        self.assertTrue(res.ok, res.error)
        self.assertEqual(json.loads(res.text)["task_id"], "t1")
        self.assertEqual(saved, ["conv-new-1"])

    def test_nested_tool_denial_through_subprocess(self):
        res, _, _ = self.run_turn("tool_denial")
        self.assertTrue(res.ok)
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(len(res.denials), 1)
        self.assertIn("denied by policy", res.denials[0])

    def test_nested_ordinary_tool_errors_through_subprocess(self):
        res, _, _ = self.run_turn("tool_error")
        self.assertTrue(res.ok)
        self.assertEqual(res.denials, [])

    def test_stderr_soft_denial_with_success(self):
        res, _, _ = self.run_turn("denial_stderr")
        self.assertTrue(res.ok)
        self.assertTrue(res.denials)

    def test_timeout_and_cancel_still_reported(self):
        res, _, _ = self.run_turn("sleep", timeout=1.0)
        self.assertEqual(res.error_kind, "timeout")
        self.assertTrue(res.error)
        os.environ["FAKE_AGY_MODE"] = "sleep"
        cancel = threading.Event()
        threading.Timer(0.8, cancel.set).start()
        res = self.ad.run(TurnRequest("w1", "p", str(self.tmp), None, timeout=30), cancel, lambda s: None)
        self.assertEqual(res.error_kind, "cancelled")
        self.assertTrue(res.error)


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
