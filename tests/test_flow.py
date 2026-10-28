import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import runner  # noqa: E402
from agent.orchestrator import State  # noqa: E402
from agent.store import Store  # noqa: E402
from tests.rig import Rig, M, W, answer, ask, dispatch, final  # noqa: E402


class FlowBase(unittest.TestCase):
    rig: Rig

    def tearDown(self):
        if getattr(self, "rig", None):
            self.rig.close()

    def assertNoIllegalTransitions(self):
        self.assertEqual(self.rig.events("illegal_transition"), [])
        self.assertEqual(self.rig.events("internal_error"), [])


class TestCoreLoop(FlowBase):
    def test_01_02_dispatch_and_done_then_final(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "請檢查 logs")], say="我先分析專案結構。"),
                        M([final("任務完成：發現三項異常。")])],
            "w1": [W("done", "t1", "已完成，發現三項異常。", ["logs/a.txt"])],
        })
        r.say("請叫員工分析這個資料夾，完成後整理結果給我。")
        chat = r.chat_lines()
        self.assertEqual(chat[0:4], [
            "Manager> 我先分析專案結構。",
            "Manager> @w1 請檢查 logs",
            "w1> @Manager 已完成，發現三項異常。",
            "Manager> @Me 任務完成：發現三項異常。"], chat)
        self.assertEqual(r.engine.state, State.DONE)
        self.assertEqual(r.store.state["tasks"]["t1"]["status"], "done")
        # worker report reached the manager's second turn
        mgr_calls = r.calls("manager")
        self.assertEqual(len(mgr_calls), 2)
        self.assertIn("[from w1 | task t1 | status done]", mgr_calls[1]["prompt"])
        self.assertTrue(mgr_calls[1]["resume"])
        self.assertNoIllegalTransitions()
        # transitions went through the documented path
        self.assertIn("running->waiting_worker", r.engine.transitions_log)
        self.assertIn("waiting_worker->waiting_manager", r.engine.transitions_log)
        self.assertEqual(r.engine.transitions_log[-1], "running->done")

    def test_03_worker_question_manager_answer_worker_done(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "請檢查 logs")]),
                        M([answer("w1", "t1", "請先檢查 logs/ 資料夾。")]),
                        M([final("完成。")])],
            "w1": [W("question", "t1", "請問 logs 在哪裡？"),
                   W("done", "t1", "已完成，發現三項異常。", expect_prompt_contains=["請先檢查 logs/ 資料夾。"], expect_turn=2)],
        })
        r.say("整理 logs")
        self.assertEqual(r.chat_lines(), [
            "Manager> @w1 請檢查 logs",
            "w1> @Manager 請問 logs 在哪裡？",
            "Manager> @w1 請先檢查 logs/ 資料夾。",
            "w1> @Manager 已完成，發現三項異常。",
            "Manager> @Me 完成。"])
        w = r.calls("w1")
        self.assertEqual([c["turn"] for c in w], [1, 2])
        self.assertEqual(w[0]["session"], w[1]["session"])           # same worker session resumed
        self.assertTrue(w[1]["resume"])
        self.assertEqual(r.engine.state, State.DONE)
        self.assertNoIllegalTransitions()

    def test_04_ask_user_pauses_and_answer_returns_to_manager_session(self):
        self.rig = r = Rig({
            "manager": [M([ask("可以刪除 build/ 嗎？")]),
                        M([final("已依你的許可處理。")])],
            "w1": [],
        })
        r.say("清理專案")
        self.assertEqual(r.engine.state, State.WAITING_USER)
        self.assertEqual(r.ui.texts("ask_user"), ["可以刪除 build/ 嗎？"])
        self.assertEqual(len(r.calls("manager")), 1)                 # manager paused: no extra turn
        r.say("可以")
        c = r.calls("manager")
        self.assertEqual(len(c), 2)
        self.assertEqual(c[0]["session"], c[1]["session"])
        self.assertIn("[user answer to your ask_user]\n可以", c[1]["prompt"])
        self.assertNotIn("[from user]\n可以", c[1]["prompt"])
        self.assertEqual(r.engine.state, State.DONE)
        self.assertIsNone(r.store.state["awaiting_user"])
        self.assertNoIllegalTransitions()

    def test_04b_worker_report_is_held_while_waiting_for_user(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "slow")]),   # then ask user while worker runs? -> via report
                        M([ask("worker 完成，要繼續嗎？")]),
                        M([final("OK")])],
            "w1": [W("done", "t1", "done")],
        })
        r.say("go")
        self.assertEqual(r.engine.state, State.WAITING_USER)
        r.say("繼續")
        self.assertEqual(r.engine.state, State.DONE)

    def test_05_invalid_json_gets_one_repair(self):
        self.rig = r = Rig({"manager": [{"text": "我不是 JSON"}, M([final("修正後的回覆")])], "w1": []})
        r.say("hi")
        self.assertIn("Manager> @Me 修正後的回覆", r.chat_lines())
        c = r.calls("manager")
        self.assertEqual(len(c), 2)
        self.assertIn("REJECTED", c[1]["prompt"])
        self.assertEqual(r.engine.state, State.DONE)

    def test_05b_invalid_json_twice_fails_then_recovers(self):
        self.rig = r = Rig({"manager": [{"text": "x"}, {"text": "still not json"}, M([final("恢復了")])], "w1": []})
        r.say("hi")
        self.assertEqual(r.engine.state, State.FAILED)
        self.assertTrue(any("invalid output" in t for t in r.ui.texts("error")))
        r.say("再試一次")                                              # system is operable again
        self.assertEqual(r.engine.state, State.DONE)
        self.assertIn("Manager> @Me 恢復了", r.chat_lines())
        self.assertIn("aborted", r.calls("manager")[-1]["prompt"])      # manager is told what happened
        self.assertNoIllegalTransitions()

    def test_06_invalid_action_type_rejected_with_schema_enum(self):
        bad = M([{"type": "delete_everything", "text": "x"}])
        self.rig = r = Rig({"manager": [bad, M([final("ok")])], "w1": []})
        r.say("hi")
        c = r.calls("manager")
        self.assertIn("not one of", c[1]["prompt"])
        self.assertEqual(r.engine.state, State.DONE)

    def test_07_nonexistent_worker_and_self_dispatch(self):
        self.rig = r = Rig({"manager": [M([dispatch("w9", "t1", "x")]),
                                       M([dispatch("manager", "t1", "x")]),
                                       M([final("ok")])], "w1": []})
        r.say("hi")
        self.assertEqual(r.engine.state, State.FAILED)              # 2nd invalid output -> no more repairs
        r.say("again")
        self.assertEqual(r.engine.state, State.DONE)
        self.assertIn("unknown worker 'w9'", r.calls("manager")[1]["prompt"])
        self.assertEqual(r.calls("w1"), [])                          # nothing reached the worker
        self.assertEqual(r.store.state["tasks"], {})

    def test_08_wrong_task_ids(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "work")]),
                        M([answer("w1", "nope", "?")]),          # unknown task id -> rejected, repaired
                        M([answer("w1", "t1", "ans")]),
                        M([final("done")])],
            "w1": [W("question", "t1", "q?"), W("done", "t1", "ok")],
        })
        r.say("go")
        self.assertIn("unknown task_id 'nope'", r.calls("manager")[2]["prompt"])
        self.assertEqual(r.engine.state, State.DONE)

    def test_08b_worker_wrong_task_id_is_rejected_and_repaired(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "work")]), M([final("done")])],
            "w1": [W("done", "t999", "wrong id"), W("done", "t1", "right id")],
        })
        r.say("go")
        w = r.calls("w1")
        self.assertEqual(len(w), 2)
        self.assertIn("does not match the active task 't1'", w[1]["prompt"])
        self.assertNotIn("wrong id", " ".join(r.chat_lines()))
        self.assertEqual(r.engine.state, State.DONE)

    def test_09_premature_final_is_refused(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "work")]),
                        M([final("我已完成！(其實 worker 還在問問題)")]),
                        M([answer("w1", "t1", "A")]),
                        M([final("真的完成")])],
            "w1": [W("question", "t1", "Q?"), W("done", "t1", "ok")],
        })
        r.say("go")
        chat = "\n".join(r.chat_lines())
        self.assertNotIn("我已完成", chat)
        self.assertIn("tasks still open", r.calls("manager")[2]["prompt"])
        self.assertEqual(r.chat_lines()[-1], "Manager> @Me 真的完成")

    def test_09b_final_mixed_with_dispatch_is_refused(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "work"), final("done already")]),
                        M([dispatch("w1", "t1", "work")]),
                        M([final("ok")])],
            "w1": [W("done", "t1", "ok")],
        })
        r.say("go")
        self.assertIn("final must be the only action", r.calls("manager")[1]["prompt"])
        self.assertEqual(r.engine.state, State.DONE)

    def test_09c_dispatch_to_busy_worker_refused(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "work")]),
                        M([dispatch("w1", "t2", "more")]),     # t1 is in 'question' -> w1 busy
                        M([answer("w1", "t1", "A")]),
                        M([final("ok")])],
            "w1": [W("question", "t1", "Q"), W("done", "t1", "ok")],
        })
        r.say("go")
        self.assertIn("still has open task", r.calls("manager")[2]["prompt"])

    def test_10_worker_blocked_by_permission_denial_overrides_done_claim(self):
        denial = [{"tool_name": "Write", "tool_input": {"file_path": "/x/out.txt"}}]
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "寫檔")]),
                        M([final("需要你授權 Write 權限；工作尚未完成。")])],
            "w1": [W("done", "t1", "我完成了！", denial=denial)],
        })
        r.say("go")
        self.assertEqual(r.store.state["tasks"]["t1"]["status"], "blocked")
        p = r.calls("manager")[1]["prompt"]
        self.assertIn("status blocked", p)
        self.assertIn("Write(/x/out.txt)", p)
        self.assertIn("Permissions were NOT changed", p)
        self.assertIn("blocked", " ".join(r.engine.transitions_log))
        self.assertNoIllegalTransitions()

    def test_10b_worker_reports_blocked(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "x")]), M([final("worker 被擋住")])],
            "w1": [W("blocked", "t1", "權限不足")],
        })
        r.say("go")
        self.assertEqual(r.store.state["tasks"]["t1"]["status"], "blocked")
        self.assertEqual(r.engine.state, State.DONE)


class TestFailures(FlowBase):
    def test_11_worker_timeout_is_reported_and_process_killed(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "slow")]), M([final("worker 逾時，沒有完成")])],
            "w1": [{"sleep": 30, "reply": {"status": "done", "task_id": "t1", "text": "late", "files_touched": []}}],
        }, timeout=1.5)
        t0 = time.time()
        r.say("go")
        self.assertLess(time.time() - t0, 20)
        self.assertEqual(r.store.state["tasks"]["t1"]["status"], "timeout")
        self.assertIn("[timeout]", r.calls("manager")[1]["prompt"])
        self.assertIn("waiting_worker->timeout", r.engine.transitions_log)
        self.assertEqual(r.engine.state, State.DONE)
        self.assertEqual(len(runner._ACTIVE), 0)

    def test_12_user_interrupt(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "long job")]), M([final("好的，已中斷後重新處理")])],
            "w1": [{"sleep": 60, "reply": {"status": "done", "task_id": "t1", "text": "never", "files_touched": []}}],
        })
        r.engine.submit("開始")
        self.assertTrue(r.wait_until(lambda: r.engine.agents["w1"].running is not None))
        t0 = time.time()
        r.engine.interrupt()
        self.assertTrue(r.engine.wait_idle(30), r.dump())
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(r.engine.state, State.INTERRUPTED)
        self.assertEqual(r.store.state["tasks"]["t1"]["status"], "interrupted")
        self.assertEqual(len(runner._ACTIVE), 0)                       # child really gone
        self.assertNotIn("never", " ".join(r.chat_lines()))
        r.say("繼續")                                                    # operable again
        self.assertEqual(r.engine.state, State.DONE)
        self.assertIn("INTERRUPTED", r.calls("manager")[-1]["prompt"])
        self.assertNoIllegalTransitions()

    def test_12b_interrupt_when_idle_is_harmless(self):
        self.rig = r = Rig({"manager": [], "w1": []})
        r.engine.interrupt()
        self.assertTrue(r.engine.wait_idle(5))
        self.assertEqual(r.engine.state, State.IDLE)
        self.assertIn("Nothing is running.", r.ui.texts("system"))

    def test_13_worker_nonzero_exit(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "x")]), M([final("worker 崩潰了")])],
            "w1": [{"exit": 3, "stderr": "boom: cli crashed"}],
        })
        r.say("go")
        self.assertEqual(r.store.state["tasks"]["t1"]["status"], "failed")
        p = r.calls("manager")[1]["prompt"]
        self.assertIn("status failed", p)
        self.assertIn("exit code 3", p)
        self.assertIn("boom: cli crashed", p)

    def test_13b_manager_nonzero_exit_then_recovers(self):
        self.rig = r = Rig({"manager": [{"exit": 5, "stderr": "mgr crash"}, M([final("back")])], "w1": []})
        r.say("hi")
        self.assertEqual(r.engine.state, State.FAILED)
        self.assertTrue(any("exit code 5" in t for t in r.ui.texts("error")))
        r.say("hi again")
        self.assertEqual(r.engine.state, State.DONE)

    def test_14_session_resume_and_lost_session_is_an_error_not_a_fresh_chat(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "x")]),
                        M([answer("w1", "t1", "A")], forget_sessions_of="w1"),   # worker session vanishes
                        M([final("worker 的 session 失效，無法續談")])],
            "w1": [W("question", "t1", "Q"), W("done", "t1", "should never run")],
        })
        r.say("go")
        w = r.calls("w1")
        self.assertEqual(len(w), 1)                                    # no silent fresh conversation
        self.assertEqual(r.store.state["tasks"]["t1"]["status"], "failed")
        self.assertIn("[session_lost]", r.calls("manager")[2]["prompt"])
        self.assertEqual(r.engine.state, State.DONE)

    def test_15_chinese_multiline_roundtrip(self):
        text = "第一行：中文\n第二行 \"引號\" & %PATH% ^ | <>\n  第三行（縮排） ✓"
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", text)], expect_prompt_contains=["使用者輸入：\n含有 \"引號\" 與 & 符號"]),
                        M([final("收到\n多行\n回覆")])],
            "w1": [W("done", "t1", "完成\n換行", expect_prompt_contains=["第三行（縮排） ✓", "%PATH% ^ | <>"])],
        })
        r.say("使用者輸入：\n含有 \"引號\" 與 & 符號")
        self.assertEqual(r.chat_lines()[-1], "Manager> @Me 收到\n多行\n回覆")
        self.assertEqual(r.engine.state, State.DONE)
        self.assertEqual(r.events("agent_error"), [])

    def test_17_paths_with_spaces_and_cjk(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "x")]), M([final("ok")])],
            "w1": [W("done", "t1", "ok")],
        })
        # work dir and temp/state dirs contain spaces + CJK: every CLI call below ran in them
        r.say("go")
        self.assertIn(" ", str(r.cwd))
        self.assertTrue(all(os.path.samefile(c["cwd"], r.cwd) for c in r.calls()))

    def test_18_cli_not_found_worker_and_manager(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "x")]), M([final("worker CLI 不存在")])],
            "w1": [],
        }, worker_command="definitely-not-a-real-cli-xyz")
        r.say("go")
        self.assertIn("command not found", r.calls("manager")[1]["prompt"])
        self.assertEqual(r.store.state["tasks"]["t1"]["status"], "failed")
        r.close()
        self.rig = r = Rig({"manager": [], "w1": []}, manager_command="definitely-not-a-real-cli-xyz")
        r.say("hi")
        self.assertEqual(r.engine.state, State.FAILED)
        self.assertTrue(any("command not found" in t for t in r.ui.texts("error")))

    def test_19_manager_turn_limit(self):
        steps = [M([dispatch("w1", "t1", "x")])] + [M([answer("w1", "t1", "again")])] * 10
        self.rig = r = Rig({"manager": steps, "w1": [W("question", "t1", "Q")] * 10}, max_turns=3)
        r.say("loop forever")
        self.assertEqual(r.engine.state, State.FAILED)
        self.assertTrue(any("turn limit" in t for t in r.ui.texts("error")))
        self.assertEqual(len(r.calls("manager")), 3)
        self.assertEqual(r.store.state["tasks"]["t1"]["status"], "cancelled")
        self.assertEqual(len(runner._ACTIVE), 0)

    def test_20_invalid_model_is_an_error_no_fallback(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "x", model="bad-model-9")]), M([final("worker 模型無效")])],
            "w1": [W("done", "t1", "would not run")],
        })
        r.say("go")
        p = r.calls("manager")[1]["prompt"]
        self.assertIn("[model_invalid]", p)
        self.assertIn("bad-model-9", p)
        self.assertEqual(r.calls("w1")[0]["model"], "bad-model-9")       # model override was passed through
        self.assertEqual(len(r.calls("w1")), 1)                          # not retried on another model

    def test_20b_default_model_from_config_and_override(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "x", model="m-override", effort="high")]), M([final("ok")])],
            "w1": [W("done", "t1", "ok")],
        }, worker_model="m-default")
        r.say("go")
        c = r.calls("w1")[0]
        self.assertEqual((c["model"], c["effort"]), ("m-override", "high"))
        self.assertIsNone(r.calls("manager")[0]["model"])

    def test_21_invalid_effort_is_reported(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "x", effort="ultra-mega")]), M([final("effort 無效")])],
            "w1": [W("done", "t1", "ok")],
        })
        r.say("go")
        self.assertIn("[effort_invalid]", r.calls("manager")[1]["prompt"])

    def test_22_file_change_snapshot_reaches_manager(self):
        target = "新檔案 report.txt"
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "x")]), M([final("ok")])],
            "w1": [W("done", "t1", "ok", files=["x"], write_file={"path": "PLACEHOLDER", "content": "hi"})],
        })
        # rewrite scenario with the real path (cwd known only after Rig creation)
        scen = json.loads((r.tmp / "scenario.json").read_text(encoding="utf-8"))
        scen["w1"][0]["write_file"]["path"] = str(r.cwd / target)
        (r.tmp / "scenario.json").write_text(json.dumps(scen, ensure_ascii=False), encoding="utf-8")
        r.say("go")
        p = r.calls("manager")[1]["prompt"]
        self.assertIn("observed file changes: +1", p)
        self.assertIn(target, p)
        self.assertIn("may not be caused by this agent", p)
        self.assertTrue((r.cwd / target).exists())                       # never touched/reverted


class TestSessionsAndStorage(FlowBase):
    def test_16_state_saved_and_loadable(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "x")]), M([final("ok")])],
            "w1": [W("done", "t1", "ok", files=["a.txt"])],
        })
        r.say("go")
        st = Store.load_state(r.store.dir)
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["tasks"]["t1"]["status"], "done")
        sids = {a: st["agents"][a]["session_id"] for a in ("manager", "w1")}
        self.assertTrue(all(sids.values()))
        self.assertNotEqual(sids["manager"], sids["w1"])                  # same CLI, different sessions
        self.assertEqual(sids["manager"], r.calls("manager")[0]["session"])
        self.assertFalse((r.store.dir / "state.json.tmp").exists())
        ev = r.events()
        self.assertTrue(any(e["type"] == "chat" and e["sender"] == "Me" for e in ev))
        self.assertTrue(any(e["type"] == "turn_end" for e in ev))
        self.assertTrue((r.store.dir / "raw" / "w1.jsonl").exists())
        self.assertIn(r.store.id, Store.list_sessions(r.store.dir.parent))

    def test_23_two_agents_same_cli_have_distinct_sessions_and_each_resumes_own(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "x")]), M([answer("w1", "t1", "A")]), M([final("ok")])],
            "w1": [W("question", "t1", "Q"), W("done", "t1", "ok")],
        })
        r.say("go")
        m, w = r.calls("manager"), r.calls("w1")
        self.assertEqual({c["session"] for c in m}, {m[0]["session"]})
        self.assertEqual({c["session"] for c in w}, {w[0]["session"]})
        self.assertNotEqual(m[0]["session"], w[0]["session"])

    def test_24_session_saved_before_turn_finishes(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "x")]), M([final("ok")])],
            "w1": [{"sleep": 3, "reply": {"status": "done", "task_id": "t1", "text": "ok", "files_touched": []}}],
        })
        r.engine.submit("go")
        self.assertTrue(r.wait_until(lambda: r.engine.agents["w1"].running is not None))
        self.assertTrue(r.wait_until(lambda: Store.load_state(r.store.dir)["agents"]["w1"]["session_id"]))
        self.assertTrue(r.engine.agents["w1"].running)                    # still running: id was saved early
        self.assertTrue(r.engine.wait_idle(30))

    def test_25_status_text(self):
        self.rig = r = Rig({"manager": [M([ask("sure?")])], "w1": []})
        r.say("go")
        s = r.engine.status_text()
        self.assertIn("state: waiting_user", s)
        self.assertIn("waiting for your answer", s)


if __name__ == "__main__":
    unittest.main()
