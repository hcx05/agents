"""Batch 3C: model-name formatting, symbol / colour policy, message headers and the width-aware status bar."""
import io
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import modelname  # noqa: E402
from agent.modelname import header_parts, pretty_model, shorten  # noqa: E402

try:
    import prompt_toolkit  # noqa: F401
    HAVE_PTK = True
except ImportError:
    HAVE_PTK = False

if HAVE_PTK:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import ColorDepth
    from agent.theme import ASCII, UNICODE, build_style, pick_symbols, pick_theme
    from agent.tui import ChatView, Entry, TuiApp, TuiUI, one_line, status_fragments, status_line, text_width


def rows_of(v, w=80, h=60):
    return ["".join(t for _, t in r) for r in v.window_rows(w, h)]


class TestPrettyModel(unittest.TestCase):
    def test_known_patterns(self):
        cases = {"claude-sonnet-5-5": "Claude Sonnet 5.5", "claude-opus-4-1-20250805": "Claude Opus 4.1",
                 "claude-haiku-5-5": "Claude Haiku 5.5", "claude-3-5-sonnet-20241022": "Claude Sonnet 3.5",
                 "claude-3-opus-20240229": "Claude Opus 3", "claude-sonnet-4-5-20250929[1m]": "Claude Sonnet 4.5",
                 "gemini-3-flash": "Gemini 3 Flash", "gemini-2.5-pro": "Gemini 2.5 Pro",
                 "gemini-3-flash-preview": "Gemini 3 Flash Preview"}
        for raw, want in cases.items():
            self.assertEqual(pretty_model(raw), want, raw)

    def test_unknown_ids_are_kept_and_nothing_is_invented(self):
        for raw in ("gpt-oss-120b", "sonnet", "Gemini 3 Flash", "claude-sonnet", "my-model-7", "claude-x", "opus[1m]"):
            self.assertEqual(pretty_model(raw), raw)
        self.assertEqual(pretty_model(None), "")
        self.assertEqual(pretty_model(42), "")
        self.assertEqual(pretty_model("  claude-sonnet-5-5  "), "Claude Sonnet 5.5")

    def test_shorten_is_safe(self):
        self.assertEqual(shorten("abcdef", 10), "abcdef")
        self.assertEqual(shorten("abcdefghij", 8), "abcde...")
        self.assertEqual(shorten("abcdef", 3), "abc")
        self.assertEqual(shorten("abc", 0), "")
        self.assertLessEqual(len(shorten("x" * 500, 40)), 40)


class TestHeaderEvidence(unittest.TestCase):
    def test_reported_requested_unknown_are_kept_apart(self):
        rep = header_parts("Manager", {"agent": "manager", "cli": "claude", "source": "reported",
                                       "reported": "claude-sonnet-5-5", "requested": "opus", "models": ["claude-sonnet-5-5"]})
        self.assertEqual((rep.name, rep.tag, rep.evidence), ("Claude Sonnet 5.5", "Mgr", ""))
        req = header_parts("w1", {"agent": "w1", "cli": "agy", "source": "requested", "requested": "gemini-3-flash"})
        self.assertEqual((req.name, req.tag, req.evidence), ("Gemini 3 Flash", "w1", modelname.EVIDENCE_REQUESTED))
        unk = header_parts("w2", {"agent": "w2", "cli": "agy", "source": "unknown", "requested": None})
        self.assertEqual((unk.name, unk.tag, unk.evidence), ("Antigravity", "w2", modelname.EVIDENCE_UNKNOWN))

    def test_requested_never_becomes_reported_and_unknown_never_guesses(self):
        # source says "requested" even though a stray 'reported' value exists: the source field decides
        h = header_parts("w1", {"cli": "agy", "source": "requested", "requested": "x1", "reported": "claude-opus-4-1"})
        self.assertEqual(h.evidence, modelname.EVIDENCE_REQUESTED)
        self.assertEqual(h.name, "x1")
        # unknown: only the tool's name, no model
        h = header_parts("w1", {"cli": "agy", "source": "unknown", "requested": "gemini-3-flash"})
        self.assertEqual(h.name, "Antigravity")
        self.assertNotIn("Gemini", h.name)

    def test_several_reported_models_are_flagged(self):
        h = header_parts("Manager", {"agent": "manager", "cli": "claude", "source": "reported", "reported": "claude-opus-4-1",
                                     "models": ["claude-opus-4-1", "claude-haiku-5-5"]})
        self.assertEqual(h.extra, "+1")

    def test_broken_metadata_degrades_to_unknown(self):
        for meta in (None, {}, {"weird": 1}, "str", 5, {"source": "reported"}, {"source": "reported", "reported": ""},
                     {"source": "requested", "requested": None}, {"source": "reported", "reported": 7, "models": 3}):
            h = header_parts("w9", meta)
            self.assertEqual(h.evidence, modelname.EVIDENCE_UNKNOWN, meta)
            self.assertEqual(h.tag, "w9")

    def test_unknown_cli_name_is_shown_as_it_is(self):
        self.assertEqual(header_parts("w3", {"cli": "foo", "source": "unknown"}).name, "foo")
        self.assertEqual(header_parts("w3", {"source": "unknown"}).name, "Agent")


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestSymbolPolicy(unittest.TestCase):
    def test_override_wins_over_everything(self):
        self.assertIs(pick_symbols({"AGENT_SYMBOLS": "ascii", "WT_SESSION": "x"}, "win32", "utf-8"), ASCII)
        self.assertIs(pick_symbols({"AGENT_SYMBOLS": "unicode", "TERM": "dumb"}, "linux", "ascii"), UNICODE)
        self.assertIs(pick_symbols({"AGENT_SYMBOLS": " ASCII "}, "linux", "utf-8"), ASCII)

    def test_windows_needs_a_modern_host(self):
        self.assertIs(pick_symbols({}, "win32", "utf-8"), ASCII)                        # legacy console host
        self.assertIs(pick_symbols({"WT_SESSION": "abc"}, "win32", "utf-8"), UNICODE)   # Windows Terminal
        self.assertIs(pick_symbols({"TERM_PROGRAM": "vscode"}, "win32", "utf-8"), UNICODE)
        # inherited variables of a POSIX-ish shell do not make the old console host trustworthy
        self.assertIs(pick_symbols({"TERM": "xterm-256color", "MSYSTEM": "MINGW64"}, "win32", "utf-8"), ASCII)
        self.assertIs(pick_symbols({"TERM_PROGRAM": "mintty"}, "win32", "utf-8"), ASCII)

    def test_unix_needs_utf8_and_a_real_terminal(self):
        self.assertIs(pick_symbols({"TERM": "xterm-256color"}, "linux", "utf-8"), UNICODE)
        self.assertIs(pick_symbols({"TERM": "xterm"}, "linux", "ANSI_X3.4-1968"), ASCII)
        self.assertIs(pick_symbols({"TERM": "dumb"}, "linux", "utf-8"), ASCII)
        self.assertIs(pick_symbols({"TERM": "linux"}, "linux", "utf-8"), ASCII)

    def test_bad_override_falls_back_to_auto(self):
        self.assertIs(pick_symbols({"AGENT_SYMBOLS": "fancy", "TERM": "xterm"}, "linux", "utf-8"), UNICODE)

    def test_ascii_set_really_is_ascii_and_narrow(self):
        for field in ASCII:
            self.assertTrue(field.isascii(), field)
        for field in ASCII + UNICODE:
            self.assertEqual(text_width(field), 1 if len(field) == 1 else len(field), field)

    def test_theme_choice(self):
        self.assertEqual(pick_theme({}), "dark")
        self.assertEqual(pick_theme({"AGENT_THEME": "light"}), "light")
        self.assertEqual(pick_theme({"COLORFGBG": "0;15"}), "light")
        self.assertEqual(pick_theme({"COLORFGBG": "15;0"}), "dark")
        self.assertEqual(pick_theme({"COLORFGBG": "junk"}), "dark")
        self.assertEqual(pick_theme({"AGENT_THEME": "dark", "COLORFGBG": "0;15"}), "dark")


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestStyles(unittest.TestCase):
    def test_every_depth_and_theme_builds_every_class(self):
        for depth in (ColorDepth.DEPTH_1_BIT, ColorDepth.DEPTH_4_BIT, ColorDepth.DEPTH_8_BIT, ColorDepth.DEPTH_24_BIT):
            for th in ("dark", "light"):
                st = build_style(depth, th)
                for cls in ("me", "manager", "worker", "worker2", "worker3", "system", "error", "ask", "dim", "replay",
                            "evidence", "status", "status.dot.run", "status.dot.idle", "status.hint", "title",
                            "input-title", "prompt"):
                    st.get_attrs_for_style_str(f"class:{cls}")          # must not raise

    def test_sixteen_colours_use_ansi_names_and_mono_has_no_colour(self):
        a = build_style(ColorDepth.DEPTH_4_BIT, "dark").get_attrs_for_style_str("class:manager")
        self.assertEqual(a.color, "ansicyan")
        m = build_style(ColorDepth.DEPTH_1_BIT, "dark")
        for cls in ("manager", "worker", "error", "system"):
            self.assertIn(m.get_attrs_for_style_str(f"class:{cls}").color, (None, "", "default"))
        self.assertTrue(m.get_attrs_for_style_str("class:manager").bold)

    def test_light_and_dark_differ_in_truecolour(self):
        d = build_style(ColorDepth.DEPTH_24_BIT, "dark").get_attrs_for_style_str("class:manager").color
        l = build_style(ColorDepth.DEPTH_24_BIT, "light").get_attrs_for_style_str("class:manager").color
        self.assertNotEqual(d, l)


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestHeaderLayout(unittest.TestCase):
    MGR = {"agent": "manager", "cli": "claude", "source": "reported", "reported": "claude-sonnet-5-5", "models": ["claude-sonnet-5-5"]}

    def view(self, sym=UNICODE):
        v = ChatView()
        v.symbols = sym
        return v, TuiUI(v, lambda: None)

    def test_rows_never_exceed_the_width_in_either_symbol_mode(self):
        long_meta = {"agent": "w1", "cli": "agy", "source": "reported", "reported": "m" * 300, "models": ["a", "b", "c"]}
        for sym in (UNICODE, ASCII):
            for w in (12, 14, 20, 30, 45, 80, 160):
                v, ui = self.view(sym)
                ui.echo_me("你好" * 40)
                ui.chat("Manager", "混合 mixed 中英文 text " * 6, "w1", self.MGR)
                ui.chat("w1" * 20, "x", "to" * 20, long_meta)
                ui.chat("w2", "y", None, {"agent": "w2", "cli": "agy", "source": "requested", "requested": "q" * 200})
                ui.ask_user("question " * 30)
                ui.system("系統訊息 " * 30)
                ui.error("err " * 30)
                for r in rows_of(v, w):
                    self.assertLessEqual(text_width(r), max(w, 12), (sym is ASCII, w, r))

    def test_evidence_survives_a_narrow_window(self):
        v, ui = self.view()
        ui.chat("w1", "hi", None, {"agent": "w1", "cli": "agy", "source": "unknown"})
        ui.chat("w2", "hi", None, {"agent": "w2", "cli": "agy", "source": "requested", "requested": "gemini-3-flash"})
        head = [r for r in rows_of(v, 30) if "(w" in r]
        self.assertTrue(any("模型未知" in r for r in head), head)
        self.assertTrue(any("未確認" in r for r in head), head)

    def test_clock_is_the_first_thing_to_go(self):
        v, ui = self.view()
        ui.chat("Manager", "hi", None, self.MGR)
        wide = rows_of(v, 80)[0]
        self.assertRegex(wide, r"\d\d:\d\d$")
        narrow = rows_of(v, 30)[0]
        self.assertNotRegex(narrow, r"\d\d:\d\d")
        self.assertIn("(Mgr)", narrow)

    def test_ascii_mode_has_only_ascii_decoration(self):
        v, ui = self.view(ASCII)
        ui.echo_me("你好")
        ui.chat("Manager", "派工", "w1", self.MGR)
        ui.system("sys")
        ui.error("bad")
        ui.replay_view().chat("w1", "old", None, {"agent": "w1", "cli": "agy", "source": "unknown"})
        for r in rows_of(v, 60):
            stripped = "".join(c for c in r if ord(c) > 127 and not (0x4e00 <= ord(c) <= 0x9fff))
            self.assertEqual(stripped, "", r)

    def test_unicode_mode_uses_the_role_symbols(self):
        v, ui = self.view()
        ui.echo_me("a")
        ui.chat("Manager", "b", None, self.MGR)
        ui.chat("w1", "c", None, {"agent": "w1", "cli": "agy", "source": "unknown"})
        ui.system("d")
        text = "\n".join(rows_of(v))
        for mark in ("›", "◆", "◇", "·"):
            self.assertIn(mark, text)

    def test_spacing_chat_gets_a_blank_line_system_stays_compact(self):
        v, ui = self.view()
        ui.chat("Manager", "one", None, self.MGR)
        ui.system("s1")
        ui.system("s2")
        ui.chat("Manager", "two", None, self.MGR)
        r = rows_of(v)
        self.assertEqual(r[0][:4], "  ◆ ")
        i1 = r.index("  · s1")
        self.assertEqual(r[i1 - 1], "")                    # blank line before the first system line
        self.assertEqual(r[i1 + 1], "  · s2")             # consecutive system lines are tight
        self.assertEqual(r[i1 + 2], "")
        self.assertEqual(r[0], r[0].rstrip())

    def test_each_worker_keeps_its_own_colour_class(self):
        v, ui = self.view()
        for n in ("w1", "w2", "w3", "w4"):
            ui.chat(n, "x", None, {"agent": n, "cli": "agy", "source": "unknown"})
        a = [Entry("chat", n, "x").lines(80)[0][1][0] for n in ("w1", "w1")]
        self.assertEqual(a[0], a[1])
        classes = {e._role() for e in v.entries}
        self.assertTrue(classes <= {"worker", "worker2", "worker3"})

    def test_hostile_sender_and_model_strings_are_neutralised(self):
        evil = "\x1b[31mX\x1b]52;c;QQ==\x07\u202eTXET\u2066\x00\r\ny"
        v, ui = self.view()
        ui.chat("w" + evil, "body", "t" + evil, {"agent": "w", "cli": "c" + evil, "source": "reported",
                                                   "reported": "claude-" + evil, "models": [evil, evil]})
        ui.chat("w2", "body", None, {"agent": "w2", "cli": "agy", "source": "requested", "requested": evil})
        for r in rows_of(v, 100):
            for bad in ("\x1b", "\x07", "\x00", "\r", "\u202e", "\u2066"):
                self.assertNotIn(bad, r)

    def test_one_line_is_still_the_gate_for_dynamic_text(self):
        self.assertEqual(one_line("a\u202eb\nc"), "ab c")

    def test_resize_reflow_cache_is_per_width_and_symbol_set(self):
        e = Entry("chat", "Manager", "你好" * 30, None, self.MGR)
        a = e.lines(40, UNICODE)
        self.assertIs(a, e.lines(40, UNICODE))
        self.assertIsNot(a, e.lines(41, UNICODE))
        self.assertIsNot(e.lines(41, UNICODE), e.lines(41, ASCII))


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestStatusBar(unittest.TestCase):
    SNAP = {"state": "running", "agents": [
        {"name": "Manager", "role": "manager", "running": True, "elapsed": 8.0, "quiet": 6.0, "tool": "Read", "tool_ago": 2.0,
         "task": None, "queued": 0},
        {"name": "w1", "role": "worker", "running": False, "queued": 0},
        {"name": "員工二號", "role": "worker", "running": True, "elapsed": 61.0, "quiet": 0, "tool": None, "tool_ago": None,
         "task": "t1", "queued": 2}]}

    def test_never_wider_than_the_window_and_degrades_in_order(self):
        v = ChatView()
        seen = []
        for cols in range(1, 140):
            frags = status_fragments(self.SNAP, v, cols)
            w = text_width("".join(t for _, t in frags))
            self.assertLessEqual(w, cols, cols)
            seen.append("".join(t for _, t in frags).strip())
        full = status_line(self.SNAP, v, 200)
        self.assertIn("● Mgr 執行中 8s", full)
        self.assertIn("工具 Read", full)
        self.assertIn("任務 t1", full)
        self.assertIn("○ w1 閒置", full)
        # the running manager's name survives to a very narrow bar
        self.assertTrue(any(s.startswith("● Mgr") for s in seen if len(s) <= 12 and s))

    def test_urgent_notices_outlive_minor_ones(self):
        v = ChatView()
        snap = {"state": "waiting_worker", "hold": True, "agents": [{"name": "Manager", "role": "manager", "running": False,
                                                                         "queued": 0}]}
        s = status_line(snap, v, 200)
        self.assertIn("已暫停", s)
        self.assertNotIn("Manager 等待 Worker", s)                    # not shown while on hold
        snap2 = {"state": "waiting_worker", "agents": snap["agents"]}
        self.assertIn("Manager 等待 Worker", status_line(snap2, v, 200))
        self.assertNotIn("Manager 等待 Worker", status_line(snap2, v, 24))
        v.scroll, v.unseen = 4, 2
        self.assertIn("2 則新訊息", status_line({"agents": snap["agents"]}, v, 200))

    def test_cut_never_lets_later_pieces_slip_in(self):
        v = ChatView()
        v.scroll, v.unseen = 3, 1
        snap = {"hold": True, "agents": [{"name": "Manager", "role": "manager", "running": False, "queued": 0}]}
        for cols in range(3, 60):
            s = status_line(snap, v, cols)
            self.assertLessEqual(text_width(s), cols)

    def test_cjk_names_are_cut_by_cells_not_characters(self):
        v = ChatView()
        v.symbols = UNICODE
        e = Entry("chat", "w1", "x", None, {"agent": "w1", "cli": "agy", "source": "reported", "reported": "模型" * 30})
        for w in range(12, 60):
            for r in e.lines(w, UNICODE):
                self.assertLessEqual(text_width("".join(t for _, t in r)), w, (w, r))

    def test_garbage_snapshots_do_not_raise(self):
        v = ChatView()
        for snap in ({}, {"agents": None}, {"agents": [None, 3, "x"]}, {"agents": [{}]}, {"agents": [{"running": True}]},
                     {"agents": [{"name": None, "running": True, "elapsed": None}]}):
            status_fragments(snap, v, 40)
        self.assertEqual(status_line({}, v, 40), "準備中")

    def test_ascii_dots(self):
        s = status_line(self.SNAP, ChatView(), 200, ASCII)
        self.assertTrue(s.isascii() or all(ord(c) < 128 or 0x4e00 <= ord(c) <= 0x9fff or c in "，（）：" for c in s), s)
        self.assertIn("* Mgr", s)
        self.assertIn("o w1", s)


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestAppUsesTheTheme(unittest.TestCase):
    def run_app(self, **kw):
        buf = io.StringIO()
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.output.vt100 import Vt100_Output
        with create_pipe_input() as pipe:
            out = Vt100_Output(buf, lambda: Size(14, 70), term="xterm-256color")
            snap = TestStatusBar.SNAP
            app = TuiApp(lambda s: None, lambda: snap, lambda: False, input=pipe, output=out, **kw)
            app.ui.echo_me("你好")
            app.ui.chat("Manager", "派工", "w1", TestHeaderLayout.MGR)
            t = threading.Thread(target=app.run, daemon=True)
            t.start()
            end = time.monotonic() + 5
            while time.monotonic() < end and not (app.app.is_running and getattr(app.app, "loop", None)):
                time.sleep(0.02)
            time.sleep(0.4)
            app.request_exit()
            t.join(5)
        return buf.getvalue()

    def test_renders_with_each_symbol_set_and_depth(self):
        for sym in (UNICODE, ASCII):
            for depth in (ColorDepth.DEPTH_1_BIT, ColorDepth.DEPTH_4_BIT, ColorDepth.DEPTH_8_BIT, ColorDepth.DEPTH_24_BIT):
                raw = self.run_app(symbols=sym, color_depth=depth)
                self.assertIn("Claude Sonnet 5.5", raw)
                self.assertIn("Mgr", raw)
                if sym is ASCII:
                    self.assertNotIn("◆", raw)
                else:
                    self.assertIn("◆", raw)
                if depth == ColorDepth.DEPTH_1_BIT:
                    self.assertNotRegex(raw, r"\x1b\[[0-9;]*3[0-7]m|38;5;|38;2;")


if __name__ == "__main__":
    unittest.main()
