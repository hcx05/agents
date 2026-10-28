"""Run the real `python -m agent` process with piped stdin against the fake CLI."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.rig import M, W, answer, ask, dispatch, final  # noqa: E402


def run_agent(args, stdin, env_extra, cwd, timeout=90):
    env = dict(os.environ)
    env.update(env_extra)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run([sys.executable, "-m", "agent"] + args, input=stdin, capture_output=True,
                          text=True, encoding="utf-8", env=env, cwd=cwd, timeout=timeout)


class TestCliE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="e2e 測試 "))
        self.home = self.tmp / "home"
        self.work = self.tmp / "work"
        self.work.mkdir()
        (self.tmp / "fs").mkdir()
        self.cfg = {"manager": "manager",
                    "agents": {"manager": {"role": "manager", "cli": "fake", "model": None, "effort": None},
                               "w1": {"role": "worker", "cli": "fake", "model": None, "effort": None}},
                    "turn_timeout_sec": 30, "max_manager_turns_per_input": 30}
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps(self.cfg), encoding="utf-8")

    def env(self, scenario):
        (self.tmp / "scn.json").write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
        return {"AGENT_HOME": str(self.home), "FAKE_SCENARIO": str(self.tmp / "scn.json"),
                "FAKE_STATE": str(self.tmp / "fs")}

    def test_help_and_config(self):
        r = run_agent(["--help"], "", {}, self.work)
        self.assertEqual(r.returncode, 0)
        self.assertIn("config", r.stdout)
        r = run_agent(["config"], "", {"AGENT_HOME": str(self.tmp / "fresh")}, self.work)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("config.json", r.stdout)
        data = json.loads((self.tmp / "fresh" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(data["agents"]["w1"]["cli"], "agy")
        self.assertEqual(data["turn_timeout_sec"], 1800)

    def test_full_chat_room_session(self):
        scn = {"manager": [M([dispatch("w1", "t1", "請檢查 logs/ 資料夾")], say="我先分析專案結構。"),
                           M([final("任務完成，以下是整理結果。")])],
               "w1": [W("done", "t1", "已完成，發現三項異常。")]}
        r = run_agent([], "請叫員工分析這個資料夾，完成後整理結果給我。\n/status\n/bogus\n/exit\n", self.env(scn), self.work)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout
        for needle in ["Agent Team", "Manager: Fake", "Worker: Fake", "Manager> 我先分析專案結構。",
                       "Manager> @w1 請檢查 logs/ 資料夾", "w1> @Manager 已完成，發現三項異常。",
                       "Manager> @Me 任務完成，以下是整理結果。", "state: done", "Unknown command /bogus"]:
            self.assertIn(needle, out)
        sessions = list((self.home / "sessions").iterdir())
        self.assertEqual(len(sessions), 1)                         # one new session per run
        st = json.loads((sessions[0] / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(st["agents"]["manager"]["session_id"] and st["agents"]["w1"]["session_id"])

    def test_ask_user_through_stdin(self):
        scn = {"manager": [M([ask("可以執行嗎？")]), M([final("好，已執行。")])], "w1": []}
        r = run_agent([], "做危險的事\n可以\n/exit\n", self.env(scn), self.work)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # ask_user is now headed "[需要你的回答]" with the question on its own line (format changed on purpose)
        self.assertLess(r.stdout.index("Manager> @Me [需要你的回答]\n可以執行嗎？"),
                        r.stdout.index("Manager> @Me 好，已執行。"))

    def test_missing_cli_exits_cleanly(self):
        self.cfg["agents"]["w1"]["command"] = "definitely-not-a-real-cli-xyz"
        (self.home / "config.json").write_text(json.dumps(self.cfg), encoding="utf-8")
        r = run_agent([], "hi\n", self.env({"manager": [], "w1": []}), self.work)
        self.assertEqual(r.returncode, 2)
        self.assertIn("command not found", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_bad_config_message(self):
        (self.home / "config.json").write_text("{not json", encoding="utf-8")
        r = run_agent([], "", self.env({}), self.work)
        self.assertEqual(r.returncode, 2)
        self.assertIn("Config error", r.stderr)


if __name__ == "__main__":
    unittest.main()
