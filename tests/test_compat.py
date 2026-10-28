"""CLI-update resilience: version check and parser behaviour on unexpected output. No model calls."""
import json
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import compat, runner  # noqa: E402
from agent.adapters.agy import AgyAdapter  # noqa: E402
from agent.adapters.base import TurnRequest  # noqa: E402
from agent.adapters.claude import ClaudeAdapter  # noqa: E402
from agent.adapters.fake import FakeAdapter  # noqa: E402
from agent.config import AgentCfg, parse_config  # noqa: E402
from agent.orchestrator import Engine  # noqa: E402
from agent.store import Store  # noqa: E402
from agent.ui import BufferUI  # noqa: E402
from tests.test_units import agy_parse, result_ev, su  # noqa: E402


def claude_parse(lines, stderr=(), exit_code=0, session_id=None):
    ad = ClaudeAdapter(AgentCfg("a", "manager", "claude"))
    p = ad.make_parser(TurnRequest("a", "x", ".", session_id, schema={"type": "object"}), lambda s: None)
    for l in lines:
        p.feed("stdout", l)
    for l in stderr:
        p.feed("stderr", l)
    return ClaudeAdapter._classify_process(runner.ProcResult("ok", exit_code), p)


CL_INIT = '{"type":"system","subtype":"init","session_id":"s1"}'
CL_OK = ('{"type":"result","subtype":"success","is_error":false,"session_id":"s1","result":"r",'
         '"structured_output":{"say":"hi","actions":[]}}')


class TestClaudeParserResilience(unittest.TestCase):
    def test_valid_events(self):
        res = claude_parse([CL_INIT, CL_OK])
        self.assertTrue(res.ok)
        self.assertEqual(json.loads(res.text)["say"], "hi")
        self.assertEqual(res.notes, [])

    def test_extra_unknown_events_are_ignored_but_reported(self):
        res = claude_parse([CL_INIT, '{"type":"brand_new_thing","x":1}', '{"foo":"bar"}', CL_OK,
                            '{"type":"brand_new_thing"}'])
        self.assertTrue(res.ok)                                    # harmless extras never fail a good turn
        self.assertEqual(json.loads(res.text)["say"], "hi")
        self.assertTrue(any("'brand_new_thing' x2" in n for n in res.notes), res.notes)
        self.assertTrue(any("<no type>" in n for n in res.notes), res.notes)

    def test_missing_result_with_unfamiliar_events_is_unknown_format(self):
        res = claude_parse([CL_INIT, '{"type":"v2_final","payload":{"text":"hello"}}'])
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, "unknown_format")
        self.assertIn("v2_final", res.error)
        self.assertEqual(res.text, "")                              # nothing guessed from the unknown event

    def test_missing_result_with_only_known_events_is_plain_no_output(self):
        res = claude_parse([CL_INIT, '{"type":"assistant","message":{"content":[{"type":"text","text":"partial"}]}}'])
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, "no_output")

    def test_missing_result_with_nonzero_exit_is_exit(self):
        res = claude_parse([CL_INIT, '{"type":"v2_final"}'], exit_code=3)
        self.assertEqual(res.error_kind, "exit")

    def test_stdout_not_json_at_all(self):
        res = claude_parse(["Welcome to Claude 9!", "please log in"])
        self.assertEqual(res.error_kind, "unknown_format")

    def test_result_event_with_changed_shape(self):
        res = claude_parse([CL_INIT, '{"type":"result","session_id":"s1","payload":{"answer":"hi"}}'])
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, "unknown_format")
        self.assertIn("payload", res.error)

    def test_cli_argument_error_is_exit_not_format(self):
        res = claude_parse([], stderr=["error: unknown option '--frobnicate'"], exit_code=2)
        self.assertEqual(res.error_kind, "exit")

    def test_denial_and_ordinary_error_are_distinguished(self):
        denied = ('{"type":"result","subtype":"success","is_error":false,"session_id":"s1","result":"r",'
                  '"structured_output":{"say":"","actions":[]},'
                  '"permission_denials":[{"tool_name":"Write","tool_input":{"file_path":"x.txt"}}]}')
        res = claude_parse([CL_INIT, denied])
        self.assertTrue(res.ok)
        self.assertEqual(res.denials, ["Write(x.txt)"])
        boom = '{"type":"result","subtype":"error_during_execution","is_error":true,"session_id":"s1","result":"boom"}'
        res = claude_parse([CL_INIT, boom])
        self.assertEqual((res.ok, res.error_kind, res.denials), (False, "cli_error", []))


AGY_INIT = '{"event":"init","conversation_id":"c"}'


class TestAgyParserResilience(unittest.TestCase):
    def test_valid_events(self):
        res, _ = agy_parse([AGY_INIT, su("agent_response", "c", text_delta="hi"),
                            result_ev("c", status="SUCCESS", response="done")])
        self.assertTrue(res.ok)
        self.assertEqual(res.notes, [])

    def test_extra_unknown_events_and_steps_do_not_fail_a_good_turn(self):
        res, _ = agy_parse([AGY_INIT, '{"event":"heartbeat","n":1}', su("quantum_step", "c"),
                            '{"event":"heartbeat","n":2}', result_ev("c", status="SUCCESS", response="done")])
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "done")
        self.assertTrue(any("heartbeat" in n and "x2" in n for n in res.notes), res.notes)
        self.assertTrue(any("quantum_step" in n for n in res.notes), res.notes)

    def test_missing_terminal_result(self):
        res, _ = agy_parse([AGY_INIT, su("agent_response", "c", text_delta="partial")])
        self.assertEqual((res.ok, res.error_kind), (False, "no_output"))
        res, _ = agy_parse([AGY_INIT, '{"event":"finished","text":"hello"}'])
        self.assertEqual((res.ok, res.error_kind), (False, "unknown_format"))
        self.assertEqual(res.text, "")

    def test_result_shape_changed(self):
        res, _ = agy_parse([AGY_INIT, '{"event":"result","result":"just a string"}'])
        self.assertEqual((res.ok, res.error_kind), (False, "unknown_format"))
        res, _ = agy_parse([AGY_INIT, '{"event":"result","result":{"conversation_id":"c","answer":"x"}}'])
        self.assertEqual((res.ok, res.error_kind), (False, "unknown_format"))

    def test_session_id_mismatch(self):
        res, seen = agy_parse([ '{"event":"init","conversation_id":"OTHER"}',
                               result_ev("OTHER", status="SUCCESS", response="done")], session_id="c")
        self.assertEqual((res.ok, res.error_kind), (False, "session_lost"))
        self.assertEqual(seen, [])

    def test_argument_error_and_denial_vs_system_error(self):
        res, _ = agy_parse([], stderr=["error: unknown flag: --frobnicate"], exit_code=2)
        self.assertFalse(res.ok)
        self.assertNotEqual(res.error_kind, "unknown_format")
        res, _ = agy_parse([AGY_INIT, result_ev("c", status="SUCCESS", response="ok")],
                           stderr=["tool run_command auto-denied in headless mode"])
        self.assertTrue(res.ok)
        self.assertEqual(len(res.denials), 1)
        res, _ = agy_parse([AGY_INIT, result_ev("c", status="ERROR", error="disk full")])
        self.assertEqual((res.error_kind, res.denials), ("cli_error", []))


class TestVersionHelpers(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(compat.parse_version("2.1.312 (Claude Code)"), "2.1.312")
        self.assertEqual(compat.parse_version("agy version 1.4.9\n"), "1.4.9")
        self.assertIsNone(compat.parse_version("no digits here"))

    def test_assess_never_claims_full_compatibility(self):
        self.assertEqual(compat.assess("claude", "2.1.312")[0], "tested")
        st, note = compat.assess("agy", "1.4.9")
        self.assertEqual(st, "partial")
        self.assertIn("not observed", note)
        self.assertEqual(compat.assess("claude", "9.0.0")[0], "unverified")
        self.assertEqual(compat.assess("agy", None)[0], "unknown")
        self.assertNotIn("full", " ".join(n for v in compat.KNOWN.values() for _, n in v.values()).lower())

    def test_notice_only_when_needed(self):
        mk = lambda status, ver="1.0.0": dict(cli="claude", version=ver, status=status, note="n")  # noqa: E731
        self.assertIsNone(compat.startup_notice(mk("tested")))
        self.assertIsNone(compat.startup_notice(mk("partial")))
        self.assertIn("1.0.0", compat.startup_notice(mk("unverified")))
        self.assertIn("無法讀取", compat.startup_notice(mk("unknown", None)))


class TestVersionCheckProcess(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ver check "))

    def script(self, name, body):
        p = self.tmp / name
        p.write_text(textwrap.dedent(body), encoding="utf-8")
        return runner.resolve_command(str(p))

    def test_reads_version(self):
        pre = self.script("ok.py", "import sys\nassert sys.argv[1:] == ['--version']\nprint('2.1.312 (Claude Code)')\n")
        info = compat.check_version("claude", pre, str(self.tmp))
        self.assertEqual((info["ok"], info["version"], info["status"]), (True, "2.1.312", "tested"))

    def test_failures_never_raise(self):
        cases = {"hang.py": "import time\ntime.sleep(30)\n", "bad.py": "import sys\nsys.exit(4)\n",
                 "junk.py": "print('hello world')\n"}
        for name, body in cases.items():
            t0 = time.monotonic()
            info = compat.check_version("claude", self.script(name, body), str(self.tmp), timeout=1.5)
            self.assertFalse(info["ok"], name)
            self.assertEqual(info["status"], "unknown", name)
            self.assertLess(time.monotonic() - t0, 10, name)
        info = compat.check_version("claude", [str(self.tmp / "nope.exe")], str(self.tmp))
        self.assertFalse(info["ok"])
        self.assertIn("could not start", info["note"])


class StubAdapter(FakeAdapter):
    def version_args_supported(self):
        return True


class TestEngineVersionCheck(unittest.TestCase):
    def make(self, script_body, cli="claude"):
        tmp = Path(tempfile.mkdtemp(prefix="eng ver "))
        script = tmp / "v.py"
        script.write_text(textwrap.dedent(script_body), encoding="utf-8")
        cfg = parse_config({"manager": "m", "agents": {
            "m": {"role": "manager", "cli": "fake", "command": str(script)},
            "w": {"role": "worker", "cli": "fake", "command": str(script)}}})
        store, ui = Store(tmp / "sessions"), BufferUI()
        eng = Engine(cfg, store, ui, str(tmp), adapter_factory=StubAdapter)
        return eng, store, ui

    def test_version_recorded_logged_and_shown_in_status(self):
        eng, store, ui = self.make("print('9.9.9')\n")
        eng.check_cli_versions(wait=True)
        self.assertEqual(eng.cli_versions["m"]["version"], "9.9.9")
        st = eng.status_text()
        self.assertIn("9.9.9", st)
        self.assertIn("[unverified]", st)
        rows = [json.loads(l) for l in store.events_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue([r for r in rows if r["type"] == "cli_version" and r["version"] == "9.9.9"])
        self.assertTrue(any("9.9.9" in t for t in ui.texts("system")))   # soft notice, nothing else changes

    def test_check_does_not_block_and_failure_is_soft(self):
        eng, _, ui = self.make("import time\ntime.sleep(60)\n")
        t0 = time.monotonic()
        th = eng.check_cli_versions(timeout=1.0)                 # returns immediately: runs in the background
        self.assertLess(time.monotonic() - t0, 0.5)
        th.join(20)
        self.assertEqual(eng.cli_versions["m"]["status"], "unknown")
        self.assertTrue(any("無法讀取" in t for t in ui.texts("system")))
        eng.status_text()                                         # still fully usable


class TestChatStartsWithoutVersionCheck(unittest.TestCase):
    def test_fake_agents_skip_the_check(self):
        from tests.rig import Rig, M, final
        r = Rig({"manager": [M([final("ok")])], "w1": []})
        try:
            th = r.engine.check_cli_versions(wait=True)
            self.assertEqual(r.engine.cli_versions, {})
            r.say("hi")
            self.assertEqual(r.chat_lines(), ["Manager> @Me ok"])
        finally:
            r.close()


if __name__ == "__main__":
    unittest.main()
