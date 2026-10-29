"""Up-arrow input history of the full-screen chat room (Batch 3B).

A  basic history          B  draft protection      C  multi-line boxes
D  the Engine is never touched by browsing           E  resume: the history comes back, read-only
Fake CLI and scripted key input only: real Windows Terminal / PowerShell console key handling, IME and clipboard are
manual checks (see README)."""
import copy
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.inputhistory import HISTORY_LIMIT, user_inputs  # noqa: E402
from tests.rig import M, final  # noqa: E402

try:
    import prompt_toolkit  # noqa: F401
    HAVE_PTK = True
except ImportError:
    HAVE_PTK = False

if HAVE_PTK:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from agent import tui
    from agent.tui import TuiApp

UP, DOWN, CTRL_P, CTRL_N, ENTER, CTRL_J = "\x1b[A", "\x1b[B", "\x10", "\x0e", "\r", "\n"


def until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def chat(sender, text, to=None):
    return {"type": "chat", "sender": sender, "to": to, "text": text}


class TestUserInputsFromEvents(unittest.TestCase):
    def test_only_what_the_user_sent_is_history(self):
        events = [chat("Me", "第一則", "Manager"),
                  chat("Manager", "我是 Manager 的回覆", "Me"),
                  chat("w1", "worker 報告", "Manager"),
                  {"type": "ui", "kind": "system", "text": "Me> 系統訊息"},
                  {"type": "ask_user", "text": "請問要不要繼續？"},
                  {"type": "turn_start", "agent": "manager", "prompt_chars": 3},
                  {"type": "turn_end", "text": "Me"},
                  chat("Me", "second", "Manager")]
        self.assertEqual(user_inputs(events), ["第一則", "second"])

    def test_redacted_lines_are_left_out_never_repaired(self):
        events = [chat("Me", "before"), chat("Me", "my key is [REDACTED] ok"), chat("Me", "after")]
        self.assertEqual(user_inputs(events), ["before", "after"])

    def test_blank_and_damaged_events_are_ignored(self):
        events = [chat("Me", ""), chat("Me", "   \n "), chat("Me", None), chat("Me", 5), {"type": "chat"},
                  "junk", None, 7, {"sender": "Me", "text": "no type"}, chat("Me", "ok")]
        self.assertEqual(user_inputs(events), ["ok"])

    def test_only_the_newest_200_are_kept_whatever_the_session_size(self):
        events = []
        for i in range(1000):
            events += [chat("Me", f"msg {i}"), chat("Manager", f"reply {i}")]
        got = user_inputs(events)
        self.assertEqual(len(got), HISTORY_LIMIT)
        self.assertEqual(got[0], "msg 800")
        self.assertEqual(got[-1], "msg 999")
        self.assertEqual(user_inputs(iter(events), limit=3), ["msg 997", "msg 998", "msg 999"])   # any iterable
        self.assertEqual(user_inputs(events, limit=0), [])

    def test_the_limit_counts_usable_lines_only(self):
        events = [chat("Me", f"keep {i}") for i in range(150)] + [chat("Me", "[REDACTED]")] * 100 + \
                 [chat("Me", f"late {i}") for i in range(100)]
        got = user_inputs(events)
        self.assertEqual(len(got), 200)
        self.assertEqual(got[0], "keep 50")

    def test_events_are_not_modified(self):
        events = [chat("Me", "a"), chat("Manager", "b"), chat("Me", "c [REDACTED]")]
        snapshot = copy.deepcopy(events)
        user_inputs(events)
        self.assertEqual(events, snapshot)


class Harness:
    """A TuiApp on a scripted pipe input with a dummy output, running in its own thread."""

    def __init__(self, history=(), status=lambda: {}):
        self.lines = []
        self.pipe_cm = create_pipe_input()
        self.pipe = self.pipe_cm.__enter__()
        self.app = TuiApp(self.lines.append, status, lambda: False, input=self.pipe, output=DummyOutput(),
                          history=history)
        self.error = None
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()
        assert until(lambda: self.app.app.is_running and getattr(self.app.app, "loop", None) is not None)
        time.sleep(0.15)                       # the first repaint loads the history into the box

    def _run(self):
        try:
            self.app.run()
        except BaseException as e:      # noqa: BLE001
            self.error = e

    @property
    def text(self):
        return self.app.input.text

    def send(self, s, settle=0.12):
        self.pipe.send_text(s)
        time.sleep(settle)

    def say(self, *msgs):
        """Type and send each message."""
        for m in msgs:
            self.send(m)
            self.send(ENTER)
        assert until(lambda: len(self.lines) >= len(msgs))

    def close(self):
        self.app.request_exit()
        self.t.join(5)
        self.pipe_cm.__exit__(None, None, None)


class HistoryCase(unittest.TestCase):
    def start(self, **kw):
        self.h = Harness(**kw)
        self.addCleanup(self.h.close)
        return self.h


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestBasicHistory(HistoryCase):
    def test_up_walks_back_through_what_was_sent_and_down_walks_forward(self):
        h = self.start()
        h.say("A", "B", "C")
        h.send(UP)
        self.assertEqual(h.text, "C")
        h.send(UP)
        self.assertEqual(h.text, "B")
        h.send(UP)
        self.assertEqual(h.text, "A")
        h.send(DOWN)
        self.assertEqual(h.text, "B")
        h.send(DOWN)
        self.assertEqual(h.text, "C")
        h.send(DOWN)
        self.assertEqual(h.text, "")                      # back at the newest position: an empty box

    def test_the_ends_of_the_history_are_safe(self):
        h = self.start()
        h.send(UP)
        self.assertEqual(h.text, "")                      # nothing sent yet
        h.send(DOWN)
        h.send(CTRL_P)
        h.send(CTRL_N)
        self.assertEqual(h.text, "")
        h.say("only")
        for _ in range(5):
            h.send(UP)
        self.assertEqual(h.text, "only")                  # the top stays on the oldest entry
        for _ in range(5):
            h.send(DOWN)
        self.assertEqual(h.text, "")
        self.assertIsNone(h.error)

    def test_blank_input_is_not_added(self):
        h = self.start()
        h.say("A")
        h.send("   ")
        h.send(ENTER)
        h.send(ENTER)
        h.send(UP)
        self.assertEqual(h.text, "A")
        h.send(UP)
        self.assertEqual(h.text, "A")                     # no empty entry in between
        self.assertEqual(h.lines, ["A"])

    def test_chinese_and_mixed_text_come_back_exactly(self):
        h = self.start()
        h.say("幫我分析目前的專案結構", "請讓 Worker 檢查 test 結果，謝謝！")
        h.send(UP)
        self.assertEqual(h.text, "請讓 Worker 檢查 test 結果，謝謝！")
        h.send(UP)
        self.assertEqual(h.text, "幫我分析目前的專案結構")

    def test_ctrl_p_and_ctrl_n_move_between_entries(self):
        h = self.start()
        h.say("A", "B", "C")
        h.send(CTRL_P)
        self.assertEqual(h.text, "C")
        h.send(CTRL_P)
        self.assertEqual(h.text, "B")
        h.send(CTRL_N)
        self.assertEqual(h.text, "C")
        h.send(CTRL_N)
        self.assertEqual(h.text, "")

    def test_an_immediate_repeat_is_one_entry_and_the_memory_is_bounded(self):
        h = self.start()
        h.say("same", "same")
        h.send(UP)
        h.send(UP)
        self.assertEqual(h.text, "same")
        self.assertEqual(h.app._history, ["same"])
        for i in range(HISTORY_LIMIT + 25):               # straight into the model so the test stays quick
            h.app._remember(f"m{i}")
        self.assertEqual(len(h.app._history), HISTORY_LIMIT)
        self.assertEqual(h.app._history[-1], f"m{HISTORY_LIMIT + 24}")

    def test_a_history_entry_can_be_edited_and_sent_and_the_original_stays(self):
        h = self.start()
        h.say("alpha", "beta")
        h.send(UP)
        h.send("!")                                       # edit "beta" -> "beta!"
        self.assertEqual(h.text, "beta!")
        self.assertEqual(h.lines, ["alpha", "beta"])      # still not sent
        h.send(ENTER)
        self.assertTrue(until(lambda: h.lines == ["alpha", "beta", "beta!"]))
        h.send(UP)
        self.assertEqual(h.text, "beta!")
        h.send(UP)
        self.assertEqual(h.text, "beta")                  # the original entry was not rewritten
        h.send(UP)
        self.assertEqual(h.text, "alpha")


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestDraftIsProtected(HistoryCase):
    def test_the_unsent_draft_comes_back_at_the_newest_position(self):
        h = self.start()
        h.say("A", "B")
        h.send("我還在打的草稿")
        h.send(UP)
        self.assertEqual(h.text, "B")
        h.send(UP)
        self.assertEqual(h.text, "A")
        h.send(DOWN)
        h.send(DOWN)
        self.assertEqual(h.text, "我還在打的草稿")
        self.assertEqual(h.lines, ["A", "B"])             # nothing was sent while browsing

    def test_the_draft_survives_ctrl_p_ctrl_n_too(self):
        h = self.start()
        h.say("A", "B")
        h.send("draft")
        h.send(CTRL_P)
        h.send(CTRL_P)
        self.assertEqual(h.text, "A")
        h.send(CTRL_N)
        h.send(CTRL_N)
        self.assertEqual(h.text, "draft")

    def test_background_messages_change_neither_the_draft_nor_the_browsing_position(self):
        h = self.start()
        h.say("A", "B")
        h.send("草稿")
        h.send(UP)
        self.assertEqual(h.text, "B")
        for i in range(5):                                # what the Engine does from its own threads
            h.app.ui.chat("Manager", f"背景訊息 {i}", "Me")
            h.app.ui.system(f"w1 開始執行 {i}")
            h.app.ui.error("某個錯誤")
        time.sleep(0.2)
        self.assertEqual(h.text, "B")
        h.send(UP)
        self.assertEqual(h.text, "A")
        h.send(DOWN)
        h.send(DOWN)
        self.assertEqual(h.text, "草稿")


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestMultiLineBox(HistoryCase):
    def test_up_moves_the_cursor_inside_a_multi_line_draft_before_it_touches_the_history(self):
        h = self.start(history=["older entry"])
        h.send("l1")
        h.send(CTRL_J)
        h.send("l2")
        h.send(CTRL_J)
        h.send("l3")
        doc = h.app.input.document
        self.assertEqual((h.text, doc.cursor_position_row), ("l1\nl2\nl3", 2))
        h.send(UP)
        self.assertEqual((h.text, h.app.input.document.cursor_position_row), ("l1\nl2\nl3", 1))
        h.send(UP)
        self.assertEqual((h.text, h.app.input.document.cursor_position_row), ("l1\nl2\nl3", 0))
        h.send(UP)                                        # first line reached: only now the history
        self.assertEqual(h.text, "older entry")
        h.send(DOWN)                                      # one line only, so this is both the first and last row
        self.assertEqual(h.text, "l1\nl2\nl3")            # the draft, intact

    def test_down_moves_inside_the_draft_and_leaves_it_only_on_the_last_line(self):
        h = self.start(history=["x", "multi\nline\nentry"])
        h.send(UP)                                        # the multi-line entry, cursor at its end
        self.assertEqual(h.text, "multi\nline\nentry")
        h.send(UP)
        h.send(UP)                                        # walks up its lines first
        self.assertEqual((h.text, h.app.input.document.cursor_position_row), ("multi\nline\nentry", 0))
        h.send(DOWN)
        h.send(DOWN)
        self.assertEqual((h.text, h.app.input.document.cursor_position_row), ("multi\nline\nentry", 2))
        h.send(DOWN)                                      # last line: leave for the newer position
        self.assertEqual(h.text, "")

    def test_ctrl_p_and_ctrl_n_jump_entries_from_any_row(self):
        h = self.start(history=["first", "second\nin two"])
        h.send("d1")
        h.send(CTRL_J)
        h.send("d2")
        h.send(UP)                                        # cursor on row 0 of the draft (still the draft)
        self.assertEqual(h.text, "d1\nd2")
        h.send(CTRL_P)                                    # straight to the previous entry, wherever the cursor is
        self.assertEqual(h.text, "second\nin two")
        h.send(CTRL_P)
        self.assertEqual(h.text, "first")
        h.send(CTRL_N)
        h.send(CTRL_N)
        self.assertEqual(h.text, "d1\nd2")

    def test_a_multi_line_message_is_stored_and_restored_whole(self):
        h = self.start()
        h.send("第一行")
        h.send(CTRL_J)
        h.send("第二行")
        h.send(ENTER)
        self.assertTrue(until(lambda: h.lines == ["第一行\n第二行"]))
        h.send(UP)
        self.assertEqual(h.text, "第一行\n第二行")
        h.send(CTRL_J)                                    # Ctrl+J still just inserts a newline
        h.send("第三行")
        self.assertEqual(h.text, "第一行\n第二行\n第三行")
        self.assertEqual(h.lines, ["第一行\n第二行"])

    def test_a_multi_line_paste_is_one_draft_never_several_sends_or_history_entries(self):
        h = self.start(history=["old"])
        h.send("\x1b[200~line1\r\nline2\r\n貼上的第三行\x1b[201~", settle=0.3)
        self.assertEqual(h.text, "line1\nline2\n貼上的第三行")
        self.assertEqual(h.lines, [])
        self.assertEqual(h.app._history, ["old"])
        h.send(ENTER)
        self.assertTrue(until(lambda: h.lines == ["line1\nline2\n貼上的第三行"]))
        self.assertEqual(h.app._history, ["old", "line1\nline2\n貼上的第三行"])


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestBrowsingNeverReachesTheEngine(HistoryCase):
    """The glue is the real one (cli._tui_line) on a real Engine with a fake CLI that has NO scripted turn: a model call
    would fail the test."""

    def setUp(self):
        from agent import cli
        self.cli = cli
        self.exits = []
        self.sent = []

    def new_rig(self, scenario):
        from tests.rig import Rig
        self.rig = Rig(scenario)
        self.addCleanup(self.rig.close)

        class App:
            def request_exit(inner):
                self.exits.append(1)
        e = self.rig.engine
        self.holder = {"engine": e, "ui": self.rig.ui, "app": App(), "status": e.activity_snapshot}

    def harness(self, **kw):
        h = Harness(**kw)
        h.app.on_line = lambda s: (h.lines.append(s), self.sent.append(s), self.cli._tui_line(self.holder, s))
        self.addCleanup(h.close)
        return h

    def chats(self):
        return [e for e in self.rig.store.read_events(self.rig.store.dir) if e.get("type") == "chat"]

    def turns(self):
        return [e for e in self.rig.store.read_events(self.rig.store.dir) if e.get("type") == "turn_start"]

    def test_browsing_calls_no_model_and_logs_no_chat_event(self):
        self.new_rig({"manager": [], "w1": []})
        h = self.harness(history=["/status", "earlier question"])
        before = len(self.rig.store.read_events(self.rig.store.dir))
        for key in (UP, UP, UP, DOWN, CTRL_P, CTRL_P, CTRL_N, DOWN, DOWN):
            h.send(key)
        time.sleep(0.3)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.chats(), [])
        self.assertEqual(self.turns(), [])
        self.assertEqual(len(self.rig.store.read_events(self.rig.store.dir)), before)
        self.assertEqual(self.rig.engine.agents["manager"].turns, 0)
        self.assertEqual(self.rig.engine.state.value, "idle")

    def test_a_remembered_slash_command_runs_only_when_enter_is_pressed(self):
        self.new_rig({"manager": [], "w1": []})
        h = self.harness()
        h.say("/status")
        self.assertEqual(self.sent, ["/status"])
        n = len(self.rig.ui.texts("system"))
        h.send(UP)
        self.assertEqual(h.text, "/status")               # in the box, not executed
        time.sleep(0.3)
        self.assertEqual(self.sent, ["/status"])
        self.assertEqual(len(self.rig.ui.texts("system")), n)
        h.send(ENTER)
        self.assertTrue(until(lambda: self.sent == ["/status", "/status"]))
        self.assertTrue(until(lambda: len(self.rig.ui.texts("system")) > n))

    def test_a_remembered_interrupt_or_continue_is_not_run_by_selecting_it(self):
        self.new_rig({"manager": [], "w1": []})
        h = self.harness(history=["/continue", "/interrupt", "/exit"])
        for key in (UP, UP, UP, CTRL_N, CTRL_N):
            h.send(key)
        time.sleep(0.2)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.exits, [])                  # /exit in the history did not exit

    def test_only_enter_submits_and_then_the_engine_sees_exactly_one_message(self):
        self.new_rig({"manager": [M([final("收到")])], "w1": []})
        e = self.rig.engine
        h = self.harness(history=["請檢查專案"])
        h.send(UP)
        self.assertEqual(h.text, "請檢查專案")
        time.sleep(0.2)
        self.assertEqual(self.turns(), [])
        h.send(ENTER)
        self.assertTrue(until(lambda: len(self.turns()) == 1, 20))
        self.assertTrue(e.wait_idle(30))
        self.assertEqual([c["text"] for c in self.chats() if c["sender"] == "Me"], ["請檢查專案"])
        self.assertEqual(len(self.turns()), 1)


# ------------------------------------------------------------------------------------------ resume
from tests import test_cli_cleanup as cleanup  # noqa: E402
from tests import test_tui as tt  # noqa: E402


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestResumeBringsTheHistoryBack(cleanup.Base):
    """`agent resume <id>` with the real cli glue and the real TuiApp; the screen itself is replaced by a recorder that
    returns at once (the pty test below drives a real screen)."""

    def make_session(self, messages=("第一則：請看一下專案", "second 請繼續")):
        scn = {"manager": [M([final(f"回覆 {i}")]) for i, _ in enumerate(messages)], "w1": []}
        res, out, err = self.inproc([], "".join(m + "\n" for m in messages) + "/exit\n", scenario=scn)
        self.assertEqual(res, 0, out + err)
        (sid,) = self.sessions()
        return sid

    def resume_with_recorder(self, argv, stdin="/exit\n"):
        seen = []
        with create_pipe_input() as pipe:
            class Recorder(tui.TuiApp):
                def __init__(self, *a, **k):
                    super().__init__(*a, input=pipe, output=DummyOutput(), **k)
                    seen.append(self)

                def run(self):
                    self.at_start = (list(self._history), list(self.input.buffer.history.load_history_strings()))
            with mock.patch.object(cleanup.cli, "_tui_decision", return_value=(True, None)), \
                    mock.patch.object(tui, "TuiApp", Recorder):
                res, out, err = self.inproc(argv, stdin)
        return res, out, err, seen

    def test_resume_restores_the_users_earlier_messages_only(self):
        sid = self.make_session()
        n_calls, events = len(self.calls()), self.events(sid)
        res, out, err, seen = self.resume_with_recorder(["resume", sid])
        self.assertEqual(res, 0, out + err)
        (app,) = seen
        self.assertEqual(app.at_start[0], ["第一則：請看一下專案", "second 請繼續"])
        self.assertEqual(app.at_start[1], ["second 請繼續", "第一則：請看一下專案"])     # newest first inside prompt_toolkit
        self.assertNotIn("回覆", "".join(app.at_start[0]))                                  # no Manager text
        # read-only: no model call, no new turn, no new chat event, same session, lock released
        self.assertEqual(len(self.calls()), n_calls)
        new = self.events(sid)[len(events):]
        self.assertEqual([e for e in new if e.get("type") in ("chat", "turn_start", "turn_end")], [])
        self.assertEqual(self.sessions(), [sid])
        self.assertFalse(cleanup.SessionLock.is_locked(self.home / "sessions" / sid))

    def test_replay_zero_hides_the_old_chat_but_not_the_history(self):
        sid = self.make_session()
        res, out, err, seen = self.resume_with_recorder(["resume", sid, "--replay", "0"])
        self.assertEqual(res, 0, out + err)
        (app,) = seen
        self.assertEqual(app.at_start[0], ["第一則：請看一下專案", "second 請繼續"])
        self.assertEqual([e for e in app.view.entries if e.replay], [])         # nothing replayed on screen

    def test_a_fresh_session_starts_with_an_empty_history(self):
        res, out, err, seen = self.resume_with_recorder([])
        self.assertEqual(res, 0, out + err)
        self.assertEqual(seen[0].at_start[0], [])

    def test_text_that_store_redacted_is_not_restored(self):
        from agent.store import Store
        secret = "my api_key = sk_live_abcdefghijklmnop12345 please keep it"
        sid = self.make_session(messages=("ordinary question", secret, "last one"))
        saved = [e["text"] for e in Store.read_events(self.home / "sessions" / sid)
                 if e.get("type") == "chat" and e["sender"] == "Me"]
        self.assertEqual(len(saved), 3, saved)
        self.assertIn("[REDACTED]", saved[1])                                # the log never held the secret ...
        self.assertNotIn("sk_live", json.dumps(saved))
        res, out, err, seen = self.resume_with_recorder(["resume", sid])
        self.assertEqual(res, 0, out + err)
        got = seen[0].at_start[0]
        self.assertEqual(got, ["ordinary question", "last one"])           # ... and the placeholder line is not offered
        self.assertNotIn("sk_live", json.dumps(got))

    def test_a_line_that_redaction_made_unreadable_is_skipped_without_trouble(self):
        # a secret at the very end of a message: redaction eats the closing quote and the event line stops being JSON,
        # so the event reader skips it (it never reaches the history either).
        from agent.store import Store
        sid = self.make_session(messages=("before", "my api_key = sk_live_abcdefghijklmnop12345", "after"))
        got = [e["text"] for e in Store.read_events(self.home / "sessions" / sid)
               if e.get("type") == "chat" and e["sender"] == "Me"]
        self.assertNotIn("sk_live", json.dumps(got))
        res, out, err, seen = self.resume_with_recorder(["resume", sid])
        self.assertEqual(res, 0, out + err)
        self.assertEqual(seen[0].at_start[0], ["before", "after"])
        self.assertNotIn("sk_live", json.dumps(seen[0].at_start))

    def test_only_the_newest_200_of_a_long_session_are_restored(self):
        msgs = tuple(f"message number {i}" for i in range(230))
        sid = self.make_session(messages=msgs)
        res, out, err, seen = self.resume_with_recorder(["resume", sid, "--replay", "0"])
        self.assertEqual(res, 0, out + err)
        got = seen[0].at_start[0]
        self.assertEqual((len(got), got[0], got[-1]), (200, "message number 30", "message number 229"))

    def test_an_imported_session_keeps_its_fork_and_stage_when_it_is_resumed(self):
        res, out, err = self.inproc([cleanup.A, "--yes", "--here"], "/exit\n")
        self.assertEqual(res, 0, out + err)
        (sid,) = self.sessions()
        before = self.state(sid)
        res, out, err, seen = self.resume_with_recorder(["resume", sid])
        self.assertEqual(res, 0, out + err)
        after = self.state(sid)
        self.assertEqual(after["import"], before["import"])                  # fork id, stage and attempts untouched
        self.assertEqual({k: v["session_id"] for k, v in after["agents"].items()},
                         {k: v["session_id"] for k, v in before["agents"].items()})
        self.assertEqual(seen[0].at_start[0], [])
        self.assertEqual(self.calls(), [])                                   # no Fork happened, no model was called
        self.assertEqual(self.sessions(), [sid])


import os  # noqa: E402
import subprocess  # noqa: E402


@unittest.skipUnless(HAVE_PTK, "needs prompt_toolkit")
class TestHistoryOnARealScreen(tt.TestWholeRunInAPty):
    """`python -m agent resume <id> --replay 0` on a pseudo terminal (POSIX pty, or ConPTY on Windows): Up brings the earlier message into the box."""

    test_full_flow_then_resume = None            # inherited tests belong to test_tui, run them only there
    test_replay_zero_shows_no_history = None

    def test_up_after_resume_with_replay_zero_shows_the_earlier_message_and_sends_nothing(self):
        scn = {"manager": [M([final("第一輪")])], "w1": []}
        msg = "請檢查歷史輸入功能唯一標記"
        r = subprocess.run([sys.executable, "-m", "agent", "--plain"], input=msg + "\n/exit\n", capture_output=True,
                           text=True, encoding="utf-8", env=self.env(scn), cwd=self.work, timeout=90)
        self.assertEqual(r.returncode, 0, r.stderr)
        sess, ev = self.events()
        n_events = len(ev)
        term = self.spawn(["resume", sess.name, "--replay", "0"], {"manager": [], "w1": []})
        try:
            self.assertTrue(tt.until(lambda: self.ready(term), 20), bytes(term.buf[-400:]))
            time.sleep(1.0)
            self.assertNotIn(msg.encode(), bytes(term.buf))               # --replay 0: not on screen yet
            term.write(b"\x1b[A")                                         # Up
            self.assertTrue(tt.until(lambda: msg.encode() in bytes(term.buf), 10), bytes(term.buf[-400:]))
            time.sleep(0.5)
            term.write(b"\x03")                                           # Ctrl+C clears the box, sends nothing
            time.sleep(0.3)
            term.write(b"/exit\r")
            self.assertEqual(term.wait(30), 0, bytes(term.buf[-500:]))
        finally:
            if term.poll() is None:
                term.kill()
        _, ev2 = self.events()
        new = ev2[n_events:]
        self.assertEqual([e for e in new if e.get("type") in ("chat", "turn_start")], [])


if __name__ == "__main__":
    unittest.main()
