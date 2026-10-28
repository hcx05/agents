"""Fault injection for `_run_chat`: whatever fails after the Session lock is taken (screen setup, banner, replay, the
Engine already running), the Engine is stopped, the lock is let go exactly once, and the SAME session can be resumed
afterwards without any model call, new Fork or new Session id. Fake CLI, in-process, plain UI unless stated."""
import contextlib
import io
import json
import os
import sys
import threading
import unittest
from unittest import mock

from tests import test_import_cli as base
from tests.rig import M, final

from agent import cli
from agent.sessionlock import SessionLock
from agent.ui import ChatUI

A = base.A


def engine_threads():
    return [t for t in threading.enumerate() if t.name == "engine" and t.is_alive()]


class Boom(Exception):
    pass


class Base(base.Base):
    def inproc(self, argv, stdin="/exit\n", scenario=None):
        """Run the CLI in this process. Returns (exit code or raised exception, stdout, stderr)."""
        (self.tmp / "scn.json").write_text(json.dumps(scenario or {"manager": [], "w1": []}, ensure_ascii=False),
                                           encoding="utf-8")
        env = dict(AGENT_HOME=str(self.home), FAKE_SCENARIO=str(self.tmp / "scn.json"), FAKE_STATE=str(self.fs),
                   CLAUDE_CONFIG_DIR=str(self.claude), PYTHONPATH=str(base.ROOT))
        out, err = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.work)
        try:
            with mock.patch.dict(os.environ, env), \
                    mock.patch.dict(os.environ, {"AGENT_PLAIN": "1"}), \
                    mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
                args = cli.build_parser().parse_args(cli.rewrite_argv(argv))
                try:
                    res = {"import": cli.cmd_import, "resume": cli.cmd_resume}.get(args.command, cli.cmd_chat)(args)
                except Boom as e:
                    res = e
        finally:
            os.chdir(cwd)
        return res, out.getvalue(), err.getvalue()

    def session_dir(self):
        (sid,) = self.sessions()
        return self.home / "sessions" / sid

    def events(self, sid):
        p = self.home / "sessions" / sid / "events.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def assert_clean(self, sid):
        d = self.home / "sessions" / sid
        self.assertEqual(engine_threads(), [], "engine thread still running")
        self.assertFalse(SessionLock.is_locked(d), "session lock still held")
        self.assertEqual(self.calls(), [], "a model was called")
        self.assertEqual([e for e in self.events(sid) if e.get("type") in ("turn_start", "turn_done")], [])

    def assert_resumable(self, sid, calls_before=0):
        before = len([e for e in self.events(sid) if e.get("type") == "chat"])
        res, out, err = self.inproc(["resume", sid])
        self.assertEqual(res, 0, out + err)
        self.assertIn("Bye. Session saved", out)
        self.assertEqual(len(self.calls()), calls_before, "resume must not call the model")
        self.assertFalse(SessionLock.is_locked(self.home / "sessions" / sid))
        self.assertEqual(len([e for e in self.events(sid) if e.get("type") == "chat"]), before)


class TestFreshStartFailures(Base):
    def test_banner_failure_stops_engine_and_frees_the_lock(self):
        with mock.patch.object(ChatUI, "banner", side_effect=Boom("banner")):
            res, out, err = self.inproc([])
        self.assertIsInstance(res, Boom)
        (sid,) = self.sessions()
        self.assert_clean(sid)
        self.assertIn("Bye. Session saved", out)               # the user is still told where the session is
        self.assert_resumable(sid)

    def test_a_failure_after_the_engine_started_is_cleaned_up(self):
        from agent.orchestrator import Engine
        with mock.patch.object(Engine, "check_cli_versions", side_effect=Boom("late")):
            res, out, err = self.inproc([])
        self.assertIsInstance(res, Boom)
        (sid,) = self.sessions()
        self.assert_clean(sid)
        self.assert_resumable(sid)

    def test_engine_construction_failure_frees_the_lock_without_waiting_for_a_thread_that_never_started(self):
        from agent.orchestrator import Engine
        with mock.patch.object(Engine, "__init__", side_effect=Boom("ctor")):
            res, out, err = self.inproc([])
        self.assertIsInstance(res, Boom)
        (sid,) = self.sessions()
        self.assertFalse(SessionLock.is_locked(self.home / "sessions" / sid))
        self.assertEqual(engine_threads(), [])
        self.assertNotIn("Bye.", out)                          # nothing ran, nothing to say

    def test_engine_start_failure_frees_the_lock(self):
        from agent.orchestrator import Engine
        with mock.patch.object(Engine, "start", side_effect=Boom("start")):
            res, out, err = self.inproc([])
        self.assertIsInstance(res, Boom)
        (sid,) = self.sessions()
        self.assertFalse(SessionLock.is_locked(self.home / "sessions" / sid))

    def test_an_error_while_the_normal_exit_message_is_printed_still_frees_the_lock(self):
        # shutdown itself raising must not skip lock.release()
        from agent.orchestrator import Engine
        real = Engine.shutdown

        def bad(self):
            real(self)
            raise Boom("shutdown")
        with mock.patch.object(Engine, "shutdown", bad):
            res, out, err = self.inproc([])
        self.assertIsInstance(res, Boom)
        (sid,) = self.sessions()
        self.assertFalse(SessionLock.is_locked(self.home / "sessions" / sid))
        self.assertEqual(engine_threads(), [])

    def test_normal_run_is_unchanged(self):
        res, out, err = self.inproc([], "/exit\n")
        self.assertEqual(res, 0, out + err)
        (sid,) = self.sessions()
        self.assert_clean(sid)


class TestResumeFailures(Base):
    def make_session(self):
        scn = {"manager": [M([final("第一輪完成")])], "w1": []}
        res, out, err = self.inproc([], "hi\n/exit\n", scenario=scn)
        self.assertEqual(res, 0, out + err)
        (sid,) = self.sessions()
        return sid

    def test_replay_failure_cleans_up_and_the_session_resumes_afterwards(self):
        sid = self.make_session()
        n_calls, n_events = len(self.calls()), len(self.events(sid))
        with mock.patch.object(cli, "replay", side_effect=Boom("replay")):
            res, out, err = self.inproc(["resume", sid])
        self.assertIsInstance(res, Boom)
        self.assertEqual(engine_threads(), [])
        self.assertFalse(SessionLock.is_locked(self.home / "sessions" / sid))
        self.assertEqual(len(self.calls()), n_calls)
        self.assertEqual(self.sessions(), [sid])               # no second session was created
        self.assert_resumable(sid, calls_before=n_calls)
        self.assertGreaterEqual(len(self.events(sid)), n_events)

    def test_banner_failure_on_resume_keeps_the_saved_manager_session_id(self):
        sid = self.make_session()
        before = json.loads((self.home / "sessions" / sid / "state.json").read_text(encoding="utf-8"))
        with mock.patch.object(ChatUI, "banner", side_effect=Boom("banner")):
            res, out, err = self.inproc(["resume", sid])
        self.assertIsInstance(res, Boom)
        after = json.loads((self.home / "sessions" / sid / "state.json").read_text(encoding="utf-8"))
        self.assertEqual({k: v["session_id"] for k, v in before["agents"].items()},
                         {k: v["session_id"] for k, v in after["agents"].items()})
        self.assertFalse(SessionLock.is_locked(self.home / "sessions" / sid))


class TestImportFailures(Base):
    def test_failure_during_import_startup_leaves_the_prepared_fork_untouched(self):
        with mock.patch.object(ChatUI, "banner", side_effect=Boom("banner")):
            res, out, err = self.inproc([A, "--yes", "--here"])
        self.assertIsInstance(res, Boom)
        (sid,) = self.sessions()
        imp = self.state(sid)["import"]
        self.assertEqual((imp["source_id"], imp["stage"], imp["attempts"]), (A, "prepared", 0))
        fork = imp["fork_id"]
        self.assert_clean(sid)
        # resume: same session, same fork id, still no model call and no Fork yet (first message creates it)
        res, out, err = self.inproc(["resume", sid])
        self.assertEqual(res, 0, out + err)
        imp2 = self.state(sid)["import"]
        self.assertEqual((imp2["fork_id"], imp2["stage"], imp2["source_id"]), (fork, "prepared", A))
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.sessions(), [sid])
        self.assertFalse(SessionLock.is_locked(self.home / "sessions" / sid))


try:
    import prompt_toolkit  # noqa: F401
    HAVE_PTK = True
except ImportError:
    HAVE_PTK = False


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
class TestScreenFailure(Base):
    def test_the_screen_dying_reports_clearly_and_leaves_a_resumable_session(self):
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from agent import tui

        with create_pipe_input() as pipe:
            class Dies(tui.TuiApp):
                def __init__(self, *a, **k):
                    super().__init__(*a, input=pipe, output=DummyOutput())

                def run(self):
                    raise Boom("screen died")
            with mock.patch.object(cli, "_tui_decision", return_value=(True, None)), \
                    mock.patch.object(tui, "TuiApp", Dies):
                res, out, err = self.inproc([])
        self.assertEqual(res, 1)
        self.assertIn("screen died", err)
        self.assertIn("agent resume", err)
        (sid,) = self.sessions()
        self.assert_clean(sid)
        self.assert_resumable(sid)

    def test_a_failure_while_setting_up_the_replay_in_full_screen_mode_is_cleaned_up_too(self):
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from agent import tui
        res, out, err = self.inproc([])
        self.assertEqual(res, 0, out + err)
        (sid,) = self.sessions()
        with create_pipe_input() as pipe:
            class App(tui.TuiApp):
                def __init__(self, *a, **k):
                    super().__init__(*a, input=pipe, output=DummyOutput())
            with mock.patch.object(cli, "_tui_decision", return_value=(True, None)), \
                    mock.patch.object(tui, "TuiApp", App), \
                    mock.patch.object(cli, "replay", side_effect=Boom("replay")):
                res, out, err = self.inproc(["resume", sid])
        self.assertIsInstance(res, Boom)
        self.assertEqual(engine_threads(), [])
        self.assertFalse(SessionLock.is_locked(self.home / "sessions" / sid))
        self.assert_resumable(sid)


if __name__ == "__main__":
    unittest.main()
