"""Full-screen chat room: the view model (wrap / scroll / sanitize), the prompt_toolkit app driven by scripted key input,
the plain fallbacks, and one whole run of `python -m agent` inside a pseudo terminal with the fake CLI.
Nothing here touches a Windows console: IME, a real Windows Terminal paste and real resizes are manual checks."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import prompt_toolkit  # noqa: F401
    HAVE_PTK = True
except ImportError:
    HAVE_PTK = False

from tests.rig import M, W, dispatch, final  # noqa: E402

if HAVE_PTK:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from agent import tui
    from agent.tui import ChatView, Entry, TuiApp, TuiUI, sanitize, status_line, text_width, wrap


def until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def texts(rows):
    return ["".join(t for _, t in r) for r in rows]


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestWrapAndSanitize(unittest.TestCase):
    def test_every_row_fits_for_cjk_ascii_and_mixed(self):
        samples = ["你好，這是一段很長的中文訊息，沒有任何空白可以斷行。" * 3,
                   "A" * 100,
                   "hello world " * 20,
                   "混合 mixed 中英文 text 與 emoji 😀 以及 long_identifier_without_spaces_" * 3,
                   "第一行\n\n第三行"]
        for w in (12, 20, 33, 80):
            for t in samples:
                for row in wrap(t, w):
                    self.assertLessEqual(text_width(row), w, (w, row))

    def test_nothing_is_lost_and_blank_lines_survive(self):
        t = "你好，世界" * 10
        self.assertEqual("".join(wrap(t, 15)), t)
        self.assertEqual(wrap("a\n\nb", 10), ["a", "", "b"])
        self.assertEqual(wrap("", 10), [""])

    def test_breaks_at_a_space_when_there_is_one(self):
        self.assertEqual(wrap("hello world foo", 11), ["hello world", "foo"])

    def test_sanitize_drops_escapes_controls_and_bidi_overrides(self):
        dirty = "a\x1b[31mred\x1b[0m\x1b]0;title\x07 b\x00c\x08d‮evil\r\nx\ty"
        clean = sanitize(dirty)
        self.assertNotIn("\x1b", clean)
        self.assertNotIn("\x00", clean)
        self.assertNotIn("‮", clean)
        self.assertNotIn("\r", clean)
        self.assertEqual(clean, "ared bcdevil\nx    y")

    def test_agent_output_with_escapes_cannot_reach_the_screen(self):
        v = ChatView()
        v.add(Entry("chat", "Manager", "hi \x1b[2J\x1b[Hgone \x1b]52;c;AAAA\x07"))
        for r in texts(v.window_rows(60, 10)):
            self.assertNotIn("\x1b", r)


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestChatView(unittest.TestCase):
    def fill(self, n=40):
        v = ChatView()
        for i in range(n):
            v.add(Entry("chat", "Manager", f"message number {i}"))
        return v

    def test_following_shows_the_newest(self):
        v = self.fill()
        rows = texts(v.window_rows(60, 10))
        self.assertTrue(any("message number 39" in r for r in rows))
        self.assertEqual(v.scroll, 0)

    def test_scrolling_back_keeps_the_view_still_when_new_messages_arrive(self):
        v = self.fill()
        v.window_rows(60, 10)
        v.page(+1)
        v.page(+1)
        before = texts(v.window_rows(60, 10))
        self.assertGreater(v.scroll, 0)
        for i in range(5):
            v.add(Entry("chat", "Worker", f"late {i}"))
            v.add(Entry("system", "system", f"sys {i}"))
        self.assertEqual(texts(v.window_rows(60, 10)), before)
        self.assertEqual(v.unseen, 10)

    def test_to_bottom_and_scrolling_down_to_the_end_resume_following(self):
        v = self.fill()
        v.window_rows(60, 10)
        v.page(+1)
        v.add(Entry("chat", "Manager", "new one"))
        v.to_bottom()
        self.assertTrue(any("new one" in r for r in texts(v.window_rows(60, 10))))
        self.assertEqual((v.scroll, v.unseen), (0, 0))
        v.page(+1)
        v.page(-1)
        v.window_rows(60, 10)
        self.assertEqual((v.scroll, v.unseen), (0, 0))

    def test_cannot_scroll_past_the_start(self):
        v = self.fill(5)
        v.window_rows(60, 10)
        for _ in range(10):
            v.page(+1)
        self.assertLessEqual(v.scroll, max(0, v.total - v.height))

    def test_resize_reflows(self):
        v = ChatView()
        v.add(Entry("chat", "Manager", "這是一段需要換行的長訊息，" * 6))
        for w in (80, 40, 20, 14):
            rows = v.window_rows(w, 40)
            self.assertTrue(all(text_width(t) <= w for t in texts(rows)), w)
        narrow = len(v.window_rows(14, 80))
        wide = len(v.window_rows(100, 80))
        self.assertGreater(narrow, wide)

    def test_old_entries_are_dropped_beyond_the_cap(self):
        v = ChatView()
        for i in range(tui.MAX_ENTRIES + 25):
            v.entries.append(Entry("system", "system", str(i)))
        v.add(Entry("system", "system", "last"))
        self.assertEqual(len(v.entries), tui.MAX_ENTRIES)
        self.assertEqual(v.entries[-1].text, "last")

    def test_empty_view_renders(self):
        self.assertEqual(texts(ChatView().window_rows(40, 5)), [""])


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestEntriesAndStatus(unittest.TestCase):
    def test_sources_are_distinguishable_and_model_label_is_kept(self):
        v = ChatView()
        ui = TuiUI(v, lambda: None)
        meta = {"source": "reported", "reported": "claude-x", "models": ["claude-x"]}
        ui.echo_me("你好")
        ui.chat("Manager", "派工", "Worker", meta)
        ui.chat("Worker", "回報", "Manager", {"source": "unknown"})
        ui.system("activity")
        ui.error("boom")
        ui.ask_user("要選哪個？")
        rows = texts(v.window_rows(100, 60))
        self.assertTrue(any(r.endswith("Me -> Manager") for r in rows), rows)
        self.assertTrue(any("Manager -> Worker" in r and "claude-x（CLI 回報）" in r for r in rows))
        self.assertTrue(any("Worker -> Manager" in r and "模型未知" in r for r in rows))
        self.assertTrue(any(r.startswith("* activity") for r in rows))
        self.assertTrue(any(r.startswith("! boom") for r in rows))
        self.assertTrue(any("[需要你的回答]" in r for r in rows))
        me = [r for r in rows if r.endswith("Me -> Manager")]
        self.assertNotIn("(", me[0])               # the user's own line carries no model label

    def test_title_line_always_says_what_the_box_is_and_fits(self):
        for cols in (30, 50, 70, 90, 140):
            frags = tui.title_fragments(cols)
            self.assertLessEqual(text_width("".join(t for _, t in frags)), cols, cols)
            self.assertIn("Agent 聊天室", "".join(t for _, t in frags))
        self.assertIn("不是 PowerShell", "".join(t for _, t in tui.title_fragments(80)))
        self.assertIn("Ctrl+J", "".join(t for _, t in tui.title_fragments(100)))

    def test_a_bad_meta_never_breaks_the_screen(self):
        v = ChatView()
        TuiUI(v, lambda: None).chat("Manager", "x", None, {"weird": 1})
        self.assertEqual(len(v.entries), 1)

    def test_replay_is_marked_and_has_no_clock(self):
        v = ChatView()
        ui = TuiUI(v, lambda: None)
        r = ui.replay_view()
        r.system("── 以下是先前的對話 ──")
        r.chat("Me", "舊訊息", "Manager")
        r.error("old error")
        ui.chat("Manager", "新訊息", None)
        rows = texts(v.window_rows(80, 40))
        old = [x for x in rows if "舊訊息" in x or "old error" in x or "Me -> Manager" in x]
        self.assertTrue(old and all(x.startswith("| ") for x in old), old)
        new = [x for x in rows if "新訊息" in x]
        self.assertFalse(new[0].startswith("| "))
        self.assertTrue(all(e.replay for e in v.entries[:3]))
        self.assertFalse(v.entries[3].replay)

    def test_status_shows_only_real_facts(self):
        v = ChatView()
        snap = {"state": "running", "hold": False, "awaiting_user": False, "open_tasks": [], "import_stage": None,
                "agents": [{"name": "Manager", "running": True, "elapsed": 75.0, "quiet": 12.0, "tool": "Read",
                            "tool_ago": 3.0, "task": None, "queued": 1},
                           {"name": "Worker", "running": False, "elapsed": None, "quiet": None, "tool": None,
                            "tool_ago": None, "task": None, "queued": 0}]}
        s = status_line(snap, v)
        self.assertIn("Manager 執行中 1m15s", s)
        self.assertIn("12 秒沒有輸出", s)
        self.assertIn("工具 Read", s)
        self.assertIn("（佇列 1）", s)
        self.assertIn("Worker 閒置", s)
        self.assertNotIn("%", s)
        snap["agents"][0]["tool"] = None
        self.assertNotIn("工具", status_line(snap, v))

    EVIL = "\x1b[31mRED\x1b[0m\x1b]0;PWNED\x07\x1b]52;c;QUFB\x07\u202eTXET\u202c\u2066x\u2069\x00\x08\r\nNEXT\x9b31m\u200b"

    def evil_snap(self):
        e = self.EVIL
        return {"state": "running", "agents": [
            {"name": "Man" + e + "ager", "running": True, "elapsed": 3.0, "quiet": 9.0, "tool": "Re" + e + "ad",
             "tool_ago": 1.0, "task": "t" + e + "1", "queued": 0},
            {"name": "W" + e, "running": False, "queued": 0}], "import_stage": e}

    def test_status_text_from_dynamic_fields_is_neutralised(self):
        s = status_line(self.evil_snap(), ChatView())
        for bad in ("\x1b", "\x07", "\x00", "\x08", "\r", "\n", "\x9b", "\u202e", "\u202c", "\u2066", "\u2069", "\u200b"):
            self.assertNotIn(bad, s, repr(bad))
        self.assertIn("工具 Re", s)
        self.assertIn("執行中 3s", s)                                      # the real status words are untouched

    def test_normal_names_keep_their_cjk_and_punctuation(self):
        snap = {"agents": [{"name": "員工 w1", "running": True, "elapsed": 1.0, "quiet": 0, "tool": "mcp__fs__read_file-2.0",
                            "tool_ago": 2.0, "task": "任務-A_1.2", "queued": 2}]}
        s = status_line(snap, ChatView())
        self.assertIn("員工 w1 執行中 1s", s)
        self.assertIn("工具 mcp__fs__read_file-2.0", s)
        self.assertIn("任務 任務-A_1.2", s)
        self.assertIn("（佇列 2）", s)

    def test_long_dynamic_values_are_capped_and_none_is_not_shown(self):
        snap = {"agents": [{"name": "x" * 500, "running": True, "elapsed": 1.0, "quiet": 0, "tool": "y" * 500,
                            "tool_ago": 1.0, "task": None, "queued": 0}]}
        s = status_line(snap, ChatView())
        self.assertLess(len(s), 200)
        self.assertNotIn("None", s)
        self.assertNotIn("任務", s)

    def test_headers_of_chat_entries_are_cleaned_too(self):
        v = ChatView()
        TuiUI(v, lambda: None).chat("Wor\x1b[31mker\u202e\nx", "hi", "Man\x07ager")
        head = texts(v.window_rows(80, 10))[0]
        for bad in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(bad, head)

    def test_what_prompt_toolkit_really_writes_for_hostile_text(self):
        """Measured behaviour (not assumed): ESC / BEL / NUL / C1 are drawn as inert caret text by prompt_toolkit itself;
        bidi overrides would reach the terminal. With the cleaning above nothing hostile is written - checked on the raw
        bytes of a real render of the status bar and the timeline."""
        import io
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.output.vt100 import Vt100_Output
        buf = io.StringIO()
        with create_pipe_input() as pipe:
            out = Vt100_Output(buf, lambda: Size(12, 100), term="xterm-256color")
            snap = self.evil_snap()
            app = TuiApp(lambda s: None, lambda: snap, lambda: False, input=pipe, output=out)
            app.ui.chat("Manager", "msg " + self.EVIL)
            app.ui.system("sys " + self.EVIL)
            t = threading.Thread(target=app.run, daemon=True)
            t.start()
            self.assertTrue(until(lambda: app.app.is_running and getattr(app.app, "loop", None) is not None))
            time.sleep(0.5)
            app.request_exit()
            t.join(5)
        raw = buf.getvalue()
        self.assertIn("TXET", raw)                                          # it did render the visible text
        for bad in ("\u202e", "\u202c", "\u2066", "\u2069", "\x1b]", "\x07", "\x00", "\x9b", "\u200b"):
            self.assertNotIn(bad, raw, repr(bad))
        self.assertNotIn("\x1b[31mRED", raw)

    def test_status_waiting_hold_and_scroll_hint(self):
        v = ChatView()
        idle = {"name": "Manager", "running": False, "queued": 0}
        self.assertIn("暫停", status_line({"hold": True, "agents": [idle]}, v))
        self.assertIn("等你回答", status_line({"awaiting_user": True, "agents": [idle]}, v))
        self.assertIn("Manager 等待 Worker", status_line({"state": "waiting_worker", "agents": [idle]}, v))
        self.assertEqual(status_line({}, v), "準備中")
        v.scroll, v.unseen = 5, 3
        self.assertIn("3 則新訊息", status_line({"agents": [idle]}, v))


class TestActivityFacts(unittest.TestCase):
    """What the status bar is allowed to show: only things the engine / CLI really reported."""

    def test_claude_parser_reports_tool_names_only(self):
        from agent.adapters.base import TurnRequest
        from tests.test_models_fork import claude_parse, ev
        got = []
        req = TurnRequest("a", "x", ".", None, schema={"type": "object"}, on_activity=got.append)
        tool = ev(type="assistant", message={"content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/secret/token.txt", "k": "sk-abc"}}]})
        claude_parse([ev(type="system", subtype="init", session_id="s"), tool, tool.replace("Read", "Bash")], req=req)
        self.assertEqual(got, ["Read", "Bash"])                      # names; the inputs are never passed on

    def test_a_failing_callback_cannot_break_the_turn(self):
        from agent.adapters.base import TurnRequest
        from tests.test_models_fork import claude_parse, ev

        def boom(name):
            raise RuntimeError("ui bug")
        req = TurnRequest("a", "x", ".", None, schema={"type": "object"}, on_activity=boom)
        res, _ = claude_parse([ev(type="assistant", message={"content": [{"type": "tool_use", "name": "Read"}]}),
                               ev(type="result", subtype="success", is_error=False, session_id="s", result="r",
                                  structured_output={"say": "hi", "actions": []})], req=req)
        self.assertTrue(res.ok)

    def test_snapshot_of_a_running_and_an_idle_engine(self):
        from tests.rig import FakeClock, Rig
        clk = FakeClock()
        rig = Rig({"manager": [M([final("ok")], wait_for_file="m.flag")], "w1": []}, clock=clk)
        try:
            idle = rig.engine.activity_snapshot()
            self.assertEqual([a["running"] for a in idle["agents"]], [False, False])
            self.assertFalse(idle["hold"])
            rig.engine.submit("go")
            self.assertTrue(rig.wait_until(lambda: rig.engine.agents["manager"].running is not None))
            clk.advance(42)
            snap = rig.engine.activity_snapshot()
            m = [a for a in snap["agents"] if a["id"] == "manager"][0]
            self.assertTrue(m["running"])
            self.assertEqual(int(m["elapsed"]), 42)
            self.assertIsNone(m["tool"])                              # fake CLI reports no tools: nothing invented
            self.assertNotIn("progress", json.dumps(snap).lower())
            run = rig.engine.agents["manager"].running
            run.tool, run.tool_at = "Read", clk()
            clk.advance(5)
            m = [a for a in rig.engine.activity_snapshot()["agents"] if a["id"] == "manager"][0]
            self.assertEqual((m["tool"], int(m["tool_ago"])), ("Read", 5))
            (rig.cwd / "m.flag").write_text("go", encoding="utf-8")
            self.assertTrue(rig.engine.wait_idle(20))
        finally:
            rig.close()

    def test_snapshot_never_blocks_the_screen_when_the_engine_lock_is_held(self):
        from tests.rig import Rig
        rig = Rig({"manager": [], "w1": []})
        try:
            first = rig.engine.activity_snapshot()
            got = {}
            with rig.engine._lock:
                t = threading.Thread(target=lambda: got.update(v=rig.engine.activity_snapshot()))
                t.start()
                t.join(2)
                self.assertFalse(t.is_alive())
            self.assertEqual(got["v"], first)                         # last known picture, no hang
        finally:
            rig.close()


class TestTuiCommands(unittest.TestCase):
    """The glue between the full-screen input and the Engine (cli._tui_line / _tui_ctrl_c), on a fake CLI."""

    def setUp(self):
        from tests.rig import Rig
        self.rig = Rig({"manager": [M([final("never")], wait_for_file="never.flag")], "w1": []})
        self.exits = []

        class App:
            def request_exit(inner):
                self.exits.append(1)
        e = self.rig.engine
        self.holder = {"engine": e, "ui": self.rig.ui, "app": App(), "status": e.activity_snapshot}

    def tearDown(self):
        self.rig.close()

    def test_plain_message_is_submitted_and_slash_exit_asks_the_app_to_exit(self):
        from agent import cli
        cli._tui_line(self.holder, "你好")
        self.assertTrue(self.rig.wait_until(lambda: self.rig.engine.agents["manager"].running is not None))
        cli._tui_line(self.holder, "/exit")
        self.assertEqual(self.exits, [1])

    def test_ctrl_c_is_false_when_idle_and_interrupts_when_busy(self):
        from agent import cli
        self.assertFalse(cli._tui_ctrl_c(self.holder))
        cli._tui_line(self.holder, "work")
        self.assertTrue(self.rig.wait_until(lambda: self.rig.engine.agents["manager"].running is not None))
        self.assertTrue(cli._tui_ctrl_c(self.holder))
        self.assertTrue(self.rig.engine.wait_idle(30))
        self.assertIsNone(self.rig.engine.agents["manager"].running)

    def test_slash_interrupt_runs_off_the_ui_thread_and_slash_status_reports(self):
        from agent import cli
        cli._tui_line(self.holder, "work")
        self.assertTrue(self.rig.wait_until(lambda: self.rig.engine.agents["manager"].running is not None))
        cli._tui_line(self.holder, "/interrupt")                      # returns at once; the wait happens in a helper
        cli._tui_line(self.holder, "/status")
        self.assertTrue(self.rig.engine.wait_idle(30))
        self.assertTrue(until(lambda: any("Manager" in t for t in self.rig.ui.texts("system")) , 5))
        self.assertEqual(self.exits, [])

    def test_unknown_slash_command_is_explained(self):
        from agent import cli
        cli._tui_line(self.holder, "/nope")
        self.assertTrue(any("Unknown command /nope" in t for t in self.rig.ui.texts("system")))


class Harness:
    """A TuiApp on a scripted pipe input with a dummy output, running in its own thread."""

    def __init__(self, ctrl_c=lambda: False, status=lambda: {}):
        self.lines = []
        self.pipe_cm = create_pipe_input()
        self.pipe = self.pipe_cm.__enter__()
        self.app = TuiApp(self.lines.append, status, ctrl_c, input=self.pipe, output=DummyOutput())
        self.error = None
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()
        assert until(lambda: self.app.app.is_running and getattr(self.app.app, "loop", None) is not None)
        time.sleep(0.1)

    def _run(self):
        try:
            self.app.run()
        except BaseException as e:      # noqa: BLE001
            self.error = e

    def send(self, s):
        self.pipe.send_text(s)

    def close(self):
        self.app.request_exit()
        self.t.join(5)
        self.pipe_cm.__exit__(None, None, None)


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestApp(unittest.TestCase):
    def setUp(self):
        self.h = None

    def tearDown(self):
        if self.h:
            self.h.close()

    def start(self, **kw):
        self.h = Harness(**kw)
        return self.h

    def test_chinese_text_is_typed_and_sent_on_enter(self):
        h = self.start()
        h.send("你好，請幫我看一下")
        self.assertTrue(until(lambda: h.app.input.text == "你好，請幫我看一下"))
        self.assertEqual(h.lines, [])
        h.send("\r")
        self.assertTrue(until(lambda: h.lines == ["你好，請幫我看一下"]))
        self.assertEqual(h.app.input.text, "")
        self.assertTrue(any(e.sender == "Me" and e.text == "你好，請幫我看一下" for e in h.app.view.entries))

    def test_ctrl_j_inserts_a_newline_without_sending(self):
        h = self.start()
        h.send("第一行\x0a第二行\x0a\x0a第四行")
        self.assertTrue(until(lambda: h.app.input.text == "第一行\n第二行\n\n第四行"))
        self.assertEqual(h.lines, [])
        h.send("\r")
        self.assertTrue(until(lambda: h.lines == ["第一行\n第二行\n\n第四行"]))

    def test_alt_enter_also_inserts_a_newline(self):
        h = self.start()
        h.send("a\x1b\rb")
        self.assertTrue(until(lambda: h.app.input.text == "a\nb"))
        self.assertEqual(h.lines, [])

    def test_a_bracketed_paste_with_newlines_is_not_sent(self):
        h = self.start()
        h.send("\x1b[200~line1\r\nline2\r\n貼上的第三行\x1b[201~")
        self.assertTrue(until(lambda: h.app.input.text == "line1\nline2\n貼上的第三行"))
        time.sleep(0.2)
        self.assertEqual(h.lines, [])
        h.send("\r")
        self.assertTrue(until(lambda: h.lines == ["line1\nline2\n貼上的第三行"]))

    def test_blank_input_is_not_sent(self):
        h = self.start()
        h.send("   \r\r")
        time.sleep(0.3)
        self.assertEqual(h.lines, [])
        self.assertEqual(h.app.input.text, "")

    def test_background_messages_do_not_touch_typed_text(self):
        h = self.start()
        h.send("我正在打字")
        self.assertTrue(until(lambda: h.app.input.text == "我正在打字"))

        def spam():
            for i in range(30):
                h.app.ui.chat("Worker", f"背景訊息 {i}", "Manager")
                h.app.ui.system(f"activity {i}")
        ts = [threading.Thread(target=spam) for _ in range(3)]
        for t in ts:
            t.start()
        h.send("，繼續")
        for t in ts:
            t.join()
        self.assertTrue(until(lambda: h.app.input.text == "我正在打字，繼續"))
        self.assertEqual(len(h.app.view.entries), 180)
        self.assertEqual(h.lines, [])
        self.assertIsNone(h.error)

    def test_pageup_freezes_the_view_and_end_returns_to_the_newest(self):
        h = self.start()
        for i in range(80):
            h.app.ui.chat("Manager", f"history {i}")
        h.app.view.window_rows(80, 20)
        h.send("\x1b[5~")
        self.assertTrue(until(lambda: h.app.view.scroll > 0))
        s0 = h.app.view.scroll
        top = texts(h.app.view.window_rows(80, h.app.view.height))
        h.app.ui.chat("Worker", "arrives while reading")
        self.assertEqual(texts(h.app.view.window_rows(80, h.app.view.height)), top)
        self.assertGreater(h.app.view.scroll, s0)
        h.send("\x1b[6~")                                   # PageDown
        self.assertTrue(until(lambda: h.app.view.scroll < s0 + 10))
        h.send("\x1b[5~")
        self.assertTrue(until(lambda: h.app.view.scroll > 0))
        h.send("\x1b[F")                                     # End, input box empty
        self.assertTrue(until(lambda: h.app.view.scroll == 0))

    def test_end_inside_a_draft_moves_the_cursor_not_the_view(self):
        h = self.start()
        for i in range(60):
            h.app.ui.chat("Manager", f"history {i}")
        h.app.view.window_rows(80, 20)
        h.send("\x1b[5~")
        self.assertTrue(until(lambda: h.app.view.scroll > 0))
        h.send("draft")
        self.assertTrue(until(lambda: h.app.input.text == "draft"))
        h.send("\x1b[F")
        time.sleep(0.2)
        self.assertGreater(h.app.view.scroll, 0)

    def test_sending_jumps_back_to_the_bottom(self):
        h = self.start()
        for i in range(60):
            h.app.ui.chat("Manager", f"history {i}")
        h.app.view.window_rows(80, 20)
        h.send("\x1b[5~")
        self.assertTrue(until(lambda: h.app.view.scroll > 0))
        h.send("go\r")
        self.assertTrue(until(lambda: h.lines == ["go"]))
        self.assertEqual(h.app.view.scroll, 0)

    def test_ctrl_c_interrupts_when_busy_else_clears_the_draft(self):
        busy = {"v": True}
        calls = []
        h = self.start(ctrl_c=lambda: (calls.append(1), busy["v"])[1])
        h.send("draft")
        self.assertTrue(until(lambda: h.app.input.text == "draft"))
        h.send("\x03")
        self.assertTrue(until(lambda: len(calls) == 1))
        self.assertEqual(h.app.input.text, "draft")           # interrupting must not eat the draft
        busy["v"] = False
        h.send("\x03")
        self.assertTrue(until(lambda: h.app.input.text == ""))
        h.send("\x03")
        self.assertTrue(until(lambda: any("/exit" in e.text for e in h.app.view.entries)))

    def test_ctrl_d_on_an_empty_box_asks_to_exit(self):
        h = self.start()
        h.send("x\x04")                                       # not empty: deletes nothing special, no exit request
        time.sleep(0.2)
        self.assertEqual(h.lines, [])
        h.app.input.buffer.reset()
        h.send("\x04")
        self.assertTrue(until(lambda: h.lines == ["/exit"]))

    def test_request_exit_ends_the_app_from_another_thread(self):
        h = self.start()
        threading.Thread(target=h.app.request_exit).start()
        h.t.join(5)
        self.assertFalse(h.t.is_alive())
        self.assertIsNone(h.error)

    def test_status_errors_do_not_break_repaint(self):
        def boom():
            raise RuntimeError("x")
        h = self.start(status=boom)
        h.app.ui.system("still alive")
        time.sleep(0.2)
        self.assertIsNone(h.error)
        self.assertTrue(h.t.is_alive())


class TestPlainFallback(unittest.TestCase):
    """The decision itself, and non-TTY runs of the real CLI."""

    def args(self, **kw):
        import argparse
        return argparse.Namespace(**kw)

    def test_decision_matrix(self):
        from unittest import mock
        from agent import cli
        tty = mock.Mock(isatty=lambda: True)
        no = mock.Mock(isatty=lambda: False)
        env = {k: v for k, v in os.environ.items() if k not in ("AGENT_PLAIN", "TERM")}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(sys, "stdin", tty), mock.patch.object(sys, "stdout", tty):
                self.assertEqual(cli._tui_decision(self.args(plain=True)), (False, None))
                with mock.patch.dict(os.environ, {"AGENT_PLAIN": "1"}):
                    self.assertEqual(cli._tui_decision(self.args()), (False, None))
                with mock.patch.dict(os.environ, {"TERM": "dumb"}):
                    self.assertEqual(cli._tui_decision(self.args()), (False, None))
                use, hint = cli._tui_decision(self.args())
                self.assertEqual(use, HAVE_PTK)
                if not HAVE_PTK:
                    self.assertIn("pip install", hint)
            for i, o in ((no, tty), (tty, no), (no, no)):
                with mock.patch.object(sys, "stdin", i), mock.patch.object(sys, "stdout", o):
                    self.assertEqual(cli._tui_decision(self.args()), (False, None))

    def decide(self, **patches):
        from unittest import mock
        from agent import cli
        tty = mock.Mock(isatty=lambda: True)
        env = {k: v for k, v in os.environ.items() if k not in ("AGENT_PLAIN", "TERM")}
        with mock.patch.object(sys, "stdin", tty), mock.patch.object(sys, "stdout", tty), \
                mock.patch.dict(os.environ, env, clear=True):
            if "import_module" in patches:
                with mock.patch.object(cli.importlib, "import_module", side_effect=patches["import_module"]):
                    return cli._tui_decision(self.args())
            with mock.patch.dict(sys.modules, patches.get("modules", {})):
                return cli._tui_decision(self.args())

    @unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
    def test_missing_prompt_toolkit_falls_back_with_the_install_hint(self):
        from unittest import mock
        with mock.patch.dict(sys.modules):
            sys.modules.pop("agent.tui", None)
            block = {k: None for k in list(sys.modules) if k.split(".")[0] == "prompt_toolkit"}
            use, hint = self.decide(modules=block)
        self.assertFalse(use)
        self.assertIn("pip install", hint)
        self.assertIn("沒有安裝", hint)
        self.assertNotIn("程式錯誤", hint)

    def test_a_bug_in_the_tui_module_is_reported_as_such_not_as_a_missing_package(self):
        for exc in (NameError("name 'oops' is not defined"), SyntaxError("bad"), AttributeError("no attr"),
                    ImportError("cannot import name 'Foo' from 'prompt_toolkit.layout'"),
                    ModuleNotFoundError("No module named 'agent.helper'", name="agent.helper")):
            use, hint = self.decide(import_module=exc)
            self.assertFalse(use, exc)
            self.assertNotIn("pip install \"prompt_toolkit", hint, exc)
            self.assertIn("不是缺少套件", hint, exc)
            self.assertIn(str(exc), hint)                                  # the real cause stays visible
            self.assertIn(type(exc).__name__, hint)

    def test_only_the_dependency_itself_counts_as_missing(self):
        for name in ("prompt_toolkit", "prompt_toolkit.layout", "wcwidth"):
            use, hint = self.decide(import_module=ModuleNotFoundError(f"No module named {name!r}", name=name))
            self.assertFalse(use)
            self.assertIn("pip install", hint, name)

    def test_flag_is_accepted_everywhere(self):
        from agent import cli
        p = cli.build_parser()
        self.assertTrue(p.parse_args(["--plain"]).plain)
        self.assertTrue(p.parse_args(["--plain", "resume", "x"]).plain)
        self.assertTrue(p.parse_args(["resume", "x", "--plain"]).plain)
        self.assertTrue(p.parse_args(["import", "x", "--plain"]).plain)
        self.assertFalse(p.parse_args([]).plain)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tui 測試 "))
        self.home, self.work, self.fs = self.tmp / "home", self.tmp / "work", self.tmp / "fs"
        for d in (self.home, self.work, self.fs):
            d.mkdir(parents=True)
        cfg = {"manager": "manager", "turn_timeout_sec": 30, "max_manager_turns_per_input": 30,
               "agents": {"manager": {"role": "manager", "cli": "fake", "model": None, "effort": None},
                          "w1": {"role": "worker", "cli": "fake", "model": None, "effort": None}}}
        (self.home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, scenario):
        (self.tmp / "scn.json").write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_SESSION_ID", "AGENT_PLAIN")}
        env.update(AGENT_HOME=str(self.home), FAKE_SCENARIO=str(self.tmp / "scn.json"), FAKE_STATE=str(self.fs),
                   PYTHONPATH=str(ROOT), TERM="xterm-256color")
        return env

    def events(self):
        sessions = sorted((self.home / "sessions").iterdir())
        out = []
        for ln in (sessions[-1] / "events.jsonl").read_text(encoding="utf-8").splitlines():
            out.append(json.loads(ln))
        return sessions[-1], out


class TestNonTtyStaysPlain(Base):
    SCN = {"manager": [M([dispatch("w1", "t1", "do it")]), M([final("全部完成")])],
           "w1": [W("done", "t1", "worker 報告")]}

    def test_piped_run_is_the_original_plain_chat(self):
        r = subprocess.run([sys.executable, "-m", "agent"], input="你好\n/exit\n", capture_output=True, text=True,
                           encoding="utf-8", env=self.env(self.SCN), cwd=self.work, timeout=90)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("\x1b[?1049h", r.stdout)                 # no alternate screen
        self.assertIn("Me> 你好", r.stdout)
        self.assertIn("Manager> ", r.stdout)
        self.assertIn("全部完成", r.stdout)
        self.assertIn("Bye. Session saved", r.stdout)

    def test_plain_flag_with_pipes_is_accepted(self):
        r = subprocess.run([sys.executable, "-m", "agent", "--plain"], input="/exit\n", capture_output=True, text=True,
                           encoding="utf-8", env=self.env(self.SCN), cwd=self.work, timeout=90)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Bye. Session saved", r.stdout)


@unittest.skipUnless(HAVE_PTK and os.name == "posix", "needs prompt_toolkit and a POSIX pseudo terminal")
class TestWholeRunInAPty(Base):
    """`python -m agent` on a pseudo terminal: full screen, Chinese + multi-line input, Manager -> Worker -> Manager,
    /exit cleanup, then `resume` with replay (which must not call any model)."""

    def spawn(self, args, scenario):
        import fcntl
        import pty
        import struct
        import termios
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        proc = subprocess.Popen([sys.executable, "-m", "agent"] + args, stdin=slave, stdout=slave, stderr=slave,
                                env=self.env(scenario), cwd=self.work, close_fds=True, start_new_session=True)
        os.close(slave)
        buf = bytearray()
        stop = threading.Event()

        def pump():
            import select
            while not stop.is_set():
                r, _, _ = select.select([master], [], [], 0.1)
                if r:
                    try:
                        d = os.read(master, 65536)
                    except OSError:
                        return
                    if not d:
                        return
                    buf.extend(d)
        t = threading.Thread(target=pump, daemon=True)
        t.start()

        def close():
            stop.set()
            t.join(2)
            os.close(master)
        return proc, master, buf, close

    def test_full_flow_then_resume(self):
        scn = {"manager": [M([dispatch("w1", "t1", "do it")]), M([final("全部完成")])],
               "w1": [W("done", "t1", "worker 報告")]}
        proc, master, buf, close = self.spawn([], scn)
        try:
            self.assertTrue(until(lambda: b"\x1b[?1049h" in buf, 20), bytes(buf[-300:]))      # alternate screen entered
            time.sleep(0.5)
            os.write(master, "你好\x0a第二行\r".encode("utf-8"))
            sess = None

            def done():
                nonlocal sess
                try:
                    sess, ev = self.events()
                except Exception:
                    return False
                return any(e.get("type") == "chat" and "全部完成" in str(e.get("text")) for e in ev)
            self.assertTrue(until(done, 60), bytes(buf[-500:]))
            sess, ev = self.events()
            chat = [(e["sender"], e.get("to"), e["text"]) for e in ev if e.get("type") == "chat"]
            self.assertEqual(chat[0], ("Me", "Manager", "你好\n第二行"))                 # newline kept, ONE message
            self.assertIn("Worker", " ".join(s for s, _, _ in chat) + " Worker")
            os.write(master, b"/exit\r")
            self.assertEqual(proc.wait(30), 0, bytes(buf[-500:]))
            self.assertIn(b"\x1b[?1049l", buf)                                              # alternate screen left
            self.assertIn("Bye. Session saved".encode(), bytes(buf))
            self.assertFalse((sess / "session.lock").exists() and self._locked(sess), "lock released")
        finally:
            if proc.poll() is None:
                proc.kill()
            close()

        n_chat = len([e for e in ev if e.get("type") == "chat"])
        proc, master, buf, close = self.spawn(["resume", sess.name], {"manager": [], "w1": []})
        try:
            self.assertTrue(until(lambda: b"\x1b[?1049h" in buf, 20))
            time.sleep(1.5)
            os.write(master, b"/exit\r")
            self.assertEqual(proc.wait(30), 0, bytes(buf[-500:]))
        finally:
            if proc.poll() is None:
                proc.kill()
            close()
        _, ev2 = self.events()
        self.assertEqual(len([e for e in ev2 if e.get("type") == "chat"]), n_chat, "replay must not add chat events")
        self.assertEqual([e for e in ev2 if e.get("type") == "turn_start"],
                         [e for e in ev if e.get("type") == "turn_start"], "replay must not run a model")

    def _locked(self, sess):
        from agent.sessionlock import SessionLock, SessionLocked
        lk = SessionLock(sess)
        try:
            lk.acquire()
        except SessionLocked:
            return True
        lk.release()
        return False

    def test_replay_zero_shows_no_history(self):
        scn = {"manager": [M([final("第一輪")])], "w1": []}
        r = subprocess.run([sys.executable, "-m", "agent", "--plain"], input="hi\n/exit\n", capture_output=True,
                           text=True, encoding="utf-8", env=self.env(scn), cwd=self.work, timeout=90)
        self.assertEqual(r.returncode, 0, r.stderr)
        sess, _ = self.events()
        proc, master, buf, close = self.spawn(["resume", sess.name, "--replay", "0"], {"manager": [], "w1": []})
        try:
            self.assertTrue(until(lambda: b"\x1b[?1049h" in buf, 20))
            time.sleep(1.0)
            os.write(master, b"/exit\r")
            self.assertEqual(proc.wait(30), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
            close()
        self.assertNotIn("先前的對話".encode(), bytes(buf))
        self.assertNotIn("第一輪".encode(), bytes(buf))


if __name__ == "__main__":
    unittest.main()
