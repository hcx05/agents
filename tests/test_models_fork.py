"""Per-turn model facts, Claude CLI compatibility guards, and the fork-session safety rules. Fake CLI only."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import protocol, resume, runner  # noqa: E402
from agent.adapters.agy import AgyAdapter, classify_error  # noqa: E402
from agent.adapters.base import TurnRequest  # noqa: E402
from agent.adapters.claude import ClaudeAdapter  # noqa: E402
from agent.adapters.fake import FakeAdapter  # noqa: E402
from agent.config import AgentCfg  # noqa: E402
from agent.orchestrator import State  # noqa: E402
from agent.store import Store  # noqa: E402
from tests.rig import Rig, M, W, dispatch, final  # noqa: E402
from tests.test_units import agy_parse, result_ev  # noqa: E402


def claude_parse(lines, req=None, stderr=(), exit_code=0):
    seen = []
    ad = ClaudeAdapter(AgentCfg("a", "manager", "claude"))
    p = ad.make_parser(req or TurnRequest("a", "x", ".", None, schema={"type": "object"}), seen.append)
    for l in lines:
        p.feed("stdout", l)
    for l in stderr:
        p.feed("stderr", l)
    return ClaudeAdapter._classify_process(runner.ProcResult("ok", exit_code), p), seen


def ev(**kw):
    return json.dumps(kw)


OK_RESULT = ev(type="result", subtype="success", is_error=False, session_id="S1", result="r",
               structured_output={"say": "", "actions": []})


class Base(unittest.TestCase):
    rig: Rig

    def tearDown(self):
        if getattr(self, "rig", None):
            self.rig.close()

    def chat_events(self):
        return [e for e in Store.read_events(self.rig.store.dir) if e.get("type") == "chat" and e.get("turn")]


class TestModelFromClaudeStream(unittest.TestCase):
    def test_reported_from_init_assistant_and_usage(self):
        res, _ = claude_parse([ev(type="system", subtype="init", session_id="S1", model="claude-a"),
                               ev(type="assistant", session_id="S1", message={"model": "claude-b", "content": []}),
                               ev(type="result", subtype="success", session_id="S1", result="r",
                                  structured_output={"say": "", "actions": []},
                                  modelUsage={"claude-b": {}, "claude-c": {}})])
        self.assertEqual(res.model_reported, "claude-b")                    # the model that produced the message
        self.assertEqual(res.models_all, ["claude-b", "claude-a", "claude-c"])

    def test_init_only_and_single_usage_key(self):
        res, _ = claude_parse([ev(type="system", subtype="init", session_id="S1", model="claude-a"), OK_RESULT])
        self.assertEqual(res.model_reported, "claude-a")
        res, _ = claude_parse([ev(type="system", subtype="init", session_id="S1"),
                               ev(type="result", subtype="success", session_id="S1", result="r",
                                  structured_output={}, modelUsage={"claude-z": {}})])
        self.assertEqual(res.model_reported, "claude-z")

    def test_nothing_reported_is_none_and_synthetic_ids_are_ignored(self):
        res, _ = claude_parse([ev(type="system", subtype="init", session_id="S1"), OK_RESULT])
        self.assertIsNone(res.model_reported)
        self.assertEqual(res.models_all, [])
        res, _ = claude_parse([ev(type="assistant", session_id="S1", message={"model": "<synthetic>", "content": []}),
                               OK_RESULT])
        self.assertIsNone(res.model_reported)

    def test_optional_metadata_is_optional_and_type_checked(self):
        res, _ = claude_parse([ev(type="system", subtype="init", session_id="S1", model="m", claude_code_version="9.9.9",
                                  apiKeySource="none", capabilities=["interrupt_receipt_v1", 5]), OK_RESULT])
        self.assertEqual(res.meta, {"cli_version": "9.9.9", "api_key_source": "none",
                                    "capabilities": ["interrupt_receipt_v1"]})
        res, _ = claude_parse([ev(type="system", subtype="init", session_id="S1", model="m", apiKeySource=7,
                                  claude_code_version=None), OK_RESULT])
        self.assertTrue(res.ok)                                              # nothing depends on these fields
        self.assertEqual(res.meta, {})


class TestModelFromAgy(unittest.TestCase):
    def test_only_an_explicit_model_string_counts(self):
        res, _ = agy_parse(['{"event":"init","conversation_id":"c"}', result_ev("c", status="SUCCESS", response="ok")])
        self.assertIsNone(res.model_reported)
        res, _ = agy_parse(['{"event":"init","conversation_id":"c","model":"gem-x"}',
                            result_ev("c", status="SUCCESS", response="ok")])
        self.assertEqual(res.model_reported, "gem-x")
        res, _ = agy_parse(['{"event":"init","conversation_id":"c","model":{"id":"x"}}',
                            result_ev("c", status="SUCCESS", response="ok")])
        self.assertIsNone(res.model_reported)                                # not a string: not guessed


class TestModelPerTurnInTheChatRoom(Base):
    def test_reported_requested_and_unknown_are_kept_apart(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "做")]), M([final("好")], omit_model=True)],
            "w1": [W("done", "t1", "完成", omit_model=True)]}, worker_model="gem-asked")
        r.say("開始")
        by_sender = {e["sender"]: e["turn"] for e in self.chat_events()}
        self.assertEqual(by_sender["w1"]["source"], "requested")             # asked for, CLI did not confirm
        self.assertEqual(by_sender["w1"]["requested"], "gem-asked")
        self.assertIsNone(by_sender["w1"]["reported"])                       # requested is NEVER promoted to reported
        sys_lines = r.ui.texts("system")
        self.assertIn("w1 · fake · 要求使用 gem-asked（CLI 未確認）", sys_lines)
        self.assertFalse(any("w1" in t and "CLI 回報" in t for t in sys_lines))
        finals = [e for e in self.chat_events() if e["sender"] == "Manager" and e.get("to") == "Me"]
        self.assertEqual(finals[0]["turn"]["source"], "unknown")             # nothing requested, nothing reported
        self.assertIn("Manager · fake · 模型變更：模型未知（CLI 未回報）", sys_lines)   # was reported before, now unknown

    def test_manager_dispatch_model_is_a_request_for_that_turn_only(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "a", model="gem-override")]), M([dispatch("w1", "t2", "b")]),
                        M([final("好")])],
            "w1": [W("done", "t1", "r1", omit_model=True), W("done", "t2", "r2", omit_model=True)]})
        r.say("開始")
        w_turns = [e["turn"] for e in self.chat_events() if e["sender"] == "w1"]
        self.assertEqual([t["requested"] for t in w_turns], ["gem-override", None])
        self.assertEqual([t["source"] for t in w_turns], ["requested", "unknown"])

    def test_model_change_between_turns_never_rewrites_history(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "x")], report_model="model-A"),
                        M([final("好")], report_model="model-B")],
            "w1": [W("done", "t1", "r")]})
        r.say("開始")
        turns = [e["turn"] for e in self.chat_events() if e["sender"] == "Manager"]
        self.assertEqual([t["reported"] for t in turns], ["model-A", "model-B"])
        sys_lines = r.ui.texts("system")
        self.assertIn("Manager · fake · model-A（CLI 回報）", sys_lines)
        self.assertIn("Manager · fake · 模型變更：model-B（CLI 回報）", sys_lines)
        r.engine.mgr.cfg.model = "something-else-now"                         # config changes later...
        items = resume.timeline(Store.read_events(r.store.dir))
        again = [i["turn"]["reported"] for i in items if i["kind"] == "chat" and i["sender"] == "Manager"]
        self.assertEqual(again, ["model-A", "model-B"])                      # ...the recorded turns stay as they were
        self.assertEqual(sum("model-A" in t for t in sys_lines), 1)          # announced once, not per message

    def test_assistant_message_model_beats_init_model(self):
        self.rig = r = Rig({"manager": [M([final("好")], report_model="init-m", assistant_model="asst-m")], "w1": []})
        r.say("hi")
        t = self.chat_events()[-1]["turn"]
        self.assertEqual((t["reported"], t["models"]), ("asst-m", ["asst-m", "init-m"]))
        self.assertIn("asst-m +1（CLI 回報）", " ".join(r.ui.texts("system")))        # +1 = a second id was reported too

    def test_ui_gets_the_turn_metadata_and_status_shows_the_last_turn(self):
        self.rig = r = Rig({"manager": [M([final("好")], report_model="model-A")], "w1": []})
        r.say("hi")
        metas = [m for m in r.ui.metas.values()]
        self.assertEqual(len(metas), 1)
        self.assertEqual(metas[0]["reported"], "model-A")
        st = r.engine.status_text()
        self.assertIn("last turn manager#1: model model-A（CLI 回報）", st)

    def test_resume_announces_only_changes(self):
        self.rig = r = Rig({"manager": [M([final("a")], report_model="model-A"), M([final("b")], report_model="model-A"),
                                        M([final("c")], report_model="model-C")], "w1": []})
        r.say("1")
        r.resume()
        r.say("2")
        self.assertFalse([t for t in r.ui.texts("system") if "model-A" in t])      # same model as before the restart
        r.say("3")
        self.assertTrue([t for t in r.ui.texts("system") if "模型變更：model-C" in t])


class TestClaudeGuards(Base):
    def test_auth_problem_stops_with_a_clear_message_and_changes_nothing(self):
        self.rig = r = Rig({"manager": [{"api_retry_error": "authentication_failed",
                                         "is_error": "Invalid API key · Please run /login"}, M([final("不該被呼叫")])],
                            "w1": []})
        r.say("hi")
        err = " ".join(r.ui.texts("error"))
        self.assertIn("auth_error", err)
        self.assertIn("不會自動改用 API Key", err)
        self.assertIn("不會修改 Claude 設定", err)
        self.assertIn("--bare", err)
        self.assertEqual(len(r.calls("manager")), 1)                          # no automatic retry with another setup
        self.assertEqual(r.engine.state, State.FAILED)

    def test_auth_error_is_recognized_from_text_alone_and_ordinary_errors_are_not(self):
        res, _ = claude_parse([ev(type="result", subtype="success", is_error=True, session_id="S1",
                                  result="Not logged in · Please run /login")])
        self.assertEqual(res.error_kind, "auth_error")
        res, _ = claude_parse([ev(type="result", subtype="success", is_error=True, session_id="S1", api_error_status=401,
                                  result="boom")])
        self.assertEqual(res.error_kind, "auth_error")
        res, _ = claude_parse([ev(type="result", subtype="error_during_execution", is_error=True, session_id="S1",
                                  result="disk full")])
        self.assertEqual(res.error_kind, "cli_error")
        res, _ = claude_parse([ev(type="system", subtype="api_retry", session_id="S1", error="overloaded", attempt=1),
                               ev(type="result", subtype="success", is_error=True, session_id="S1", result="overloaded")])
        self.assertEqual(res.error_kind, "cli_error")                         # retryable server trouble is not an auth problem

    def test_background_running_session_is_session_busy(self):
        self.rig = r = Rig({"manager": [{"raw_stdout": ["Session abc is running in the background. "
                                                        "Open it with: claude attach abc"], "exit": 1}], "w1": []})
        r.say("hi")
        err = " ".join(r.ui.texts("error"))
        self.assertIn("session_busy", err)
        self.assertIn("不會強行寫入", err)
        res, _ = claude_parse(["Your prompt was not sent to it: the session is running in the background"], exit_code=1)
        self.assertEqual(res.error_kind, "session_busy")
        res, _ = claude_parse(["something else broke"], exit_code=1)
        self.assertNotEqual(res.error_kind, "session_busy")

    def test_lost_session_message_says_nothing_is_replaced_silently(self):
        self.rig = r = Rig({"manager": [M([final("a")]), {"forget_sessions_of": "manager", "reply": {"say": "", "actions": []}}],
                            "w1": []})
        r.say("1")
        sid_before = r.engine.mgr.session_id
        r.say("2")
        err = " ".join(r.ui.texts("error"))
        self.assertIn("session_lost", err)
        self.assertIn("不會自動改用新 Session", err)
        self.assertEqual(r.store.state["agents"]["manager"]["session_id"], sid_before)   # saved id was not overwritten

    def test_manager_is_told_to_dispatch_instead_of_launching_the_worker_itself(self):
        text = protocol.manager_preamble("manager", [("w1", "agy")])
        self.assertIn("always use a 'dispatch' action", text)
        self.assertIn("Never start the worker's CLI yourself", text)
        self.assertIn("Doing the work yourself without any worker remains fine", text)
        self.assertIn("delegating is optional", text)                         # autonomy wording is unchanged

    def test_agy_effort_error_classification_does_not_regress(self):
        self.assertEqual(classify_error('agy turn status ERROR: invalid model selection (--model "" --effort '
                                        '"not-an-effort"): invalid --effort ...'), "effort_invalid")
        self.assertEqual(classify_error('invalid model selection (--model "zzz" --effort ""): model zzz is not recognized'),
                         "model_invalid")


FORBIDDEN = {"--bare", "--permission-mode", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
             "--allowedTools", "--allowed-tools", "--sandbox", "--permission-prompt-tool"}


class TestSessionIdGuardsAndFork(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent fork "))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fs = self.tmp / "fs"
        self.fs.mkdir()
        os.environ["FAKE_STATE"] = str(self.fs)
        os.environ["FAKE_SCENARIO"] = str(self.tmp / "scn.json")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_STATE", "FAKE_SCENARIO")])
        self.ad = FakeAdapter(AgentCfg("a", "manager", "fake"))

    def scenario(self, *steps):
        (self.tmp / "scn.json").write_text(json.dumps({"a": list(steps)}), encoding="utf-8")

    def run_turn(self, **kw):
        saved = []
        base = dict(agent_id="a", prompt="hello", cwd=str(self.tmp), session_id=None, timeout=30)
        base.update(kw)
        res = self.ad.run(TurnRequest(**base), threading.Event(), saved.append)
        return res, saved

    def calls(self):
        return [json.loads(l) for l in (self.fs / "calls.jsonl").read_text(encoding="utf-8").splitlines()]

    def session_file(self, sid):
        return self.fs / "sessions" / (sid + ".json")

    R = {"reply": {"say": "ok", "actions": []}}

    def test_fork_gets_a_new_id_leaves_the_original_untouched_and_later_turns_resume_the_fork(self):
        self.scenario(self.R, self.R, self.R)
        r1, saved1 = self.run_turn()
        orig = saved1[0]
        before = self.session_file(orig).read_bytes()
        r2, saved2 = self.run_turn(session_id=orig, fork=True)
        self.assertTrue(r2.ok, r2.error)
        self.assertEqual(len(saved2), 1)
        new = saved2[0]
        self.assertNotEqual(new, orig)                                         # new session id, handed to the engine
        self.assertEqual(r2.session_id, new)
        self.assertIn("--fork-session", self.calls()[1]["argv"])
        self.assertEqual(self.session_file(orig).read_bytes(), before)         # original session file byte-identical
        self.assertEqual(json.loads(self.session_file(new).read_text(encoding="utf-8"))["forked_from"], orig)
        r3, saved3 = self.run_turn(session_id=new)                             # a normal resume of the fork
        self.assertTrue(r3.ok, r3.error)
        self.assertEqual(saved3, [])
        self.assertNotIn("--fork-session", self.calls()[2]["argv"])
        self.assertEqual(self.session_file(orig).read_bytes(), before)

    def test_fork_that_stays_on_the_original_id_is_rejected_and_nothing_is_saved(self):
        self.scenario(self.R, dict(self.R, fork_keeps_id=True))
        _, saved1 = self.run_turn()
        r2, saved2 = self.run_turn(session_id=saved1[0], fork=True)
        self.assertFalse(r2.ok)
        self.assertEqual(r2.error_kind, "fork_failed")
        self.assertEqual(saved2, [])
        self.assertIsNone(r2.session_id)

    def test_fork_accepts_hook_events_that_still_carry_the_original_id(self):
        req = TurnRequest("a", "x", ".", "ORIG", schema={"type": "object"}, fork=True)
        res, seen = claude_parse([ev(type="system", subtype="hook_started", session_id="ORIG"),
                                  ev(type="system", subtype="init", session_id="NEW", model="m"),
                                  ev(type="result", subtype="success", is_error=False, session_id="NEW", result="r",
                                     structured_output={"say": "", "actions": []})], req)
        self.assertTrue(res.ok)
        self.assertEqual((seen, res.session_id), (["NEW"], "NEW"))

    def test_fork_with_two_different_new_ids_is_refused(self):
        req = TurnRequest("a", "x", ".", "ORIG", schema={"type": "object"}, fork=True)
        res, seen = claude_parse([ev(type="system", subtype="init", session_id="N1"),
                                  ev(type="system", subtype="status", session_id="N2"), OK_RESULT], req)
        self.assertEqual(res.error_kind, "session_lost")

    def test_plain_resume_rejects_an_unexpected_id_and_never_reports_it_to_the_engine(self):
        req = TurnRequest("a", "x", ".", "A", schema={"type": "object"})
        res, seen = claude_parse([ev(type="system", subtype="init", session_id="B"),
                                  ev(type="result", subtype="success", is_error=False, session_id="B", result="r",
                                     structured_output={"say": "", "actions": []})], req)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, "session_lost")
        self.assertEqual(seen, [])                                             # the engine never saw "B"
        self.assertEqual(res.session_id, "A")

    def test_plain_resume_with_the_same_id_is_fine_and_a_first_turn_adopts_its_id(self):
        res, seen = claude_parse([ev(type="system", subtype="init", session_id="A"), OK_RESULT.replace("S1", "A")],
                                 TurnRequest("a", "x", ".", "A", schema={"type": "object"}))
        self.assertTrue(res.ok)
        res, seen = claude_parse([ev(type="system", subtype="init", session_id="NEW"), OK_RESULT.replace("S1", "NEW")])
        self.assertEqual(seen, ["NEW"])

    def test_engine_keeps_the_saved_session_id_when_the_cli_answers_with_another_one(self):
        r = Rig({"manager": [M([final("a")])], "w1": []})
        self.addCleanup(r.close)
        r.say("1")
        saved = r.engine.mgr.session_id
        # simulate a CLI that moves a resumed session to another id: the parser must not report it
        req = TurnRequest("manager", "x", ".", saved, schema=protocol.MANAGER_SCHEMA)
        res, seen = claude_parse([ev(type="system", subtype="init", session_id="other"), OK_RESULT], req)
        self.assertEqual((res.error_kind, seen), ("session_lost", []))
        self.assertEqual(r.engine.mgr.session_id, saved)

    def test_no_dangerous_or_permission_changing_flag_is_ever_built(self):
        for ad, name in ((ClaudeAdapter(AgentCfg("a", "manager", "claude")), "claude"),
                         (AgyAdapter(AgentCfg("a", "worker", "agy")), "agy")):
            for kw in (dict(session_id=None), dict(session_id="X"), dict(session_id="X", model="m", effort="high")):
                inv = ad.build(TurnRequest("a", "p", ".", schema=protocol.MANAGER_SCHEMA, **kw), ["x"])
                self.assertFalse(FORBIDDEN & set(inv.args), (name, inv.args))
        inv = ClaudeAdapter(AgentCfg("a", "manager", "claude")).build(
            TurnRequest("a", "p", ".", "X", fork=True, schema=protocol.MANAGER_SCHEMA), ["x"])
        self.assertFalse(FORBIDDEN & set(inv.args))
        self.assertEqual(inv.args[inv.args.index("--resume") + 1], "X")
        self.assertIn("--fork-session", inv.args)
        self.assertNotIn("--fork-session", ClaudeAdapter(AgentCfg("a", "manager", "claude")).build(
            TurnRequest("a", "p", ".", "X"), ["x"]).args)

    def test_agy_cannot_fork(self):
        with self.assertRaises(ValueError):
            AgyAdapter(AgentCfg("a", "worker", "agy")).build(TurnRequest("a", "p", ".", "X", fork=True), ["x"])

    def test_fork_arguments_are_safe_for_a_cmd_shim(self):
        inv = ClaudeAdapter(AgentCfg("a", "manager", "claude")).build(TurnRequest("a", "p", ".", "11111111-2222", fork=True), ["x"])
        runner.check_shim_args(inv.args)                                       # no character cmd.exe would reinterpret


if __name__ == "__main__":
    unittest.main()
