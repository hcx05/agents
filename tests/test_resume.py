"""Agent Session persistence / resume / chat replay / lock. Fake CLI only: nothing here talks to a real Claude or agy."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import cli, resume  # noqa: E402
from agent.config import parse_config  # noqa: E402
from agent.orchestrator import State  # noqa: E402
from agent.sessionlock import LOCK_NAME, SessionLock, SessionLocked  # noqa: E402
from agent.store import STATE_VERSION, Store, StoreError  # noqa: E402
from agent.ui import BufferUI  # noqa: E402
from tests.rig import Rig, M, W, answer, ask, dispatch, final  # noqa: E402


class Base(unittest.TestCase):
    rig: Rig

    def tearDown(self):
        if getattr(self, "rig", None):
            self.rig.close()

    def flag(self, name):
        (self.rig.cwd / name).write_text("go", encoding="utf-8")

    def running(self, agent):
        return self.rig.wait_until(lambda: self.rig.engine.agents[agent].running is not None)

    def state_json(self, d=None):
        return json.loads(((d or self.rig.store.dir) / "state.json").read_text(encoding="utf-8"))

    def report_text(self):
        return "\n".join(t for _, t in self.rig.engine.restore_report)


class TestPersistAndResume(Base):
    def test_state_v2_has_sessions_preamble_config_and_cwd(self):
        self.rig = r = Rig({"manager": [M([dispatch("w1", "t1", "做")]), M([final("好")])],
                            "w1": [W("done", "t1", "完成")]})
        r.say("開始")
        st = self.state_json()
        self.assertEqual(st["version"], STATE_VERSION)
        self.assertEqual(st["cwd"], str(r.cwd))
        for aid in ("manager", "w1"):
            self.assertTrue(st["agents"][aid]["session_id"])
            self.assertIs(st["agents"][aid]["preamble_sent"], True)
        self.assertEqual(st["config"]["agents"]["w1"]["cli"], "fake")
        self.assertEqual(st["tasks"]["t1"]["status"], "done")

    def test_resume_keeps_manager_and_worker_sessions_and_never_resends_preamble(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "第一件")]), M([final("一完成")]),
                        M([dispatch("w1", "t2", "第二件")]), M([final("二完成")])],
            "w1": [W("done", "t1", "r1"), W("done", "t2", "r2")]})
        r.say("先做第一件")
        before = self.state_json()
        r.resume()
        e = r.engine
        self.assertEqual({a.id: a.session_id for a in e.agents.values()},
                         {"manager": before["agents"]["manager"]["session_id"], "w1": before["agents"]["w1"]["session_id"]})
        self.assertTrue(all(a.preamble_sent for a in e.agents.values()))
        self.assertFalse(e.hold)
        r.say("再做第二件")
        m_calls, w_calls = r.calls("manager"), r.calls("w1")
        self.assertEqual(len(m_calls), 4)
        self.assertTrue(m_calls[2]["resume"] and w_calls[1]["resume"])            # same CLI sessions continued
        self.assertEqual(m_calls[2]["session"], m_calls[0]["session"])
        self.assertEqual(w_calls[1]["session"], w_calls[0]["session"])
        self.assertNotIn("You are the MANAGER", m_calls[2]["prompt"])             # preamble not sent a second time
        self.assertNotIn('You are worker "w1"', w_calls[1]["prompt"])
        self.assertIn("You are the MANAGER", m_calls[0]["prompt"])                # ...but it was sent the first time
        self.assertEqual(r.engine.state, State.DONE)
        self.assertEqual(r.events("illegal_transition"), [])

    def test_first_turn_that_never_succeeded_does_not_count_as_preamble_sent(self):
        self.rig = r = Rig({"manager": [{"exit": 3, "stderr": "boom"}, M([final("好")])], "w1": []})
        r.say("hi")                                                                # turn 1 fails (exit 3)
        self.assertIs(self.state_json()["agents"]["manager"]["preamble_sent"], False)
        r.resume()
        r.say("再試")
        c = r.calls("manager")
        self.assertIn("You are the MANAGER", c[1]["prompt"])                       # preamble goes out again
        self.assertFalse(c[1]["resume"])                                           # on a fresh CLI session

    def test_resume_works_when_nothing_ever_ran(self):
        self.rig = r = Rig({"manager": [M([final("好")])], "w1": []})
        r.resume()
        self.assertEqual(r.engine.state, State.IDLE)
        r.say("hi")
        self.assertEqual(r.chat_lines(), ["Manager> @Me 好"])

    def test_state_shape_is_valid_json_after_every_event(self):
        self.rig = r = Rig({"manager": [M([ask("可以嗎？")]), M([final("好")])], "w1": []})
        r.say("做")
        self.assertEqual(self.state_json()["awaiting_user"]["text"], "可以嗎？")


class TestLegacyAndDamagedSessions(Base):
    def test_version1_state_is_upgraded_safely(self):
        self.rig = r = Rig({"manager": [M([dispatch("w1", "t1", "做")]), M([final("好")]), M([final("再好")])],
                            "w1": [W("done", "t1", "完成")]})
        r.say("開始")
        r.engine.shutdown()                              # a clean exit writes the final state; THEN make it look old
        st = self.state_json()
        # rewrite as the old format: no version / config / mail / preamble_sent
        st["version"] = 1
        for k in ("config", "mail"):
            st.pop(k, None)
        for a in st["agents"].values():
            a.pop("preamble_sent", None)
            a.pop("turns", None)
            a.pop("last_turn", None)
        (r.store.dir / "state.json").write_text(json.dumps(st), encoding="utf-8")
        r.resume()
        self.assertEqual(r.store.state["version"], STATE_VERSION)
        self.assertTrue(r.engine.mgr.preamble_sent)                 # inferred from the successful turn_end events
        self.assertTrue(r.engine.agents["w1"].preamble_sent)
        self.assertIn("舊版", self.report_text())
        r.say("再來")
        self.assertNotIn("You are the MANAGER", r.calls("manager")[-1]["prompt"])

    def test_version1_with_unfinished_task_marks_it_interrupted_not_done(self):
        self.rig = r = Rig({"manager": [M([dispatch("w1", "t1", "做")]), M([final("好")])],
                            "w1": [W("done", "t1", "完成", wait_for_file="w.flag")]})
        r.engine.submit("開始")
        self.assertTrue(self.running("w1"))
        crash = r.crash_copy()
        st = json.loads((crash / "state.json").read_text(encoding="utf-8"))
        st["version"] = 1
        st.pop("mail", None)
        (crash / "state.json").write_text(json.dumps(st), encoding="utf-8")
        r.engine.shutdown()
        r.resume(crash)
        self.assertEqual(r.store.state["tasks"]["t1"]["status"], "interrupted")
        self.assertIn("t1", self.report_text())

    def test_session_not_found_and_unreadable_state(self):
        root = Path(tempfile.mkdtemp(prefix="agent store "))
        self.addCleanup(shutil.rmtree, root, True)
        with self.assertRaises(StoreError):
            Store.open_existing(root, "nope")
        d = root / "bad"
        d.mkdir()
        (d / "state.json").write_text("{ not json", encoding="utf-8")
        with self.assertRaises(StoreError) as cm:
            Store.open_existing(root, "bad")
        self.assertIn("unreadable", str(cm.exception))
        self.assertEqual((d / "state.json").read_text(encoding="utf-8"), "{ not json")   # never overwritten
        (d / "state.json").write_text(json.dumps({"version": 99}), encoding="utf-8")
        with self.assertRaises(StoreError):
            Store.open_existing(root, "bad")                                            # written by a newer agent

    def test_truncated_last_event_line_and_garbage_do_not_break_resume(self):
        self.rig = r = Rig({"manager": [M([final("好")]), M([final("又好")])], "w1": []})
        r.say("第一句")
        with open(r.store.events_path, "ab") as f:
            f.write(b'\n\nnot json at all\n{"seq": 999, "type": "chat", "sender": "Me", "text": "cut o')   # no newline
        r.resume()
        r.say("第二句")
        events = Store.read_events(r.store.dir)
        texts = [e.get("text") for e in events if e.get("type") == "chat"]
        self.assertEqual(texts, ["第一句", "好", "第二句", "又好"])         # new records are intact, not glued to the stub
        self.assertEqual(len({e["seq"] for e in events if "seq" in e}), len([e for e in events if "seq" in e]))
        items = resume.timeline(events)
        self.assertEqual([i["text"] for i in items if i["kind"] == "chat"], texts)

    def test_session_without_ui_events_replays_from_chat_only(self):
        self.rig = r = Rig({"manager": [M([final("好")])], "w1": []})
        r.say("你好")
        events = [e for e in Store.read_events(r.store.dir) if e.get("type") != "ui"]      # an old session
        items = resume.timeline(events)
        self.assertEqual([(i["sender"], i["text"]) for i in items if i["kind"] == "chat"],
                         [("Me", "你好"), ("Manager", "好")])


class TestInterruptedWorkAndQueues(Base):
    def test_running_worker_task_becomes_interrupted_and_is_not_rerun(self):
        self.rig = r = Rig({"manager": [M([dispatch("w1", "t1", "危險的事")]), M([final("知道了")])],
                            "w1": [W("done", "t1", "完成", wait_for_file="w.flag")]})
        r.engine.submit("開始")
        self.assertTrue(self.running("w1"))
        crash = r.crash_copy()                         # the process dies here
        r.engine.shutdown()
        r.resume(crash)
        e = r.engine
        self.assertEqual(r.store.state["tasks"]["t1"]["status"], "interrupted")     # not done, not silently dropped
        self.assertFalse(e.agents["w1"].inflight)
        self.assertIsNone(e.agents["w1"].active_task)
        self.assertIn("t1", self.report_text())
        self.assertIn("interrupted", self.report_text())
        self.assertIn("不會自動重跑", self.report_text())
        self.assertTrue(any("t1" in n and "interrupted" in n for n in e.pending_notes))
        n_w = len(r.calls("w1"))
        e.release()
        self.assertTrue(e.wait_idle(5))
        self.assertEqual(len(r.calls("w1")), n_w)       # the worker was NOT started again
        r.say("現在狀況如何？")
        p = r.calls("manager")[-1]["prompt"]            # the Manager is told, with the user's next message
        self.assertIn("interrupted", p)
        self.assertIn("NOT rolled back", p)

    def test_user_message_being_processed_is_reported_for_resend_and_queued_one_is_kept(self):
        self.rig = r = Rig({"manager": [M([final("A 完成")], wait_for_file="m.flag"), M([final("B 完成")])], "w1": []})
        r.engine.submit("訊息A")
        self.assertTrue(self.running("manager"))
        r.engine.submit("訊息B（排隊中）")
        self.assertTrue(r.wait_until(lambda: any("queued" in t for t in r.ui.texts("system"))))
        crash = r.crash_copy()
        r.engine.shutdown()
        r.resume(crash)
        e = r.engine
        rep = self.report_text()
        self.assertIn("訊息A", rep)                                     # in flight: the user must resend if needed
        self.assertIn("結果未知", rep)
        self.assertIn("訊息B（排隊中）", rep)                           # queued: kept, shown
        self.assertEqual([m["text"] for m in e.mgr.inbox], ["訊息B（排隊中）"])
        self.assertTrue(e.hold)
        self.assertTrue(e.is_quiescent())                               # on hold counts as "not running"
        self.assertEqual(len(r.calls("manager")), 1)                    # nothing started by itself
        self.assertIn("on hold", e.status_text())
        e.release()
        self.assertTrue(e.wait_idle(30))
        p = r.calls("manager")[-1]["prompt"]
        self.assertIn("訊息B（排隊中）", p)
        self.assertIn("UNKNOWN", p)                                      # the Manager knows the earlier turn's fate is unknown
        self.assertEqual(r.chat_lines()[-1], "Manager> @Me B 完成")

    def test_typing_after_resume_releases_hold_and_keeps_order(self):
        self.rig = r = Rig({"manager": [M([final("x")], wait_for_file="m.flag"), M([final("全部處理")])], "w1": []})
        r.engine.submit("A")
        self.assertTrue(self.running("manager"))
        r.engine.submit("B")
        self.assertTrue(r.wait_until(lambda: any("queued" in t for t in r.ui.texts("system"))))
        crash = r.crash_copy()
        r.engine.shutdown()
        r.resume(crash)
        r.say("C（新）")
        p = r.calls("manager")[-1]["prompt"]
        self.assertLess(p.index("B"), p.index("C（新）"))              # restored mail first, the new message behind it
        self.assertFalse(r.engine.hold)

    def test_interrupt_discards_held_items_and_says_so(self):
        self.rig = r = Rig({"manager": [M([final("x")], wait_for_file="m.flag")], "w1": []})
        r.engine.submit("A")
        self.assertTrue(self.running("manager"))
        r.engine.submit("B")
        self.assertTrue(r.wait_until(lambda: any("queued" in t for t in r.ui.texts("system"))))
        crash = r.crash_copy()
        r.engine.shutdown()
        r.resume(crash)
        r.engine.interrupt()
        self.assertTrue(r.engine.wait_idle(30))
        self.assertFalse(r.engine.hold)
        self.assertTrue(any("Discarded 1 queued message" in t for t in r.ui.texts("system")))
        self.assertEqual(len(r.calls("manager")), 1)

    def test_ask_user_question_and_held_worker_report_survive_a_restart(self):
        self.rig = r = Rig({
            "manager": [M([dispatch("w1", "t1", "工作")]), M([ask("要繼續嗎？")], wait_for_file="m2.flag"),
                        M([final("全部處理完")])],
            "w1": [W("done", "t1", "worker 結果", wait_for_file="w.flag")]})
        r.engine.submit("開始")
        self.assertTrue(self.running("w1"))
        r.engine.submit("追加")                                            # manager turn 2 -> ask_user
        self.assertTrue(r.wait_until(lambda: len(r.calls("manager")) == 2))
        self.flag("w.flag")                                                # worker report arrives meanwhile
        self.assertTrue(r.wait_until(lambda: r.store.state["tasks"]["t1"]["status"] == "done"))
        self.flag("m2.flag")
        self.assertTrue(r.wait_until(lambda: r.engine.state == State.WAITING_USER))
        r.resume()
        e = r.engine
        self.assertEqual(e.state, State.WAITING_USER)
        self.assertEqual(e.awaiting_user["text"], "要繼續嗎？")
        self.assertIn(("ask_user", "要繼續嗎？"), e.restore_report)         # the question is shown again
        self.assertEqual(len(e.held), 1)
        self.assertEqual(len(r.calls("manager")), 2)                         # nobody woke the Manager up
        r.say("繼續")
        p = r.calls("manager")[-1]["prompt"]
        self.assertLess(p.index("[user answer to your ask_user]"), p.index("worker 結果"))
        self.assertEqual(r.engine.state, State.DONE)

    def test_pending_notes_survive_and_are_delivered_once(self):
        self.rig = r = Rig({"manager": [M([final("a")]), M([final("b")])], "w1": []})
        r.say("hi")
        r.engine.pending_notes.append("NOTE-XYZ for the manager")
        r.engine._persist()
        r.resume()
        r.say("next")
        self.assertIn("NOTE-XYZ", r.calls("manager")[-1]["prompt"])
        self.assertEqual(r.engine.pending_notes, [])

    def test_repair_retry_in_flight_is_restored(self):
        self.rig = r = Rig({"manager": [{"reply": {"say": "", "actions": "oops"}}, M([final("修好了")])], "w1": []})
        r.say("hi")
        self.assertEqual(r.chat_lines()[-1], "Manager> @Me 修好了")


LOCK_WORKER = Path(__file__).with_name("lock_worker.py")


def spawn_workers(session_dir, n, tmp, hold_sec=1.5, extra=()):
    """n real OS processes that all try to take the same session lock at the same instant (released by a flag file)."""
    go, done = tmp / "go.flag", tmp / "done.flag"
    for f in (go, done):
        if f.exists():
            f.unlink()
    procs = [subprocess.Popen([sys.executable, str(LOCK_WORKER), str(session_dir), str(go), str(done), str(hold_sec), *extra],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(n)]
    time.sleep(1.0 + 0.1 * n)                       # every worker has imported and is spinning on the go flag
    go.write_text("go")
    return procs, done


def reap(procs):
    for p in procs:
        p.wait(timeout=30)
        for pipe in (p.stdout, p.stderr):
            if pipe:
                pipe.close()


class TestLockFile(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="agent lock "))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_second_writer_is_refused_and_release_frees_it(self):
        a, b = SessionLock(self.dir), SessionLock(self.dir)
        a.acquire()
        with self.assertRaises(SessionLocked) as cm:
            b.acquire()
        self.assertIn("in use by another agent process", str(cm.exception))
        self.assertTrue(SessionLock.is_locked(self.dir))
        a.release()
        self.assertFalse(SessionLock.is_locked(self.dir))
        b.acquire()
        b.release()

    def test_lock_file_is_never_deleted_by_release_or_takeover(self):
        a = SessionLock(self.dir)
        a.acquire()
        a.release()
        self.assertTrue((self.dir / LOCK_NAME).exists())             # nobody unlinks it, so nobody can unlink a foreign one
        b = SessionLock(self.dir)
        b.acquire()
        inode = (self.dir / LOCK_NAME).stat().st_ino
        b.release()
        self.assertEqual((self.dir / LOCK_NAME).stat().st_ino, inode)

    def test_release_of_a_lock_we_do_not_hold_touches_nothing(self):
        a, stranger = SessionLock(self.dir), SessionLock(self.dir)
        a.acquire()
        info = a.read()
        stranger.release()                                           # never acquired
        with self.assertRaises(SessionLocked):
            stranger.acquire()                                       # refused ...
        stranger.release()                                           # ... and releasing the refused attempt is harmless
        self.assertEqual(a.read(), info)                             # owner info intact
        with self.assertRaises(SessionLocked):
            SessionLock(self.dir).acquire()                          # a still holds it
        a.release()
        a.release()                                                  # double release is a no-op

    def test_same_process_cannot_take_it_twice(self):
        a = SessionLock(self.dir)
        a.acquire()
        with self.assertRaises(SessionLocked):
            a.acquire()
        a.release()

    def test_owner_info_is_readable_while_locked_and_cleared_after(self):
        a = SessionLock(self.dir)
        a.acquire()
        info = SessionLock(self.dir).read()
        self.assertEqual((info["pid"], info["token"]), (os.getpid(), a.token))
        a.release()
        self.assertIsNone(SessionLock(self.dir).read())

    def test_killed_owner_leaves_no_stale_lock(self):
        procs, done = spawn_workers(self.dir, 1, self.dir, hold_sec=60)
        out = procs[0].stdout.readline().strip()
        self.assertEqual(out, "WON")
        self.assertTrue(SessionLock.is_locked(self.dir))
        procs[0].kill()                                              # hard kill: no release code runs
        reap(procs)
        self.assertFalse(SessionLock.is_locked(self.dir))            # the OS dropped the lock with the process
        a = SessionLock(self.dir)
        a.acquire()                                                  # the leftover text is just ignored
        a.release()

    def test_exactly_one_of_many_simultaneous_processes_wins_a_fresh_lock(self):
        for rnd in range(3):
            d = self.dir / f"fresh{rnd}"
            d.mkdir()
            procs, done = spawn_workers(d, 8, self.dir)
            outs = []
            for p in procs:
                outs.append(p.stdout.readline().strip())
            done.write_text("done")
            reap(procs)
            self.assertEqual(outs.count("WON"), 1, (rnd, outs))
            self.assertEqual(outs.count("LOST"), 7, (rnd, outs))

    def test_exactly_one_wins_when_the_previous_owner_just_died(self):
        """The former takeover race: several processes meet a dead owner's leftovers at the same moment."""
        for rnd in range(3):
            d = self.dir / f"stale{rnd}"
            d.mkdir()
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait()
            (d / LOCK_NAME).write_text(json.dumps({"pid": dead.pid, "host": __import__("socket").gethostname(),
                                                   "token": "old", "created": 1}), encoding="utf-8")
            os.utime(d / LOCK_NAME, (1, 1))
            procs, done = spawn_workers(d, 8, self.dir)
            outs = [p.stdout.readline().strip() for p in procs]
            done.write_text("done")
            reap(procs)
            self.assertEqual(outs.count("WON"), 1, (rnd, outs))

    def test_a_loser_never_disturbs_the_winner(self):
        procs, done = spawn_workers(self.dir, 4, self.dir, hold_sec=30)
        outs = [p.stdout.readline().strip() for p in procs]
        self.assertEqual(outs.count("WON"), 1)
        winner = procs[outs.index("WON")]
        self.assertIsNone(winner.poll())
        self.assertTrue(SessionLock.is_locked(self.dir))
        info = SessionLock(self.dir).read()
        self.assertEqual(info["pid"], winner.pid)                    # losers neither deleted nor rewrote the owner's info
        done.write_text("done")
        reap(procs)
        self.assertFalse(SessionLock.is_locked(self.dir))

    def test_is_locked_creates_nothing(self):
        d = self.dir / "never"
        d.mkdir()
        self.assertFalse(SessionLock.is_locked(d))
        self.assertFalse((d / LOCK_NAME).exists())


class TestSessionIdValidation(unittest.TestCase):
    BAD = ["..", ".", "../x", "..\\x", "a/b", "a\\b", "/etc/passwd", "\\\\server\\share", "C:\\Windows", "C:", "c:x",
           "", " ", " abc", "abc ", "a b", "x\x00y", "a" * 101, "con.", ".hidden", "-rf", "é", "a:b", "a*b", "a?b", "~", "$HOME"]

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="agent sessions "))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()

    def test_bad_ids_are_refused_without_touching_the_disk(self):
        from agent.store import safe_session_dir
        for bad in self.BAD:
            with self.assertRaises(StoreError, msg=repr(bad)):
                safe_session_dir(self.sessions, bad)
            with self.assertRaises(StoreError, msg=repr(bad)):
                Store.open_existing(self.sessions, bad)
        self.assertEqual(list(self.root.iterdir()), [self.sessions])
        self.assertEqual(list(self.sessions.iterdir()), [])

    def test_normal_ids_pass(self):
        from agent.store import safe_session_dir
        for ok in ("20261028-131316-be0f76", "abc", "A_b-1"):
            self.assertEqual(safe_session_dir(self.sessions, ok), self.sessions / ok)

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_a_link_leading_outside_the_sessions_folder_is_refused(self):
        from agent.store import safe_session_dir
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "state.json").write_text("{}", encoding="utf-8")
        try:
            os.symlink(outside, self.sessions / "linked", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create a symlink here")
        with self.assertRaises(StoreError):
            safe_session_dir(self.sessions, "linked")
        with self.assertRaises(StoreError):
            Store.open_existing(self.sessions, "linked")


class TestCliCommands(unittest.TestCase):
    """The real `agent` entry point with the fake CLI: sessions / resume exit codes and messages."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent cli "))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.work = self.tmp / "work"
        self.work.mkdir()
        (self.tmp / "fs").mkdir()
        self.home.mkdir()
        cfg = {"manager": "manager", "agents": {"manager": {"role": "manager", "cli": "fake"},
                                                "w1": {"role": "worker", "cli": "fake"}}, "turn_timeout_sec": 30}
        (self.home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.env = {"AGENT_HOME": str(self.home), "FAKE_STATE": str(self.tmp / "fs"),
                    "FAKE_SCENARIO": str(self.tmp / "scn.json")}

    def run_agent(self, argv, stdin="", scenario=None):
        if scenario is not None:
            (self.tmp / "scn.json").write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ, **self.env, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        return subprocess.run([sys.executable, "-m", "agent"] + argv, input=stdin, capture_output=True, text=True,
                              encoding="utf-8", env=env, cwd=self.work, timeout=90)

    def only_session(self):
        return next((self.home / "sessions").iterdir())

    def test_sessions_resume_end_to_end(self):
        scn = {"manager": [M([final("第一輪完成")]), M([final("第二輪完成")])], "w1": []}
        r = self.run_agent([], "第一句\n/exit\n", scn)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        sid = self.only_session().name
        self.assertIn(f"agent resume {sid}", r.stdout)
        self.assertFalse(SessionLock.is_locked(self.only_session()))            # lock released on exit
        r = self.run_agent(["sessions"])
        self.assertEqual(r.returncode, 0)
        self.assertIn(sid, r.stdout)
        self.assertIn("第一句", r.stdout)
        r = self.run_agent(["resume", sid, "--replay", "20"], "第二句\n/status\n/exit\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout
        self.assertIn("以下是先前的對話", out)
        self.assertEqual(out.count("Manager> @Me 第一輪完成"), 1)                  # replayed once
        self.assertIn("Manager> @Me 第二輪完成", out)
        self.assertLess(out.index("先前的對話到此為止"), out.index("Manager> @Me 第二輪完成"))
        self.assertIn("已恢復 Session", out)
        # the resumed run appended to the SAME session: one directory, both rounds in its log
        self.assertEqual(len(list((self.home / "sessions").iterdir())), 1)
        texts = [e.get("text") for e in Store.read_events(self.only_session()) if e.get("type") == "chat"]
        self.assertEqual(texts.count("第一輪完成"), 1)
        self.assertIn("第二輪完成", texts)

    def test_resume_unknown_id_and_locked_session_and_cwd_mismatch(self):
        r = self.run_agent(["resume", "does-not-exist"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("not found", r.stderr)
        self.run_agent([], "hi\n/exit\n", {"manager": [M([final("ok")])], "w1": []})
        sid = self.only_session().name
        lk = SessionLock(self.only_session())
        lk.acquire()                                                           # "another agent process" is writing
        try:
            r = self.run_agent(["resume", sid])
            self.assertEqual(r.returncode, 2)
            self.assertIn("in use by another agent process", r.stderr)
            self.assertEqual(lk.read()["token"], lk.token)                      # we did not disturb their lock
            self.assertIn("[IN USE]", self.run_agent(["sessions"]).stdout)
        finally:
            lk.release()
        other = self.tmp / "elsewhere"
        other.mkdir()
        env = dict(os.environ, **self.env, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        r = subprocess.run([sys.executable, "-m", "agent", "resume", sid], input="/exit\n", capture_output=True,
                           text=True, encoding="utf-8", env=env, cwd=other, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--here", r.stderr)
        self.assertFalse(SessionLock.is_locked(self.only_session()))            # refused resumes release their lock
        r = subprocess.run([sys.executable, "-m", "agent", "resume", sid, "--here"], input="/exit\n",
                           capture_output=True, text=True, encoding="utf-8", env=env, cwd=other, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("continuing here", r.stdout)

    def test_resume_refuses_path_traversal_and_outside_targets_and_touches_nothing(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "state.json").write_text(json.dumps({"version": 2, "agents": {}}), encoding="utf-8")
        (self.home / "sessions").mkdir(exist_ok=True)
        before = sorted(p.name for p in self.tmp.rglob("*"))
        for bad in ("../../outside", "..", "../outside", str(outside), "..\\..\\outside", "C:\\Windows", "a/b", "",
                    "x" * 300, "bad id"):
            r = self.run_agent(["resume", bad])
            self.assertEqual(r.returncode, 2, (bad, r.stdout, r.stderr))
            self.assertIn("Cannot resume", r.stderr, bad)
            self.assertNotIn("Traceback", r.stderr, bad)
        self.assertFalse((outside / LOCK_NAME).exists())                        # no lock file was created out there
        self.assertEqual(sorted(p.name for p in self.tmp.rglob("*")), before)    # nothing at all was created anywhere
        self.assertEqual(json.loads((outside / "state.json").read_text(encoding="utf-8")),
                         {"version": 2, "agents": {}})                           # and nothing rewritten

    def test_a_normal_session_id_still_resumes(self):
        self.run_agent([], "hi\n/exit\n", {"manager": [M([final("ok")]), M([final("again")])], "w1": []})
        sid = self.only_session().name
        r = self.run_agent(["resume", sid], "more\n/exit\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Manager> @Me again", r.stdout)

    def test_resume_refuses_when_config_changed_the_cli_of_an_agent(self):
        self.run_agent([], "hi\n/exit\n", {"manager": [M([final("ok")])], "w1": []})
        sid = self.only_session().name
        cfg = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        cfg["agents"]["w1"]["cli"] = "claude"
        (self.home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        r = self.run_agent(["resume", sid])
        self.assertEqual(r.returncode, 2)
        self.assertIn("'fake'", r.stderr)
        self.assertIn("'claude'", r.stderr)

    def test_resume_after_kill_in_the_middle_of_a_turn(self):
        scn = {"manager": [M([final("x")], wait_for_file=str(self.tmp / "never.flag")), M([final("之後")])], "w1": []}
        (self.tmp / "scn.json").write_text(json.dumps(scn), encoding="utf-8")
        env = dict(os.environ, **self.env, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        p = subprocess.Popen([sys.executable, "-m", "agent"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", env=env, cwd=self.work)
        p.stdin.write("很重要的訊息\n")
        p.stdin.flush()
        sess = None
        end = time.time() + 30
        while time.time() < end:                                                # wait until the turn is really in flight
            sd = list((self.home / "sessions").iterdir()) if (self.home / "sessions").exists() else []
            if sd:
                sess = sd[0]
                try:
                    st = json.loads((sess / "state.json").read_text(encoding="utf-8"))
                    if st.get("mail", {}).get("agents", {}).get("manager", {}).get("inflight"):
                        break
                except (OSError, ValueError):
                    pass
            time.sleep(0.1)
        else:
            p.kill()
            self.fail("turn never became in-flight")
        p.kill()                                                                # hard kill: no shutdown code runs
        p.wait()
        r = self.run_agent(["resume", sess.name], "/exit\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("很重要的訊息", r.stdout)
        self.assertIn("結果未知", r.stdout)
        self.assertIn("不會自動重做", r.stdout)


class TestReplayAndRecording(Base):
    def test_every_line_has_exactly_one_source_and_order_is_kept(self):
        self.rig = r = Rig({"manager": [M([dispatch("w1", "t1", "請分析")], say="先看看"), M([ask("刪除嗎？")]),
                                        M([final("完成了")])],
                            "w1": [W("done", "t1", "分析完成")]})
        r.say("幫我整理")
        r.say("可以")
        items = resume.timeline(Store.read_events(r.store.dir))
        flat = [(i["kind"], i["sender"], i["text"]) for i in items]
        want = [("chat", "Me", "幫我整理"), ("chat", "Manager", "先看看"), ("chat", "Manager", "請分析"),
                ("chat", "w1", "分析完成"), ("ask_user", "Manager", "刪除嗎？"), ("chat", "Me", "可以"),
                ("chat", "Manager", "完成了")]
        got_core = [x for x in flat if x[0] in ("chat", "ask_user")]
        self.assertEqual(got_core, want)                                         # nothing twice, nothing missing
        sys_texts = [x[2] for x in flat if x[0] == "system"]
        self.assertTrue(any("開始執行" in t for t in sys_texts))                  # system notices are recorded too
        idx = {x: n for n, x in enumerate(flat)}
        self.assertLess(idx[("chat", "Me", "幫我整理")], idx[("chat", "Manager", "先看看")])
        self.assertLess(idx[("chat", "w1", "分析完成")], idx[("ask_user", "Manager", "刪除嗎？")])
        self.assertLess(idx[("chat", "Me", "可以")], idx[("chat", "Manager", "完成了")])
        seqs = [e["seq"] for e in Store.read_events(r.store.dir)]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_errors_and_interrupts_are_recorded(self):
        self.rig = r = Rig({"manager": [{"exit": 3, "stderr": "boom"}], "w1": []})
        r.say("hi")
        kinds = [(i["kind"], i["text"]) for i in resume.timeline(Store.read_events(r.store.dir))]
        self.assertTrue(any(k == "error" and "Manager" in t for k, t in kinds), kinds)
        r.engine.interrupt()
        self.assertTrue(r.engine.wait_idle(10))
        texts = [t for k, t in [(i["kind"], i["text"]) for i in resume.timeline(Store.read_events(r.store.dir))]]
        self.assertTrue(any("Nothing is running" in t or "Interrupted" in t for t in texts))

    def test_replay_prints_through_the_plain_ui_only_and_respects_limit(self):
        items = [{"kind": "chat", "sender": "Me", "to": "Manager", "text": f"m{i}", "ts": i} for i in range(10)]
        ui = BufferUI()
        n = resume.replay(ui, items, 3)
        self.assertEqual(n, 3)
        chats = [t for k, s, t, _ in ui.lines if k == "chat"]
        self.assertEqual(chats, ["m7", "m8", "m9"])
        self.assertTrue(any("另有 7 則較早的未顯示" in t for k, s, t, _ in ui.lines if k == "system"))
        self.assertEqual(resume.replay(BufferUI(), items, 0), 0)

    def test_secrets_are_not_written_to_log_or_state(self):
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        self.rig = r = Rig({"manager": [M([final("x")], wait_for_file="m.flag"), M([final("y")])], "w1": []})
        r.engine.submit("first")
        self.assertTrue(self.running("manager"))
        r.engine.submit(f"my key is {secret} please use it")                     # queued -> persisted in state.json mail
        self.assertTrue(r.wait_until(lambda: any("queued" in t for t in r.ui.texts("system"))))
        r.engine.ui.system(f"note: token {secret}")
        raw_state = (r.store.dir / "state.json").read_text(encoding="utf-8")
        raw_events = r.store.events_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw_state)
        self.assertNotIn(secret, raw_events)
        self.flag("m.flag")
        self.assertTrue(r.engine.wait_idle(30))
        for f in r.store.dir.rglob("*"):
            if f.is_file():
                self.assertNotIn(secret, f.read_text(encoding="utf-8", errors="replace"), str(f))


class TestCompatCheck(unittest.TestCase):
    def cfg(self, **over):
        agents = {"manager": {"role": "manager", "cli": "claude"}, "w1": {"role": "worker", "cli": "agy"}}
        for k, v in over.items():
            agents[k] = v
        return parse_config({"manager": "manager", "agents": agents})

    def state(self, cwd="/work"):
        return {"cwd": cwd, "agents": {"manager": {"role": "manager", "cli": "claude", "model": None, "effort": None},
                                       "w1": {"role": "worker", "cli": "agy", "model": None, "effort": None}}}

    def test_ok(self):
        e, w = resume.check_compat(self.cfg(), self.state(), "/work")
        self.assertEqual((e, w), ([], []))

    def test_model_change_is_a_warning_cli_change_is_an_error(self):
        e, w = resume.check_compat(self.cfg(w1={"role": "worker", "cli": "agy", "model": "gem-x"}), self.state(), "/work")
        self.assertEqual(e, [])
        self.assertTrue(any("model" in x for x in w))
        e, _ = resume.check_compat(self.cfg(w1={"role": "worker", "cli": "claude"}), self.state(), "/work")
        self.assertTrue(any("'agy'" in x for x in e))

    def test_cwd_rule(self):
        e, _ = resume.check_compat(self.cfg(), self.state("/a"), "/b")
        self.assertTrue(any("--here" in x for x in e))
        e, w = resume.check_compat(self.cfg(), self.state("/a"), "/b", here=True)
        self.assertEqual(e, [])
        self.assertTrue(any("--here" in x for x in w))

    def test_unknown_agent_either_way(self):
        st = self.state()
        st["agents"]["ghost"] = {"role": "worker", "cli": "agy"}
        e, _ = resume.check_compat(self.cfg(), st, "/work")
        self.assertTrue(any("ghost" in x for x in e))


if __name__ == "__main__":
    unittest.main()
