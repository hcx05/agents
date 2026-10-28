"""Chat-room activity hints: start lines, elapsed time, last-output time, silence notices, /status.
Time logic runs on a fake clock; ordering uses flag files, so there are no fixed sleeps."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import ConfigError, parse_config  # noqa: E402
from agent.orchestrator import State, fmt_elapsed, fmt_quiet  # noqa: E402
from tests.rig import FakeClock, Rig, M, W, ask, dispatch, final  # noqa: E402


def quiet_notices(r):
    return [t for t in r.ui.texts("system") if "沒有新輸出" in t]


class Base(unittest.TestCase):
    rig: Rig

    def tearDown(self):
        if getattr(self, "rig", None):
            self.rig.close()

    def flag(self, name):
        (self.rig.cwd / name).write_text("go", encoding="utf-8")

    def running(self, agent):
        return self.rig.wait_until(lambda: self.rig.engine.agents[agent].running is not None)

    def lines_seen(self, agent, n):
        return self.rig.wait_until(lambda: (self.rig.engine.agents[agent].running or object()).__dict__.get("lines", 0) >= n)


class TestStartLines(Base):
    def test_manager_alone_prints_start_line_once(self):
        self.rig = r = Rig({"manager": [M([final("好了")])], "w1": []})
        r.say("你好")
        sys_lines = r.ui.texts("system")
        self.assertEqual(sys_lines, ["Manager (fake) 開始執行。"])
        self.assertEqual(r.chat_lines(), ["Manager> @Me 好了"])

    def test_dispatch_flow_is_one_chronological_timeline(self):
        self.rig = r = Rig({"manager": [M([dispatch("w1", "t1", "分析")]), M([final("整理好了")])],
                            "w1": [W("done", "t1", "分析完成")]})
        r.say("幫我分析")
        flat = [(k, s, t) for k, s, t, _ in r.ui.lines]
        marks = [("system", "Manager (fake) 開始執行。"), ("chat", "分析"), ("system", "w1 (fake) 開始執行（任務 t1）。"),
                 ("chat", "分析完成"), ("system", "Manager 正在整理結果。"), ("chat", "整理好了")]
        idx = []
        for kind, text in marks:
            hits = [i for i, (k, _, t) in enumerate(flat) if k == kind and t == text]
            self.assertEqual(len(hits), 1, (kind, text, flat))
            idx.append(hits[0])
        self.assertEqual(idx, sorted(idx))
        self.assertEqual(r.engine.state, State.DONE)
        self.assertEqual(r.events("illegal_transition"), [])

    def test_answer_to_worker_question_announced(self):
        from tests.rig import answer
        self.rig = r = Rig({"manager": [M([dispatch("w1", "t1", "做")]), M([answer("w1", "t1", "選 A")]), M([final("OK")])],
                            "w1": [W("question", "t1", "A 還是 B？"), W("done", "t1", "完成")]})
        r.say("開始")
        self.assertIn("w1 (fake) 收到 Manager 的回答，繼續執行。", r.ui.texts("system"))

    def test_ask_user_answer_announced(self):
        self.rig = r = Rig({"manager": [M([ask("可以嗎？")]), M([final("好")])], "w1": []})
        r.say("做事")
        r.say("可以")
        self.assertIn("Manager 收到你的回答，繼續執行。", r.ui.texts("system"))


class TestElapsedAndActivity(Base):
    def test_elapsed_and_last_output_follow_the_clock(self):
        clk = FakeClock()
        self.rig = r = Rig({"manager": [M([final("ok")], wait_for_file="m.flag",
                                          emit_after_wait=['{"type":"stream_event"}'], wait_for_file2="m2.flag")],
                            "w1": []}, clock=clk)
        r.engine.submit("go")
        self.assertTrue(self.running("manager"))
        self.assertTrue(self.lines_seen("manager", 1))            # init line arrived at t=1000
        run = r.engine.agents["manager"].running
        clk.advance(30)
        st = r.engine.status_text()
        self.assertIn("running 30s", st)
        self.assertIn("last output 30s ago", st)
        before = run.lines
        self.flag("m.flag")                                        # CLI prints one more line
        self.assertTrue(r.wait_until(lambda: run.lines > before))
        self.assertEqual(run.last_output, 1030)                    # stamped with the current (fake) time
        clk.advance(5)
        self.assertIn("last output 5s ago", r.engine.status_text())
        self.assertIn("running 35s", r.engine.status_text())
        self.flag("m2.flag")
        self.assertTrue(r.engine.wait_idle(20))
        self.assertIn("manager (manager, fake) idle", r.engine.status_text())

    def test_time_helpers(self):
        self.assertEqual([fmt_elapsed(x) for x in (0, 59, 65, 3725)], ["0s", "59s", "1m05s", "1h02m05s"])
        self.assertEqual([fmt_quiet(x) for x in (60, 119, 121, 300)], ["60 秒", "119 秒", "2 分 01 秒", "5 分 00 秒"])


class TestSilenceNotice(Base):
    def setUp(self):
        self.clk = FakeClock()
        self.rig = r = Rig({"manager": [M([final("done")], wait_for_file="m.flag")], "w1": []}, clock=self.clk)
        r.engine.submit("go")
        self.assertTrue(self.running("manager"))
        self.assertTrue(self.lines_seen("manager", 1))
        self.run_ = r.engine.agents["manager"].running

    def tick(self, sec):
        self.clk.advance(sec)
        self.rig.engine.check_silence()

    def test_first_notice_at_60s_then_at_most_one_per_5min(self):
        r = self.rig
        self.tick(59)
        self.assertEqual(quiet_notices(r), [])
        self.tick(1)
        self.assertEqual(len(quiet_notices(r)), 1)
        for _ in range(20):                                       # polling again and again must not flood
            r.engine.check_silence()
        self.tick(100)
        self.tick(199)
        self.assertEqual(len(quiet_notices(r)), 1)
        self.tick(1)                                              # 300s after the first notice
        self.assertEqual(len(quiet_notices(r)), 2)
        text = quiet_notices(r)[0]
        self.assertIn("Manager 已經 60 秒沒有新輸出", text)
        self.assertIn("/status", text)
        self.assertIn("/interrupt", text)
        self.assertNotIn("思考", text)                              # never claims the model is thinking

    def test_new_output_resets_the_timer(self):
        r = self.rig
        self.tick(60)
        self.assertEqual(len(quiet_notices(r)), 1)
        self.run_.last_output = self.clk()                        # what the CLI reader does on a new line
        self.tick(59)
        self.assertEqual(len(quiet_notices(r)), 1)                # restarted: first-notice threshold applies again
        self.tick(1)
        self.assertEqual(len(quiet_notices(r)), 2)

    def test_notice_never_interrupts_or_changes_state(self):
        r = self.rig
        self.tick(600)
        self.assertGreaterEqual(len(quiet_notices(r)), 1)
        self.assertIsNotNone(r.engine.agents["manager"].running)
        self.assertEqual(r.engine.state, State.RUNNING)
        self.flag("m.flag")
        self.assertTrue(r.engine.wait_idle(20))
        self.assertEqual(r.engine.state, State.DONE)
        self.assertEqual(r.events("internal_error"), [])

    def test_interrupt_cancels_a_silent_turn(self):
        r = self.rig
        self.tick(120)
        r.engine.interrupt()
        self.assertTrue(r.engine.wait_idle(40))
        self.assertIsNone(r.engine.agents["manager"].running)
        self.assertIn("interrupted", " ".join(r.ui.texts("system")).lower())
        self.tick(600)                                            # nothing running -> nothing to warn about
        self.assertEqual(len(quiet_notices(r)), 1)

    def test_thresholds_are_configurable(self):
        clk = FakeClock()
        self.rig.close()
        self.rig = r = Rig({"manager": [M([final("x")], wait_for_file="m.flag")], "w1": []}, clock=clk,
                           cfg_extra={"silence_warn_sec": 10, "silence_repeat_sec": 20})
        r.engine.submit("go")
        self.assertTrue(self.running("manager"))
        clk.advance(10)
        r.engine.check_silence()
        clk.advance(19)
        r.engine.check_silence()
        self.assertEqual(len(quiet_notices(r)), 1)
        clk.advance(1)
        r.engine.check_silence()
        self.assertEqual(len(quiet_notices(r)), 2)

    def test_worker_silence_is_reported_with_its_own_name(self):
        clk = FakeClock()
        self.rig.close()
        self.rig = r = Rig({"manager": [M([dispatch("w1", "t1", "x")]), M([final("ok")])],
                            "w1": [W("done", "t1", "d", wait_for_file="w.flag")]}, clock=clk)
        r.engine.submit("go")
        self.assertTrue(self.running("w1"))
        clk.advance(61)
        r.engine.check_silence()
        notes = quiet_notices(r)
        self.assertEqual(len(notes), 1)
        self.assertIn("w1 已經", notes[0])
        self.flag("w.flag")
        self.assertTrue(r.engine.wait_idle(30))
        self.assertEqual(r.engine.state, State.DONE)


class TestIdleTick(Base):
    def test_engine_loop_itself_raises_the_notice_without_any_event(self):
        # real clock, tiny thresholds: the loop's idle tick (not a test call) must print the notice exactly once
        self.rig = r = Rig({"manager": [M([final("x")], wait_for_file="m.flag")], "w1": []},
                           cfg_extra={"silence_warn_sec": 0.2, "silence_repeat_sec": 3600}, watch_tick=0.05)
        r.engine.submit("go")
        self.assertTrue(r.wait_until(lambda: len(quiet_notices(r)) == 1, timeout=10))
        self.assertTrue(r.engine.wait_idle(0.5) is False)          # still running, not interrupted
        self.assertEqual(len(quiet_notices(r)), 1)
        self.flag("m.flag")
        self.assertTrue(r.engine.wait_idle(20))


class TestStatusText(Base):
    def test_status_shows_worker_task_and_waiting_target(self):
        clk = FakeClock()
        self.rig = r = Rig({"manager": [M([dispatch("w1", "t1", "x")]), M([final("ok")])],
                            "w1": [W("done", "t1", "d", wait_for_file="w.flag")]}, clock=clk)
        r.engine.submit("go")
        self.assertTrue(self.running("w1"))
        self.assertTrue(r.wait_until(lambda: r.engine.agents["manager"].running is None))
        clk.advance(42)
        st = r.engine.status_text()
        self.assertIn("w1 (worker, fake) running 42s", st)
        self.assertIn("task=t1", st)
        self.assertIn("manager (manager, fake) idle", st)
        self.assertIn("Manager is waiting for: w1", st)
        self.assertIn("open tasks: t1:", st)
        self.assertNotIn("%", st)                                 # no fake progress percentage
        self.flag("w.flag")
        self.assertTrue(r.engine.wait_idle(30))

    def test_status_waiting_for_user(self):
        self.rig = r = Rig({"manager": [M([ask("刪除 build？")])], "w1": []})
        r.say("清理")
        st = r.engine.status_text()
        self.assertIn("waiting for your answer to: 刪除 build？", st)
        self.assertIn("state: waiting_user", st)


class TestConfig(unittest.TestCase):
    BASE = {"manager": "m", "agents": {"m": {"role": "manager", "cli": "fake"}, "w": {"role": "worker", "cli": "fake"}}}

    def test_defaults_and_validation(self):
        c = parse_config(dict(self.BASE))
        self.assertEqual((c.silence_warn_sec, c.silence_repeat_sec), (60, 300))
        for bad in (0, -5, "60", True):
            with self.assertRaises(ConfigError):
                parse_config({**self.BASE, "silence_warn_sec": bad})


if __name__ == "__main__":
    unittest.main()
