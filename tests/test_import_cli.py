"""`agent <Claude Session UUID>` / `agent import`: argument handling, confirmation, pre-flight and the whole flow through the
real `python -m agent` process with the fake CLI. Nothing here talks to a real Claude."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import cli, importer  # noqa: E402
from tests.rig import M, W, dispatch, final  # noqa: E402

A = "aaaaaaaa-1111-4222-8333-444444444444"
AGENT_ID = "20261028-131316-be0f76"


class TestRewriteArgv(unittest.TestCase):
    def test_a_claude_uuid_becomes_import_and_everything_else_is_untouched(self):
        self.assertEqual(cli.rewrite_argv([A]), ["import", A])
        self.assertEqual(cli.rewrite_argv([A.upper()]), ["import", A.upper()])
        self.assertEqual(cli.rewrite_argv(["--config", "c.json", A, "--yes"]), ["--config", "c.json", "import", A, "--yes"])
        for argv in ([], ["sessions"], ["resume", AGENT_ID], ["import", A], ["config", "--show"], ["--version"],
                     ["resume", A], ["bogus"]):
            self.assertEqual(cli.rewrite_argv(list(argv)), argv)

    def test_an_agent_session_id_without_resume_is_explained(self):
        self.assertIsNone(cli.rewrite_argv([AGENT_ID]))

    def test_names_and_paths_are_not_claude_ids(self):
        for bad in ("my-session", "../x", "C:\\x\\a.jsonl", A + "x", A[:-1], ""):
            self.assertFalse(importer.is_claude_id(bad), bad)
            self.assertNotEqual(cli.rewrite_argv([bad or "x"])[:1], ["import"])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="imp cli 測試 "))
        self.home, self.work, self.fs = self.tmp / "home", self.tmp / "work", self.tmp / "fs"
        self.claude = self.tmp / "claude cfg"
        for d in (self.home, self.work, self.fs, self.fs / "sessions", self.claude / "projects" / "-other-folder"):
            d.mkdir(parents=True)
        cfg = {"manager": "manager", "turn_timeout_sec": 30, "max_manager_turns_per_input": 30,
               "agents": {"manager": {"role": "manager", "cli": "fake", "model": None, "effort": None},
                          "w1": {"role": "worker", "cli": "fake", "model": None, "effort": None}}}
        (self.home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.src = self.fs / "sessions" / f"{A}.json"
        self.src.write_text(json.dumps({"turns": 5, "agent": "native"}), encoding="utf-8")
        self.transcript = self.claude / "projects" / "-other-folder" / f"{A}.jsonl"
        self.transcript.write_bytes(b"x" * 2048)
        old = time.time() - 7200
        os.utime(self.transcript, (old, old))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_agent(self, args, stdin="", scenario=None, extra_env=None, timeout=90):
        (self.tmp / "scn.json").write_text(json.dumps(scenario or {"manager": [], "w1": []}, ensure_ascii=False), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env.update(AGENT_HOME=str(self.home), FAKE_SCENARIO=str(self.tmp / "scn.json"), FAKE_STATE=str(self.fs),
                   CLAUDE_CONFIG_DIR=str(self.claude), PYTHONPATH=str(ROOT))
        env.update(extra_env or {})
        return subprocess.run([sys.executable, "-m", "agent"] + args, input=stdin, capture_output=True, text=True,
                              encoding="utf-8", env=env, cwd=self.work, timeout=timeout)

    def sessions(self):
        d = self.home / "sessions"
        return sorted(p.name for p in d.iterdir()) if d.exists() else []

    def state(self, sid):
        return json.loads((self.home / "sessions" / sid / "state.json").read_text(encoding="utf-8"))

    def calls(self):
        p = self.fs / "calls.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class TestImportCommand(Base):
    def test_without_confirmation_in_a_non_terminal_nothing_is_created_and_no_model_is_called(self):
        r = self.run_agent([A], "/exit\n")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("--yes", r.stderr)
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.calls(), [])
        self.assertIn(A, r.stdout)
        self.assertIn("保持不變", r.stdout)                              # the warning text was shown before asking
        self.assertIn("可能消耗大量額度", r.stdout)

    def test_a_guessed_other_folder_needs_an_explicit_here(self):
        r = self.run_agent([A, "--yes"], "/exit\n")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("推測", r.stdout)                                  # labelled as a guess
        self.assertIn("--here", r.stderr)
        self.assertEqual(self.sessions(), [])
        r = self.run_agent([A, "--yes", "--here"], "/exit\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.sessions()), 1)

    def test_inside_a_claude_code_session_it_refuses(self):
        r = self.run_agent([A, "--yes", "--here"], "/exit\n", extra_env={"CLAUDE_CODE_SESSION_ID": "outer"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("CLAUDE_CODE_SESSION_ID", r.stderr)
        self.assertEqual((self.sessions(), self.calls()), ([], []))

    def test_invalid_ids_are_refused_before_anything_happens(self):
        r = self.run_agent(["import", "../../x", "--yes"], "")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not a Claude session id", r.stderr)
        self.assertEqual(self.sessions(), [])

    def test_import_alias_and_bare_uuid_do_the_same_thing(self):
        for args in ([A, "--yes", "--here"], ["import", A, "--yes", "--here"]):
            for d in (self.home / "sessions",):
                if d.exists():
                    import shutil
                    shutil.rmtree(d)
            r = self.run_agent(args, "/exit\n")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            (sid,) = self.sessions()
            st = self.state(sid)
            self.assertEqual((st["version"], st["import"]["source_id"], st["import"]["stage"]), (3, A, "prepared"))

    def test_start_creates_the_record_but_does_not_call_the_model_until_the_first_message(self):
        r = self.run_agent([A, "--yes", "--here"], "/exit\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls(), [])
        self.assertIn("已匯入 Claude Session " + A, r.stdout)
        (sid,) = self.sessions()
        self.assertTrue(importer.is_claude_id(self.state(sid)["import"]["fork_id"]))
        self.assertIn(f"agent resume {sid}", r.stdout)

    def test_the_whole_story_import_chat_exit_sessions_resume(self):
        scn = {"manager": [M([dispatch("w1", "t1", "查 logs")], say="先交給員工。"), M([final("員工查完了。")]),
                           M([final("續聊完成")])],
               "w1": [W("done", "t1", "三項異常")]}
        r = self.run_agent(["import", A, "--yes", "--here"], "請幫我接著做\n/exit\n", scn)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for needle in ("Manager> 先交給員工。", "Manager> @w1 查 logs", "w1> @Manager 三項異常", "Manager> @Me 員工查完了。"):
            self.assertIn(needle, r.stdout)
        (sid,) = self.sessions()
        b = self.state(sid)["import"]["fork_id"]
        c0 = self.calls()[0]["argv"]
        self.assertEqual((c0[c0.index("--resume") + 1], c0[c0.index("--session-id") + 1]), (A, b))
        self.assertEqual(self.state(sid)["import"]["stage"], "ready")
        self.assertEqual(json.loads(self.src.read_text("utf-8")), {"turns": 5, "agent": "native"})
        # sessions list
        r = self.run_agent(["sessions"])
        self.assertIn(sid, r.stdout)
        self.assertIn("[import aaaaaaaa]", r.stdout)
        # a second import of the same Claude session is refused and points at the existing one
        r = self.run_agent([A, "--yes", "--here"], "/exit\n")
        self.assertEqual(r.returncode, 2)
        self.assertIn(sid, r.stderr)
        self.assertIn("agent resume", r.stderr)
        self.assertEqual(self.sessions(), [sid])
        # resume: history is replayed with the "before the import" notice, the Manager continues in the fork
        n = len(self.calls())
        r = self.run_agent(["resume", sid], "再問一個\n/exit\n", scn)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("匯入前的 Claude 原生歷史", r.stdout)
        self.assertIn("已匯入 Claude Session " + A, r.stdout)           # the boundary line itself is part of the replay
        self.assertIn("Manager> 先交給員工。", r.stdout)                 # replayed
        self.assertIn("Manager> @Me 續聊完成", r.stdout)
        new = self.calls()[n:]
        self.assertEqual(len(new), 1)
        a = new[0]["argv"]
        self.assertEqual(a[a.index("--resume") + 1], b)
        self.assertNotIn("--fork-session", a)
        for c in self.calls():
            argv = c["argv"]
            if "--resume" in argv and argv[argv.index("--resume") + 1] == A:
                self.assertIn("--fork-session", argv)
        # the chat log has the boundary and the new messages, and nothing from the native history
        events = [json.loads(l) for l in (self.home / "sessions" / sid / "events.jsonl").read_text("utf-8").splitlines()]
        ui = [e["text"] for e in events if e["type"] == "ui"]
        self.assertTrue(any("匯入前的原生對話不在本聊天室紀錄中" in t for t in ui))
        self.assertEqual([e["text"] for e in events if e["type"] == "chat" and e["sender"] == "Me"], ["請幫我接著做", "再問一個"])


class TestResumeAndHints(Base):
    def test_resume_with_a_claude_id_explains_the_difference(self):
        r = self.run_agent(["resume", A])
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"agent {A}", r.stderr)

    def test_an_agent_id_without_resume_explains_it(self):
        r = self.run_agent([AGENT_ID])
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"agent resume {AGENT_ID}", r.stderr)

    def test_plain_agent_and_sessions_still_work(self):
        r = self.run_agent([], "/exit\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Imported", r.stdout)
        r = self.run_agent(["sessions"])
        self.assertEqual(r.returncode, 0)


class TestPreflight(Base):
    def pf(self, cwd="/some/other", **kw):
        return importer.preflight(A, cwd, self.home / "sessions", kw.pop("cli", "claude"), env=kw.pop("env", {}),
                                  claude_dir=self.claude, **kw)

    def test_folder_guess_is_labelled_and_matching_folders_are_quiet(self):
        pf = self.pf(cwd="/some/other")
        self.assertTrue(pf.folder_mismatch)
        self.assertTrue(any("推測" in w for w in pf.warnings))
        self.assertFalse(self.pf(cwd="/other/folder").folder_mismatch)                 # encodes to -other-folder
        self.assertIn("2.0 KB", " ".join(self.pf(cwd="/other/folder").info))
        self.assertTrue(any("不等於 token" in i for i in self.pf().info))

    def test_missing_transcript_is_only_a_warning(self):
        self.transcript.unlink()
        pf = self.pf()
        self.assertEqual(pf.blockers, [])
        self.assertTrue(any("找不到" in w for w in pf.warnings))

    def test_recent_activity_warns(self):
        os.utime(self.transcript, None)
        self.assertTrue(any("可能仍在" in w for w in self.pf().warnings))

    def test_blockers(self):
        self.assertTrue(any("CLAUDE_CODE_SESSION_ID" in b for b in self.pf(env={"CLAUDE_CODE_SESSION_ID": "x"}).blockers))
        self.assertTrue(any("agy" in b for b in self.pf(cli="agy").blockers))
        self.assertEqual(self.pf().blockers, [])


if __name__ == "__main__":
    unittest.main()
