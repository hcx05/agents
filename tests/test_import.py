"""Native Claude session import: fork-on-first-message, strict ids, crash recovery, state v3, compaction. Fake CLI only.
Nothing here talks to a real Claude; the real-CLI behaviour is checked on Windows (see README)."""
import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import importer, resume  # noqa: E402
from agent.config import parse_config  # noqa: E402
from agent.orchestrator import Engine, State  # noqa: E402
from agent.store import STATE_VERSION, Store, StoreError  # noqa: E402
from agent.ui import BufferUI  # noqa: E402
from tests.rig import Rig, M, W, dispatch, final  # noqa: E402
from tests.test_models_fork import FORBIDDEN  # noqa: E402

A = "aaaaaaaa-1111-4222-8333-444444444444"


class Base(unittest.TestCase):
    rig: Rig

    def tearDown(self):
        if getattr(self, "rig", None):
            self.rig.close()

    def mk(self, scenario, **kw):
        self.rig = Rig(scenario, import_source=A, **kw)
        return self.rig

    def flag(self, name):
        (self.rig.cwd / name).write_text("go", encoding="utf-8")

    def disk(self):
        return json.loads((self.rig.store.dir / "state.json").read_text(encoding="utf-8"))

    def imp(self):
        return self.rig.engine.imp

    def mgr_calls(self):
        return [c for c in self.rig.calls("manager") if "rejected" not in c]

    def assert_original_only_forked(self):
        """The ORIGINAL id may appear exactly once, as the source of --fork-session. Never as a working session."""
        r = self.rig
        for c in r.calls():
            argv = c["argv"]
            if "--resume" in argv and argv[argv.index("--resume") + 1] == A:
                self.assertIn("--fork-session", argv, argv)
                self.assertIn("--session-id", argv, argv)
            self.assertFalse(FORBIDDEN & set(argv), argv)
            if c["agent"] != "manager":
                self.assertNotIn(A, argv)
        self.assertEqual(json.loads(r.src_file.read_text(encoding="utf-8")), {"turns": 3, "agent": "native"})
        self.assertNotEqual(r.engine.mgr.session_id, A)


class TestFlow(Base):
    def test_nothing_is_called_and_the_fork_id_is_on_disk_before_the_first_message(self):
        r = self.mk({"manager": [M([final("hi")])], "w1": []})
        self.assertEqual(r.calls(), [])
        st = self.disk()
        self.assertEqual(st["version"], STATE_VERSION)
        self.assertEqual(STATE_VERSION, 3)
        self.assertEqual((st["import"]["source_id"], st["import"]["stage"]), (A, "prepared"))
        self.assertTrue(importer.is_claude_id(st["import"]["fork_id"]))
        self.assertIsNone(st["agents"]["manager"]["session_id"])

    def test_first_message_forks_with_the_chosen_id_and_later_turns_resume_only_the_fork(self):
        r = self.mk({"manager": [M([final("你好")]), M([final("第二")])], "w1": []})
        r.say("第一則")
        b = self.imp()["fork_id"]
        c1 = self.mgr_calls()[0]
        a = c1["argv"]
        self.assertEqual(a[a.index("--resume") + 1], A)
        self.assertIn("--fork-session", a)
        self.assertEqual(a[a.index("--session-id") + 1], b)
        self.assertEqual(c1["session"], b)
        self.assertIn("You are the MANAGER", c1["prompt"])
        self.assertIn("IMPORTED CONVERSATION", c1["prompt"])
        self.assertIn("[from user]\n第一則", c1["prompt"])
        st = self.disk()
        self.assertEqual((st["import"]["stage"], st["agents"]["manager"]["session_id"], st["agents"]["manager"]["preamble_sent"]),
                         ("ready", b, True))
        r.say("第二則")
        a2 = self.mgr_calls()[1]["argv"]
        self.assertEqual(a2[a2.index("--resume") + 1], b)
        self.assertNotIn("--fork-session", a2)
        self.assertNotIn("--session-id", a2)
        self.assertNotIn("You are the MANAGER", self.mgr_calls()[1]["prompt"])      # preamble only once
        self.assertNotIn("IMPORTED CONVERSATION", self.mgr_calls()[1]["prompt"])
        self.assert_original_only_forked()

    def test_manager_still_chooses_to_dispatch_and_worker_results_return_to_the_manager(self):
        r = self.mk({"manager": [M([dispatch("w1", "t1", "查 logs")]), M([final("整理完成")])],
                     "w1": [W("done", "t1", "三項異常")]})
        r.say("用員工幫我查")
        self.assertEqual(r.engine.state, State.DONE)
        self.assertIn("Manager> @Me 整理完成", r.chat_lines())
        self.assertIn("Manager> @w1 查 logs", r.chat_lines())
        self.assertIn("三項異常", self.mgr_calls()[1]["prompt"])
        self.assertEqual(len(self.mgr_calls()), 2)
        wa = r.calls("w1")[0]["argv"]
        self.assertNotIn("--fork-session", wa)
        self.assertEqual(wa.count("--resume"), 0)                      # the worker starts its own, separate session
        self.assert_original_only_forked()

    def test_manager_may_also_answer_alone(self):
        r = self.mk({"manager": [M([final("直接回答")])], "w1": []})
        r.say("簡單問題")
        self.assertEqual(r.calls("w1"), [])
        self.assertEqual(r.engine.state, State.DONE)

    def test_exit_then_resume_keeps_the_fork_and_never_forks_again(self):
        r = self.mk({"manager": [M([final("一")]), M([final("二")])], "w1": []})
        r.say("甲")
        b = self.imp()["fork_id"]
        r.resume()
        self.assertEqual(r.engine.mgr.session_id, b)
        self.assertEqual(r.engine.imp["stage"], "ready")
        r.say("乙")
        a = self.mgr_calls()[-1]["argv"]
        self.assertEqual(a[a.index("--resume") + 1], b)
        self.assertNotIn("--fork-session", a)
        self.assertEqual([c["argv"].count("--fork-session") for c in self.mgr_calls()], [1, 0])
        self.assert_original_only_forked()

    def test_resume_before_any_message_still_forks_on_the_first_one(self):
        r = self.mk({"manager": [M([final("一")])], "w1": []})
        b = self.imp()["fork_id"]
        r.resume()
        self.assertEqual(r.calls(), [])
        self.assertIn("尚未 Fork", "\n".join(t for _, t in r.engine.restore_report))
        r.say("開始")
        a = self.mgr_calls()[0]["argv"]
        self.assertEqual((a[a.index("--resume") + 1], a[a.index("--session-id") + 1]), (A, b))

    def test_status_shows_the_import_record(self):
        r = self.mk({"manager": [M([final("x")])], "w1": []})
        t = r.engine.status_text()
        self.assertIn(f"source={A}", t)
        self.assertIn(self.imp()["fork_id"], t)
        self.assertIn("stage=prepared", t)

    def test_plain_sessions_are_untouched_by_the_import_code(self):
        self.rig = r = Rig({"manager": [M([final("a")]), M([final("b")])], "w1": []})
        r.say("x")
        r.say("y")
        for c in r.calls("manager"):
            self.assertNotIn("--fork-session", c["argv"])
            self.assertNotIn("IMPORTED CONVERSATION", c["prompt"])
        self.assertNotIn("import", self.disk())


class TestPersistBeforeCli(Base):
    def test_state_on_disk_while_the_fork_request_is_running_already_has_the_fork_id(self):
        r = self.mk({"manager": [M([final("x")], wait_for_file="gate")], "w1": []})
        b = self.imp()["fork_id"]
        r.engine.submit("go")
        self.assertTrue(r.wait_until(lambda: r.engine.agents["manager"].running is not None))
        self.assertTrue(r.wait_until(lambda: len(r.calls()) == 1))
        st = self.disk()
        self.assertEqual((st["import"]["fork_id"], st["import"]["stage"], st["import"]["attempts"]), (b, "forking", 1))
        self.assertIsNone(st["agents"]["manager"]["session_id"])      # not adopted until the fork is confirmed
        self.flag("gate")
        self.assertTrue(r.engine.wait_idle(30))
        self.assertEqual(self.disk()["import"]["stage"], "ready")


class TestCrashAndFailure(Base):
    def crash_in_fork(self, steps):
        r = self.mk({"manager": steps, "w1": []})
        r.engine.submit("第一則")
        self.assertTrue(r.wait_until(lambda: len(r.calls()) == 1))
        crash = r.crash_copy()
        r.engine.shutdown()
        r.resume(crash)
        return r

    def test_crash_during_fork_is_reported_as_unknown_and_nothing_restarts_by_itself(self):
        r = self.crash_in_fork([M([final("x")], wait_for_file="gate"), M([final("恢復後")])])
        e = r.engine
        text = "\n".join(t for _, t in e.restore_report)
        self.assertIn("結果不明", text)
        self.assertIn("第一則", text)                                   # the lost message is listed
        self.assertEqual((e.imp["stage"], e.mgr.session_id, e.mgr.preamble_sent), ("forking", None, False))
        self.assertEqual(len(r.calls()), 1)                            # no automatic re-run
        self.assertEqual(e.imp["fork_id"], json.loads((r.store.dir / "state.json").read_text("utf-8"))["import"]["fork_id"])

    def test_after_a_crash_the_next_message_reuses_the_same_fork_id_and_continues_in_the_existing_fork(self):
        r = self.crash_in_fork([M([final("x")], wait_for_file="gate"), M([final("恢復後")])])
        e = r.engine
        b = e.imp["fork_id"]
        e.submit("重新送出")
        self.assertTrue(e.wait_idle(30))
        rej = [c for c in r.calls() if "rejected" in c]
        self.assertEqual(len(rej), 1)                                  # the CLI said "already in use": no second fork was made
        self.assertIn("already in use", rej[0]["rejected"])
        a = rej[0]["argv"]
        self.assertEqual((a[a.index("--resume") + 1], a[a.index("--session-id") + 1]), (A, b))   # same id, same source
        self.assertEqual(e.imp["stage"], "forked")
        self.assertTrue(e.hold)                                        # explicit: the message waits for /continue
        self.assertEqual([m["text"] for m in e.mgr.inbox if m["kind"] == "user"], ["重新送出"])
        self.assertEqual(e.imp["fork_id"], b)
        e.release()
        self.assertTrue(e.wait_idle(30))
        last = self.mgr_calls()[-1]
        a = last["argv"]
        self.assertEqual(a[a.index("--resume") + 1], b)
        self.assertNotIn("--fork-session", a)
        self.assertIn("You are the MANAGER", last["prompt"])           # this Manager never had a confirmed preamble
        self.assertIn("IMPORTED CONVERSATION", last["prompt"])
        self.assertIn("UNKNOWN", last["prompt"])                       # and it is told the earlier turn's fate is unknown
        self.assertEqual((e.imp["stage"], e.mgr.session_id), ("ready", b))
        self.assertEqual(e.state, State.DONE)
        self.assert_original_only_forked()
        sessions = sorted(p.stem for p in (r.fake_state / "sessions").glob("*.json") if p.stem not in ("x",))
        self.assertIn(b, sessions)
        self.assertEqual(len([s for s in sessions if s != A and s != b]), 0)     # no other Manager session was ever created

    def test_crash_after_a_successful_first_turn_but_before_it_was_saved_recovers_the_same_way(self):
        r = self.mk({"manager": [M([final("ok")]), M([final("later")])], "w1": []})
        b = self.imp()["fork_id"]
        r.say("一")
        r.engine.shutdown()                                             # (a shutdown saves; edit the file afterwards)
        saved = r.store.dir / "state.json"
        st = json.loads(saved.read_text("utf-8"))
        st["import"].update(stage="forking")                            # what the disk would hold if the save never happened
        st["agents"]["manager"].update(session_id=None, preamble_sent=False)
        st["mail"] = {"agents": {"manager": {"inflight": [{"kind": "user", "text": "一"}]}}}
        saved.write_text(json.dumps(st), encoding="utf-8")
        r.resume()
        r.say("二")
        self.assertTrue(r.engine.hold)
        r.engine.release()
        self.assertTrue(r.engine.wait_idle(30))
        self.assertEqual((r.engine.imp["fork_id"], r.engine.imp["stage"]), (b, "ready"))
        self.assert_original_only_forked()

    def test_failed_fork_before_the_copy_exists_is_retried_with_the_same_id_and_never_blank(self):
        r = self.mk({"manager": [M([final("ok")])], "w1": []})
        b = self.imp()["fork_id"]
        r.src_file.rename(r.src_file.with_suffix(".away"))             # the CLI cannot find the original
        r.say("一")
        self.assertEqual(r.engine.state, State.FAILED)
        errors = " ".join(r.ui.texts("error"))
        self.assertIn("session_lost", errors)
        self.assertIn(b, errors)                                       # tells the user the id is kept
        self.assertEqual((r.engine.imp["stage"], r.engine.mgr.session_id, r.engine.imp["fork_id"]), ("forking", None, b))
        self.assertEqual(r.calls(), [])
        r.src_file.with_suffix(".away").rename(r.src_file)
        r.say("再試一次")
        a = self.mgr_calls()[0]["argv"]
        self.assertEqual(a[a.index("--session-id") + 1], b)
        self.assertEqual(r.engine.imp["stage"], "ready")
        for c in r.calls():
            self.assertIn("--resume", c["argv"])                        # never a fresh, history-less session

    def test_a_cli_that_ignores_the_requested_fork_id_fails_and_is_not_accepted(self):
        r = self.mk({"manager": [M([final("ok")], fork_ignores_session_id=True)], "w1": []})
        b = self.imp()["fork_id"]
        r.say("一")
        self.assertEqual(r.engine.state, State.FAILED)
        self.assertIn("fork_failed", " ".join(r.ui.texts("error")))
        self.assertEqual((r.engine.mgr.session_id, r.engine.imp["stage"], r.engine.imp["fork_id"]), (None, "forking", b))
        self.assertFalse(r.engine.mgr.preamble_sent)
        self.assertNotIn(b, [p.stem for p in (r.fake_state / "sessions").glob("*.json")])

    def test_a_fork_id_that_existed_before_we_ever_tried_is_never_adopted(self):
        r = self.mk({"manager": [M([final("ok")])], "w1": []})
        b = self.imp()["fork_id"]
        (r.fake_state / "sessions" / f"{b}.json").write_text(json.dumps({"turns": 9, "agent": "stranger"}), encoding="utf-8")
        r.say("一")
        self.assertEqual(r.engine.imp["stage"], "blocked")
        self.assertIn("fork_exists", " ".join(r.ui.texts("error")))
        n = len(r.calls())
        r.say("再試")
        self.assertEqual(len(r.calls()), n)                            # blocked: no further CLI call, no blank Manager
        self.assertIn("無法安全繼續", " ".join(r.ui.texts("error")))
        self.assertEqual(json.loads((r.fake_state / "sessions" / f"{b}.json").read_text("utf-8"))["turns"], 9)

    def test_interrupt_while_forking_leaves_an_unknown_outcome_and_keeps_the_id(self):
        r = self.mk({"manager": [M([final("x")], wait_for_file="gate"), M([final("ok")])], "w1": []})
        b = self.imp()["fork_id"]
        r.engine.submit("一")
        self.assertTrue(r.wait_until(lambda: len(r.calls()) == 1))
        r.engine.interrupt()
        self.assertTrue(r.engine.wait_idle(30))
        self.assertEqual((r.engine.imp["stage"], r.engine.imp["fork_id"], r.engine.mgr.session_id), ("forking", b, None))

    def test_timeout_login_failure_and_format_change_keep_the_import_recoverable(self):
        for step, kind in ((M([final("x")], api_retry_error="authentication_failed", is_error="Not logged in · Please run /login"), "auth_error"),
                           ({"raw_stdout": ["{\"type\": \"mystery\"}"], "exit": 0}, "unknown_format")):
            self.rig = None
            r = self.mk({"manager": [step], "w1": []})
            b = self.imp()["fork_id"]
            r.say("一")
            self.assertEqual(r.engine.state, State.FAILED)
            self.assertIn(kind, " ".join(r.ui.texts("error")), kind)
            self.assertEqual((r.engine.imp["fork_id"], r.engine.mgr.session_id), (b, None))
            self.assertNotIn("--dangerously-skip-permissions", r.calls()[0]["argv"])
            r.close()
            self.rig = None

    def test_a_refused_tool_is_reported_and_no_permission_flag_is_ever_added(self):
        r = self.mk({"manager": [M([final("完成")], denial=[{"tool_name": "Bash", "tool_input": {"command": "rm x"}}])], "w1": []})
        r.say("做事")
        self.assertTrue(any("refused tool call" in t for t in r.ui.texts("system")))
        self.assertIn("does not change CLI permissions", " ".join(r.ui.texts("system")))
        self.assert_original_only_forked()


class TestStateVersions(Base):
    def test_v1_and_v2_plain_sessions_resume_and_are_saved_as_v3_without_an_import_record(self):
        for ver in (1, 2):
            self.rig = r = Rig({"manager": [M([final("a")]), M([final("b")])], "w1": []})
            r.say("x")
            st = self.disk()
            st["version"] = ver
            if ver == 1:
                for a in st["agents"].values():
                    a.pop("preamble_sent", None)
                st.pop("mail", None)
                st.pop("config", None)
            (r.store.dir / "state.json").write_text(json.dumps(st), encoding="utf-8")
            r.resume()
            r.say("y")
            self.assertEqual(self.disk()["version"], 3, ver)
            self.assertNotIn("import", self.disk())
            self.assertNotIn("--fork-session", r.calls("manager")[-1]["argv"])
            self.assertNotIn("IMPORTED", r.calls("manager")[-1]["prompt"])
            r.close()
            self.rig = None

    def test_a_v3_import_record_survives_resume_and_saving(self):
        r = self.mk({"manager": [M([final("a")]), M([final("b")])], "w1": []})
        r.say("x")
        before = dict(self.disk()["import"])
        r.resume()
        r.say("y")
        after = self.disk()
        self.assertEqual(after["version"], 3)
        self.assertEqual({k: after["import"][k] for k in ("source_id", "fork_id", "stage")},
                         {k: before[k] for k in ("source_id", "fork_id", "stage")})

    def test_a_program_that_only_knows_v2_refuses_a_v3_session(self):
        r = self.mk({"manager": [M([final("a")])], "w1": []})
        r.engine.shutdown()
        with mock.patch("agent.store.STATE_VERSION", 2):
            with self.assertRaises(StoreError) as cm:
                Store.open_existing(r.store.dir.parent, r.store.id)
        self.assertIn("newer version", str(cm.exception))

    def test_summaries_flag_imported_sessions(self):
        r = self.mk({"manager": [M([final("a")])], "w1": []})
        rows = Store.summaries(r.store.dir.parent)
        self.assertEqual(rows[0]["import_source"], A)
        self.assertIn("[import aaaaaaaa]", resume.format_sessions(rows))


class TestWindowsLaunchers(unittest.TestCase):
    def test_the_fork_command_is_safe_for_a_cmd_shim_and_carries_no_permission_flag(self):
        from agent import runner
        from agent.adapters.base import TurnRequest
        from agent.adapters.claude import ClaudeAdapter
        from agent.config import AgentCfg
        from agent import protocol
        new = str(uuid.uuid4())
        with mock.patch.object(runner, "IS_WINDOWS", True):              # behave as on Windows
            inv = ClaudeAdapter(AgentCfg("m", "manager", "claude")).build(
                TurnRequest("m", "p", "C:\\Users\\測 試\\proj dir", A, schema=protocol.MANAGER_SCHEMA, fork=True,
                            new_session_id=new), ["C:\\Tools\\claude.cmd"])
            self.assertTrue(runner.is_cmd_shim(["C:\\Tools\\claude.cmd"]))
        runner.check_shim_args(inv.args)                                  # no character cmd.exe would reinterpret
        self.assertFalse(FORBIDDEN & set(inv.args))
        self.assertIn("prompt-json", inv.note)                           # .cmd: schema goes in the prompt, validated by us
        self.assertEqual(inv.args[inv.args.index("--session-id") + 1], new)


class TestRecordValidation(Base):
    def good(self):
        return {"source_id": A, "fork_id": str(uuid.uuid4()), "stage": "ready"}

    def test_validate_import_state(self):
        self.assertEqual(importer.validate_import_state(self.good()), [])
        for patch, needle in (({"source_id": "nope"}, "source_id"), ({"fork_id": "nope"}, "fork_id"),
                              ({"fork_id": A}, "equals the source"), ({"stage": "weird"}, "stage")):
            self.assertTrue(any(needle in p for p in importer.validate_import_state(dict(self.good(), **patch))), patch)
        self.assertTrue(importer.validate_import_state("x"))

    def test_the_original_id_as_the_managers_working_session_is_refused(self):
        imp = self.good()
        self.assertTrue(any("ORIGINAL" in p for p in importer.validate_import_state(imp, A)))
        self.assertTrue(any("not the saved fork" in p for p in importer.validate_import_state(imp, str(uuid.uuid4()))))
        self.assertEqual(importer.validate_import_state(imp, imp["fork_id"]), [])

    def test_resume_refuses_a_state_whose_manager_session_is_the_original(self):
        r = self.mk({"manager": [M([final("a")])], "w1": []})
        r.say("x")
        r.engine.shutdown()
        st = self.disk()
        st["agents"]["manager"]["session_id"] = A
        (r.store.dir / "state.json").write_text(json.dumps(st), encoding="utf-8")
        store = Store.open_existing(r.store.dir.parent, r.store.id)
        errors, _ = resume.check_compat(r.cfg, store.state, str(r.cwd))
        self.assertTrue(any("ORIGINAL" in e for e in errors), errors)
        with self.assertRaises(ValueError):
            Engine(r.cfg, store, BufferUI(), str(r.cwd), resume=True)

    def test_resume_refuses_an_import_whose_manager_cli_is_not_claude_family(self):
        r = self.mk({"manager": [M([final("a")])], "w1": []})
        r.engine.shutdown()
        store = Store.open_existing(r.store.dir.parent, r.store.id)
        cfg = parse_config({"manager": "manager", "agents": {
            "manager": {"role": "manager", "cli": "agy"}, "w1": {"role": "worker", "cli": "agy"}}})
        errors, _ = resume.check_compat(cfg, store.state, str(r.cwd))
        self.assertTrue(any("must run on claude" in e for e in errors), errors)


class TestCompaction(Base):
    def test_after_a_compaction_the_team_rules_are_repeated_once_and_validation_stays_on(self):
        r = self.mk({"manager": [M([final("一")], compact=True), M([final("二")]), M([final("三")])], "w1": []})
        r.say("甲")
        self.assertTrue(r.engine.mgr.reinject)
        self.assertTrue(self.disk()["agents"]["manager"]["reinject"])
        r.say("乙")
        p2 = self.mgr_calls()[1]["prompt"]
        self.assertIn("You are the MANAGER", p2)
        self.assertIn("compacted by Claude Code", p2)
        self.assertNotIn("IMPORTED CONVERSATION", p2)                  # the import notice is not repeated
        self.assertFalse(r.engine.mgr.reinject)
        r.say("丙")
        self.assertNotIn("You are the MANAGER", self.mgr_calls()[2]["prompt"])
        self.assertTrue(any("壓縮" in t for t in r.ui.texts("system")))

    def test_a_reply_that_breaks_the_format_after_compaction_is_still_rejected_by_the_program(self):
        r = self.mk({"manager": [M([final("一")], compact=True), {"text": "I am just a normal assistant, no JSON"},
                                 M([final("修正")])], "w1": []})
        r.say("甲")
        r.say("乙")
        self.assertTrue(any("rejected" in t for t in r.ui.texts("system")))
        self.assertEqual(r.chat_lines()[-1], "Manager> @Me 修正")

    def test_reinject_flag_survives_a_restart(self):
        r = self.mk({"manager": [M([final("一")], compact=True), M([final("二")])], "w1": []})
        r.say("甲")
        r.resume()
        self.assertTrue(r.engine.mgr.reinject)
        r.say("乙")
        self.assertIn("You are the MANAGER", self.mgr_calls()[1]["prompt"])


if __name__ == "__main__":
    unittest.main()
