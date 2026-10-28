"""Automated tests for Batch 4A: Telescope-style Session Picker (`agent pick`).

Covers all 28 required categories:
- Basic functionality (1..8): loading, newest-first sorting, Chinese search, case-insensitive English search,
  Session ID search, folder search, no-match empty result, and empty sessions folder.
- Keyboard operations (9..14): Up/Down navigation, Enter to resume, Ctrl+N for new session, Esc to exit,
  rapid consecutive movement, and window resize / narrow-terminal reflow.
- Session safety (15..23): Locked session refusal (including TOCTOU lock check), different folder refusal
  (never auto-applying --here), damaged session resilience (corrupt JSON, bad version, incomplete metadata),
  strictly read-only preview (never modifying state.json or events.jsonl, even with half-written tail),
  zero model calls during preview, Esc creating nothing, existing resume validation handoff, and Claude
  Source/Fork UUID preservation.
- Performance & compatibility (24..28): hundreds of sessions, large events.jsonl tail preview, non-TTY fallback,
  missing prompt_toolkit fallback, and CLI command compatibility."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import cli, importer, picker  # noqa: E402
from agent.picker import (  # noqa: E402
    MIN_SPLIT_WIDTH,
    PREVIEW_MAX_ITEMS,
    PickResult,
    PickerApp,
    PickerControl,
    PickerView,
    filter_sessions,
    load_session_items,
    read_recent_timeline,
    text_width,
)
from agent.sessionlock import LOCK_NAME, SessionLock  # noqa: E402
from agent.store import STATE_VERSION  # noqa: E402
from tests.rig import M, final  # noqa: E402

try:
    import prompt_toolkit  # noqa: F401
    HAVE_PTK = True
except ImportError:
    HAVE_PTK = False

CLAUDE_SRC_UUID = "11111111-2222-4333-8444-555555555555"
CLAUDE_FORK_UUID = "99999999-8888-4777-8666-555555555555"


def flat_lines(rows) -> list[str]:
    return ["".join(frag_text for _, frag_text in row) for row in rows]


class PickerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="picker 測試 "))
        self.home = self.tmp / "home"
        self.sessions_root = self.home / "sessions"
        self.work = self.tmp / "work"
        self.other_work = self.tmp / "other_project"
        self.fs = self.tmp / "fs"
        for d in (self.home, self.sessions_root, self.work, self.other_work, self.fs):
            d.mkdir(parents=True, exist_ok=True)
        self.cfg = {
            "manager": "manager",
            "turn_timeout_sec": 30,
            "max_manager_turns_per_input": 30,
            "agents": {
                "manager": {"role": "manager", "cli": "fake", "model": None, "effort": None},
                "w1": {"role": "worker", "cli": "fake", "model": None, "effort": None},
            },
        }
        (self.home / "config.json").write_text(json.dumps(self.cfg), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_session(
        self,
        sid: str,
        *,
        created: float = 1700000000.0,
        updated: float = 1700000100.0,
        cwd: Path | str | None = None,
        state: str = "done",
        first_msg: str = "幫我整理這個專案",
        extra_events: list[dict] | None = None,
        import_info: dict | None = None,
        mgr_cli: str = "claude",
        worker_cli: str = "agy",
    ) -> Path:
        sdir = self.sessions_root / sid
        (sdir / "raw").mkdir(parents=True, exist_ok=True)
        cwd_str = str(self.work if cwd is None else cwd)
        st = {
            "version": STATE_VERSION,
            "id": sid,
            "created": created,
            "updated": updated,
            "cwd": cwd_str,
            "state": state,
            "agents": {
                "manager": {"role": "manager", "cli": mgr_cli, "session_id": f"mgr-{sid}", "preamble_sent": True},
                "w1": {"role": "worker", "cli": worker_cli, "session_id": f"w1-{sid}", "preamble_sent": True},
            },
            "tasks": {},
            "awaiting_user": None,
            "manager_turns": 1,
            "last_error": None,
            "config": {
                "manager": "manager",
                "agents": {
                    "manager": {"role": "manager", "cli": mgr_cli, "model": None, "effort": None},
                    "w1": {"role": "worker", "cli": worker_cli, "model": None, "effort": None},
                },
            },
            "mail": {},
        }
        if import_info is not None:
            st["import"] = import_info
        (sdir / "state.json").write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")

        events = []
        seq = 1
        if first_msg:
            events.append({"seq": seq, "ts": created + 1, "type": "chat", "sender": "Me", "to": "Manager", "text": first_msg})
            seq += 1
        events.append({"seq": seq, "ts": created + 2, "type": "chat", "sender": "Manager", "to": "w1", "text": "請檢查 logs 並整理異常"})
        seq += 1
        events.append({"seq": seq, "ts": created + 3, "type": "chat", "sender": "w1", "to": "Manager", "text": "已完成，發現三項異常"})
        seq += 1
        events.append({"seq": seq, "ts": updated, "type": "chat", "sender": "Manager", "to": "Me", "text": "任務完成，以下是整理結果"})
        seq += 1
        for ev in extra_events or []:
            rec = dict(ev)
            rec.setdefault("seq", seq)
            rec.setdefault("ts", updated + seq)
            events.append(rec)
            seq += 1
        with open(sdir / "events.jsonl", "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return sdir

    def run_cli_inproc(self, argv: list[str], stdin: str = "/exit\n", scenario: dict | None = None,
                       cwd: Path | None = None, extra_env: dict | None = None) -> tuple[int, str, str]:
        (self.tmp / "scn.json").write_text(
            json.dumps(scenario or {"manager": [], "w1": []}, ensure_ascii=False), encoding="utf-8"
        )
        env = {
            "AGENT_HOME": str(self.home),
            "FAKE_SCENARIO": str(self.tmp / "scn.json"),
            "FAKE_STATE": str(self.fs),
            "PYTHONPATH": str(ROOT),
        }
        if extra_env:
            env.update(extra_env)
        out, err = io.StringIO(), io.StringIO()
        prev_cwd = os.getcwd()
        os.chdir(cwd or self.work)
        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
                code = cli.main(argv)
        finally:
            os.chdir(prev_cwd)
        return code, out.getvalue(), err.getvalue()

    def fake_calls(self) -> list[dict]:
        p = self.fs / "calls.jsonl"
        return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class TestBasicFunctionality(PickerTestBase):
    """Tests 1..8: Session loading, sorting, Chinese/English/ID/folder search, empty search, and zero sessions."""

    def test_01_and_02_load_sessions_and_sort_by_updated_descending(self):
        self.make_session("20261028-100000-aaaaaa", created=1000.0, updated=1100.0, first_msg="最舊的對話")
        self.make_session("20261028-120000-cccccc", created=1200.0, updated=1500.0, first_msg="最新的對話")
        self.make_session("20261028-110000-bbbbbb", created=1100.0, updated=1300.0, first_msg="中間的對話")

        items = load_session_items(self.sessions_root, str(self.work))
        self.assertEqual([it.id for it in items], [
            "20261028-120000-cccccc",
            "20261028-110000-bbbbbb",
            "20261028-100000-aaaaaa",
        ])
        self.assertEqual(items[0].first, "最新的對話")
        self.assertEqual(items[0].created, 1200.0)
        self.assertEqual(items[0].updated, 1500.0)
        self.assertTrue(items[0].ok)
        self.assertFalse(items[0].cwd_mismatch)

    def test_03_chinese_search_and_mixed_cjk(self):
        self.make_session("20261028-100000-aaaaaa", updated=100.0, first_msg="請幫我分析資料庫效能問題")
        self.make_session("20261028-110000-bbbbbb", updated=200.0, first_msg="修正 Windows 終端機中文輸入法 Bug")
        self.make_session("20261028-120000-cccccc", updated=300.0, first_msg="撰寫 API 測試報告")

        view = PickerView(self.sessions_root, str(self.work))
        view.set_query("輸入法")
        self.assertEqual([it.id for it in view.filtered], ["20261028-110000-bbbbbb"])

        view.set_query("Windows 中文")
        self.assertEqual([it.id for it in view.filtered], ["20261028-110000-bbbbbb"])

        view.set_query("資料庫")
        self.assertEqual([it.id for it in view.filtered], ["20261028-100000-aaaaaa"])

    def test_04_english_case_insensitive_search(self):
        self.make_session("20261028-100000-aaaaaa", updated=100.0, first_msg="Refactor Authentication Middleware")
        self.make_session("20261028-110000-bbbbbb", updated=200.0, first_msg="Deploy Kubernetes cluster")

        items = load_session_items(self.sessions_root, str(self.work))
        self.assertEqual([it.id for it in filter_sessions(items, "authentication")], ["20261028-100000-aaaaaa"])
        self.assertEqual([it.id for it in filter_sessions(items, "AUTHENTICATION")], ["20261028-100000-aaaaaa"])
        self.assertEqual([it.id for it in filter_sessions(items, "kUbErNeTeS")], ["20261028-110000-bbbbbb"])

    def test_05_session_id_search(self):
        self.make_session("20261028-100000-be0f76", updated=100.0, first_msg="任務一")
        self.make_session("20261028-110000-99aabb", updated=200.0, first_msg="任務二")

        items = load_session_items(self.sessions_root, str(self.work))
        self.assertEqual([it.id for it in filter_sessions(items, "be0f76")], ["20261028-100000-be0f76"])
        self.assertEqual([it.id for it in filter_sessions(items, "BE0F76")], ["20261028-100000-be0f76"])
        self.assertEqual([it.id for it in filter_sessions(items, "20261028-110000")], ["20261028-110000-99aabb"])

    def test_06_working_directory_search(self):
        proj_alpha = self.tmp / "alpha_service"
        proj_beta = self.tmp / "beta_frontend"
        proj_alpha.mkdir()
        proj_beta.mkdir()
        self.make_session("20261028-100000-aaaaaa", updated=100.0, cwd=proj_alpha, first_msg="一般訊息")
        self.make_session("20261028-110000-bbbbbb", updated=200.0, cwd=proj_beta, first_msg="一般訊息")

        items = load_session_items(self.sessions_root, str(self.work))
        self.assertEqual([it.id for it in filter_sessions(items, "alpha_service")], ["20261028-100000-aaaaaa"])
        self.assertEqual([it.id for it in filter_sessions(items, "BETA_FRONTEND")], ["20261028-110000-bbbbbb"])

    def test_07_search_with_no_results_shows_clear_empty_prompt(self):
        self.make_session("20261028-100000-aaaaaa", updated=100.0, first_msg="整理專案")
        view = PickerView(self.sessions_root, str(self.work))
        view.set_query("完全不存在的關鍵字xyz987")
        self.assertEqual(view.filtered, [])
        self.assertIsNone(view.selected_item)

        lines = flat_lines(view.window_rows(100, 18))
        joined = "\n".join(lines)
        self.assertIn("找不到符合", joined)
        self.assertIn("Ctrl+N", joined)
        self.assertIsNone(view.confirm())
        self.assertIsNotNone(view.status_notice)

    def test_08_empty_sessions_directory_renders_and_supports_ctrl_n(self):
        view = PickerView(self.sessions_root, str(self.work))
        self.assertEqual(view.all_items, [])
        self.assertEqual(view.filtered, [])
        self.assertIsNone(view.selected_item)

        lines = flat_lines(view.window_rows(100, 18))
        joined = "\n".join(lines)
        self.assertIn("尚無已儲存的 Agent 對話", joined)
        self.assertIn("Ctrl+N", joined)

        view.move(+1)
        view.move(-1)
        self.assertIsNone(view.confirm())

        res = view.new_session()
        self.assertEqual(res, PickResult(action="new", session_id=None))


class TestKeyboardAndLayout(PickerTestBase):
    """Tests 9..14: Up/Down navigation, Enter, Ctrl+N, Esc, rapid selection changes, and window resize / CJK width."""

    def setUp(self) -> None:
        super().setUp()
        self.s1 = "20261028-100000-111111"
        self.s2 = "20261028-110000-222222"
        self.s3 = "20261028-120000-333333"
        self.make_session(self.s1, updated=100.0, first_msg="第一個對話")
        self.make_session(self.s2, updated=200.0, first_msg="第二個對話")
        self.make_session(self.s3, updated=300.0, first_msg="第三個對話")

    def test_09_up_down_movement_and_bounds(self):
        view = PickerView(self.sessions_root, str(self.work))
        self.assertEqual(view.selected_item.id, self.s3)
        view.move(-1)
        self.assertEqual(view.selected_item.id, self.s3)
        view.move(+1)
        self.assertEqual(view.selected_item.id, self.s2)
        view.move(+1)
        self.assertEqual(view.selected_item.id, self.s1)
        view.move(+5)
        self.assertEqual(view.selected_item.id, self.s1)
        view.move(-1)
        self.assertEqual(view.selected_item.id, self.s2)

    def test_10_enter_opens_selected_session_and_continues_via_resume(self):
        scn = {"manager": [M([final("第一輪回覆")]), M([final("恢復後第二輪回覆")])], "w1": []}
        code, out, err = self.run_cli_inproc(["--plain"], "第一輪問題\n/exit\n", scenario=scn)
        self.assertEqual(code, 0, out + err)
        real_sessions = [p.name for p in self.sessions_root.iterdir() if p.name not in (self.s1, self.s2, self.s3)]
        self.assertEqual(len(real_sessions), 1)
        target_sid = real_sessions[0]

        view = PickerView(self.sessions_root, str(self.work))
        view.set_query(target_sid)
        self.assertEqual(view.selected_item.id, target_sid)
        choice = view.confirm()
        self.assertEqual(choice, PickResult(action="resume", session_id=target_sid))

        with mock.patch.object(cli, "_picker_decision", return_value=(True, None)), \
                mock.patch("agent.picker.run_picker", return_value=choice):
            code2, out2, err2 = self.run_cli_inproc(["pick", "--plain"], "第二輪問題\n/exit\n", scenario=scn)
        self.assertEqual(code2, 0, out2 + err2)
        self.assertIn("以下是先前的對話", out2)
        self.assertIn("第一輪回覆", out2)
        self.assertIn("恢復後第二輪回覆", out2)

    def test_11_ctrl_n_creates_new_conversation(self):
        before = set(p.name for p in self.sessions_root.iterdir())
        scn = {"manager": [M([final("新對話建立成功")])], "w1": []}
        with mock.patch.object(cli, "_picker_decision", return_value=(True, None)), \
                mock.patch("agent.picker.run_picker", return_value=PickResult(action="new")):
            code, out, err = self.run_cli_inproc(["pick", "--plain"], "新對話第一句\n/exit\n", scenario=scn)
        self.assertEqual(code, 0, out + err)
        self.assertIn("新對話建立成功", out)
        after = set(p.name for p in self.sessions_root.iterdir())
        self.assertEqual(len(after - before), 1)

    def test_12_and_21_esc_exits_cleanly_without_creating_session_or_calling_model(self):
        before_sessions = sorted(p.name for p in self.sessions_root.iterdir())
        before_mtimes = {
            p.name: (
                (p / "state.json").read_bytes(),
                (p / "events.jsonl").read_bytes(),
            )
            for p in self.sessions_root.iterdir()
        }
        with mock.patch.object(cli, "_picker_decision", return_value=(True, None)), \
                mock.patch("agent.picker.run_picker", return_value=PickResult(action="cancel")):
            code, out, err = self.run_cli_inproc(["pick"], "")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sorted(p.name for p in self.sessions_root.iterdir()), before_sessions)
        self.assertEqual(self.fake_calls(), [])
        for name, (st_bytes, ev_bytes) in before_mtimes.items():
            self.assertEqual((self.sessions_root / name / "state.json").read_bytes(), st_bytes)
            self.assertEqual((self.sessions_root / name / "events.jsonl").read_bytes(), ev_bytes)
            self.assertFalse((self.sessions_root / name / LOCK_NAME).exists())

    def test_13_rapid_consecutive_selection_movement_is_lazy_and_cached(self):
        for i in range(30):
            self.make_session(f"20261028-13{i:04d}-abcdef", updated=1000.0 + i, first_msg=f"快速移動測試 {i}")
        view = PickerView(self.sessions_root, str(self.work))
        self.assertEqual(view.preview_cache.load_count, 0)

        for _ in range(20):
            view.move(+1)
        self.assertEqual(view.preview_cache.load_count, 0)

        view.window_rows(100, 24)
        self.assertEqual(view.preview_cache.load_count, 1)

        view.window_rows(100, 24)
        self.assertEqual(view.preview_cache.load_count, 1)

    def test_14_window_resize_reflows_cjk_and_narrow_width_hides_preview(self):
        self.make_session(
            "20261028-150000-cjk123",
            updated=9999.0,
            first_msg="這是一段非常長的中英文混合訊息 Mixed CJK and English text for resize testing " * 2,
        )
        view = PickerView(self.sessions_root, str(self.work))

        for cols, rows_h in ((140, 35), (100, 24), (72, 18), (60, 16), (40, 12), (24, 8)):
            screen = view.window_rows(cols, rows_h)
            self.assertEqual(len(screen), rows_h)
            for r in flat_lines(screen):
                self.assertLessEqual(text_width(r), cols, f"row overflowed {cols} cols: {r!r}")
            hdr = "".join(t for _, t in view.header_fragments(cols))
            ftr = "".join(t for _, t in view.footer_fragments(cols))
            self.assertLessEqual(text_width(hdr), cols)
            self.assertLessEqual(text_width(ftr), cols)

        wide_text = "\n".join(flat_lines(view.window_rows(100, 24)))
        self.assertIn("對話預覽", wide_text)
        self.assertIn(" | ", wide_text)
        self.assertIn("Manager -> w1", wide_text)
        self.assertIn("Worker (w1) -> Manager", wide_text)

        narrow_text = "\n".join(flat_lines(view.window_rows(MIN_SPLIT_WIDTH - 10, 16)))
        self.assertNotIn("對話預覽 (唯讀 Preview)", narrow_text)
        self.assertIn("對話列表", narrow_text)
        view.move(+1)
        self.assertIsNotNone(view.selected_item)


class TestSessionSafety(PickerTestBase):
    """Tests 15..23: Lock safety, folder mismatch safety, damaged sessions, read-only preview, and Claude UUID rules."""

    def test_15_locked_session_is_marked_and_refused_on_resume_including_toctou(self):
        scn = {"manager": [M([final("ok")])], "w1": []}
        code, out, err = self.run_cli_inproc(["--plain"], "hi\n/exit\n", scenario=scn)
        self.assertEqual(code, 0)
        sid = next(self.sessions_root.iterdir()).name
        sdir = self.sessions_root / sid

        lk = SessionLock(sdir)
        lk.acquire()
        try:
            view = PickerView(self.sessions_root, str(self.work))
            self.assertTrue(view.selected_item.locked)
            screen_txt = "\n".join(flat_lines(view.window_rows(110, 26)))
            self.assertIn("[LOCKED]", screen_txt)
            self.assertIn("使用中", screen_txt)

            with mock.patch.object(cli, "_picker_decision", return_value=(True, None)), \
                    mock.patch("agent.picker.run_picker", return_value=PickResult(action="resume", session_id=sid)):
                code2, out2, err2 = self.run_cli_inproc(["pick", "--plain"], "/exit\n")
            self.assertEqual(code2, 2)
            self.assertIn("in use by another agent process", err2)
            self.assertEqual(lk.read()["token"], lk.token)
        finally:
            lk.release()

    def test_16_different_working_folder_warns_and_never_auto_adds_here(self):
        scn = {"manager": [M([final("ok")])], "w1": []}
        code, out, err = self.run_cli_inproc(["--plain"], "在原資料夾建立\n/exit\n", scenario=scn, cwd=self.work)
        self.assertEqual(code, 0)
        sid = next(self.sessions_root.iterdir()).name

        view = PickerView(self.sessions_root, str(self.other_work))
        sel = view.selected_item
        self.assertTrue(sel.cwd_mismatch)
        screen_txt = "\n".join(flat_lines(view.window_rows(110, 26)))
        self.assertIn("[不同資料夾]", screen_txt)
        self.assertIn("不會自動套用 --here", screen_txt)

        with mock.patch.object(cli, "_picker_decision", return_value=(True, None)), \
                mock.patch("agent.picker.run_picker", return_value=PickResult(action="resume", session_id=sid)):
            code2, out2, err2 = self.run_cli_inproc(["pick", "--plain"], "/exit\n", cwd=self.other_work)
        self.assertEqual(code2, 2)
        self.assertIn("--here", err2)

    def test_17_damaged_sessions_never_crash_picker_and_are_not_modified(self):
        d_corrupt = self.sessions_root / "20261028-100001-badjson"
        d_corrupt.mkdir()
        raw_corrupt = "{ not valid json"
        (d_corrupt / "state.json").write_text(raw_corrupt, encoding="utf-8")

        d_ver = self.sessions_root / "20261028-100002-badver"
        d_ver.mkdir()
        raw_ver = json.dumps({"version": 999, "updated": 2000.0, "cwd": str(self.work), "agents": {}})
        (d_ver / "state.json").write_text(raw_ver, encoding="utf-8")

        d_meta = self.sessions_root / "20261028-100003-nometa"
        d_meta.mkdir()
        raw_meta = json.dumps({"version": 2, "updated": 3000.0, "cwd": str(self.work), "agents": {}})
        (d_meta / "state.json").write_text(raw_meta, encoding="utf-8")

        self.make_session("20261028-100004-good01", updated=4000.0, first_msg="正常的對話")

        view = PickerView(self.sessions_root, str(self.work))
        self.assertEqual(len(view.all_items), 4)

        for _ in range(len(view.all_items)):
            rows = view.window_rows(110, 26)
            self.assertEqual(len(rows), 26)
            view.move(+1)

        self.assertEqual((d_corrupt / "state.json").read_text(encoding="utf-8"), raw_corrupt)
        self.assertEqual((d_ver / "state.json").read_text(encoding="utf-8"), raw_ver)
        self.assertEqual((d_meta / "state.json").read_text(encoding="utf-8"), raw_meta)

        for bad_sid in ("20261028-100001-badjson", "20261028-100002-badver", "20261028-100003-nometa"):
            with mock.patch.object(cli, "_picker_decision", return_value=(True, None)), \
                    mock.patch("agent.picker.run_picker", return_value=PickResult(action="resume", session_id=bad_sid)):
                code, out, err = self.run_cli_inproc(["pick", "--plain"], "/exit\n")
            self.assertEqual(code, 2, bad_sid)
            self.assertIn("Cannot resume", err, bad_sid)

    def test_18_19_20_preview_is_strictly_readonly_and_never_calls_models(self):
        sdir = self.make_session("20261028-120000-rotest", updated=5000.0, first_msg="唯讀預覽測試")
        with open(sdir / "events.jsonl", "ab") as f:
            f.write(b'{"seq": 99, "type": "chat", "sender": "Me", "text": "half written line without newline"')

        state_before = (sdir / "state.json").read_bytes()
        events_before = (sdir / "events.jsonl").read_bytes()

        view = PickerView(self.sessions_root, str(self.work))
        screen = "\n".join(flat_lines(view.window_rows(110, 26)))
        self.assertIn("唯讀預覽測試", screen)
        self.assertIn("Me -> Manager", screen)
        self.assertIn("Manager -> w1", screen)
        self.assertIn("Worker (w1) -> Manager", screen)

        self.assertEqual((sdir / "state.json").read_bytes(), state_before)
        self.assertEqual((sdir / "events.jsonl").read_bytes(), events_before)
        self.assertFalse((sdir / LOCK_NAME).exists())
        self.assertEqual(self.fake_calls(), [])

    def test_22_and_23_claude_import_source_and_fork_uuids_preserved_and_raw_uuid_ignored(self):
        imp_record = {
            "source_id": CLAUDE_SRC_UUID,
            "fork_id": CLAUDE_FORK_UUID,
            "stage": "prepared",
            "attempts": 0,
            "created": 1700000000.0,
        }
        sdir = self.make_session(
            "20261028-160000-imp001",
            updated=6000.0,
            first_msg="匯入後的問題",
            import_info=imp_record,
        )
        raw_uuid_dir = self.sessions_root / CLAUDE_SRC_UUID
        raw_uuid_dir.mkdir()
        (raw_uuid_dir / "state.json").write_text(json.dumps({"version": 3}), encoding="utf-8")

        claude_dir = self.tmp / "claude_cfg" / "projects" / "-proj"
        claude_dir.mkdir(parents=True)
        transcript = claude_dir / f"{CLAUDE_SRC_UUID}.jsonl"
        transcript.write_text('{"native": "do not touch"}\n', encoding="utf-8")
        transcript_before = transcript.read_bytes()

        with mock.patch.object(importer, "find_native_transcripts", side_effect=AssertionError("must not scan native transcripts")):
            view = PickerView(self.sessions_root, str(self.work))
            self.assertEqual([it.id for it in view.all_items], ["20261028-160000-imp001"])
            sel = view.selected_item
            self.assertEqual(sel.import_source, CLAUDE_SRC_UUID)
            self.assertEqual(sel.import_fork, CLAUDE_FORK_UUID)
            self.assertEqual(sel.import_stage, "prepared")

            screen_txt = "\n".join(flat_lines(view.window_rows(120, 28)))
            self.assertIn(CLAUDE_SRC_UUID, screen_txt)
            self.assertIn(f"[匯入:{CLAUDE_SRC_UUID[:8]}]", screen_txt)
            choice = view.confirm()
            self.assertEqual(choice, PickResult(action="resume", session_id="20261028-160000-imp001"))

        after_state = json.loads((sdir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(after_state["import"]["source_id"], CLAUDE_SRC_UUID)
        self.assertEqual(after_state["import"]["fork_id"], CLAUDE_FORK_UUID)
        self.assertEqual(transcript.read_bytes(), transcript_before)


class TestPerformanceAndFallback(PickerTestBase):
    """Tests 24..28: Large session count, large history tail preview, non-TTY fallback, missing prompt_toolkit, CLI."""

    def test_24_large_number_of_sessions_loads_and_filters_fast(self):
        for i in range(220):
            self.make_session(
                f"20261028-{100000 + i:06d}-{i:06x}",
                updated=1000.0 + i,
                first_msg=f"大量對話測試項目編號 {i} — 處理模組 alpha_{i}",
            )
        t0 = time.monotonic()
        view = PickerView(self.sessions_root, str(self.work))
        load_elapsed = time.monotonic() - t0
        self.assertEqual(len(view.all_items), 220)
        self.assertLess(load_elapsed, 5.0)
        self.assertEqual(view.preview_cache.load_count, 0)

        t1 = time.monotonic()
        for q in ("alpha_199", "編號 42", "20261028-100150", "不存在的查詢"):
            view.set_query(q)
            view.window_rows(100, 24)
        filter_elapsed = time.monotonic() - t1
        self.assertLess(filter_elapsed, 1.0)

    def test_25_large_history_preview_reads_tail_and_caps_at_10_items(self):
        many_events = [
            {
                "seq": i + 10,
                "ts": 1700001000.0 + i,
                "type": "chat",
                "sender": "Manager" if i % 2 == 0 else "w1",
                "to": "w1" if i % 2 == 0 else "Manager",
                "text": f"歷史訊息第 {i} 則：" + ("資料內容填充 " * 8),
            }
            for i in range(2500)
        ]
        sdir = self.make_session("20261028-180000-biglog", updated=99999.0, first_msg="超長歷史紀錄首則訊息", extra_events=many_events)
        self.assertGreater((sdir / "events.jsonl").stat().st_size, 200_000)

        t0 = time.monotonic()
        recent, omitted = read_recent_timeline(sdir, limit=PREVIEW_MAX_ITEMS)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5)
        self.assertEqual(len(recent), PREVIEW_MAX_ITEMS)
        self.assertTrue(omitted)
        self.assertIn("歷史訊息第 2499 則", recent[-1]["text"])
        self.assertIn("歷史訊息第 2490 則", recent[0]["text"])

    def test_26_non_tty_and_dumb_term_fallback_does_not_crash_or_create_session(self):
        self.make_session("20261028-190000-tty001", updated=5000.0, first_msg="非TTY測試對話")
        before_sessions = sorted(p.name for p in self.sessions_root.iterdir())

        env = dict(
            os.environ,
            AGENT_HOME=str(self.home),
            FAKE_SCENARIO=str(self.tmp / "scn.json"),
            FAKE_STATE=str(self.fs),
            PYTHONPATH=str(ROOT),
        )
        (self.tmp / "scn.json").write_text(json.dumps({"manager": [], "w1": []}), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, "-m", "agent", "pick"],
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=self.work,
            timeout=30,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("20261028-190000-tty001", r.stdout)
        self.assertIn("非TTY測試對話", r.stdout)
        self.assertIn("agent resume", r.stderr)
        self.assertEqual(sorted(p.name for p in self.sessions_root.iterdir()), before_sessions)
        self.assertEqual(self.fake_calls(), [])

        tty = mock.Mock(isatty=lambda: True)
        with mock.patch.object(sys, "stdin", tty), mock.patch.object(sys, "stdout", tty), \
                mock.patch.dict(os.environ, {"TERM": "dumb"}):
            use, hint = cli._picker_decision(argparse.Namespace(plain=False))
            self.assertFalse(use)
            self.assertIn("agent resume", hint)

    def test_27_missing_prompt_toolkit_and_screen_startup_failure_handled_safely(self):
        self.make_session("20261028-200000-ptk001", updated=6000.0, first_msg="缺少套件測試")
        tty = mock.Mock(isatty=lambda: True)
        env = {k: v for k, v in os.environ.items() if k not in ("AGENT_PLAIN", "TERM")}
        with mock.patch.object(sys, "stdin", tty), mock.patch.object(sys, "stdout", tty), \
                mock.patch.dict(os.environ, env, clear=True):
            for mod_name in ("prompt_toolkit", "prompt_toolkit.application", "wcwidth"):
                with mock.patch.object(
                    cli.importlib, "import_module", side_effect=ModuleNotFoundError(f"No module named {mod_name!r}", name=mod_name)
                ):
                    use, hint = cli._picker_decision(argparse.Namespace(plain=False))
                    self.assertFalse(use)
                    self.assertIn("pip install", hint)

            with mock.patch.object(cli.importlib, "import_module", side_effect=RuntimeError("unexpected bug")):
                use, hint = cli._picker_decision(argparse.Namespace(plain=False))
                self.assertFalse(use)
                self.assertIn("不是缺少套件", hint)
                self.assertIn("RuntimeError", hint)

        with mock.patch.object(cli, "_picker_decision", return_value=(True, None)), \
                mock.patch("agent.picker.run_picker", side_effect=RuntimeError("console cannot enter alternate screen")):
            code, out, err = self.run_cli_inproc(["pick"], "")
        self.assertEqual(code, 0)
        self.assertIn("20261028-200000-ptk001", out)
        self.assertIn("無法在這個終端啟動", err)
        self.assertEqual(self.fake_calls(), [])

    def test_28_cli_commands_and_rewrite_argv_preserved(self):
        self.assertEqual(cli.rewrite_argv(["pick"]), ["pick"])
        self.assertEqual(cli.rewrite_argv(["--config", "c.json", "pick"]), ["--config", "c.json", "pick"])
        self.assertEqual(cli.rewrite_argv([CLAUDE_SRC_UUID]), ["import", CLAUDE_SRC_UUID])
        self.assertEqual(cli.rewrite_argv([]), [])
        self.assertEqual(cli.rewrite_argv(["sessions"]), ["sessions"])
        self.assertEqual(cli.rewrite_argv(["resume", "20261028-131316-be0f76"]), ["resume", "20261028-131316-be0f76"])

        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["pick"]).command, "pick")
        self.assertEqual(parser.parse_args(["sessions"]).command, "sessions")
        self.assertEqual(parser.parse_args(["resume", "abc"]).command, "resume")
        self.assertIsNone(parser.parse_args([]).command)


class TestPickerAppWiring(PickerTestBase):
    """Tests PickerApp and PickerControl wiring (keybindings, paste handling, buffer filtering)."""

    def test_picker_app_keybindings_and_paste_handling_with_harness(self):
        self.make_session("20261028-100000-s1", updated=100.0, first_msg="第一筆對話")
        self.make_session("20261028-110000-s2", updated=200.0, first_msg="第二筆對話")
        view = PickerView(self.sessions_root, str(self.work))

        class FakeBuffer:
            def __init__(self):
                self.text = ""
                self.cursor_position = 0
                self._handlers = []
                self.on_text_changed = self

            def __iadd__(self, fn):
                self._handlers.append(fn)
                return self

            def insert_text(self, s: str):
                self.text += s
                self.cursor_position = len(self.text)
                for fn in list(self._handlers):
                    fn(self)

            def set_text(self, s: str):
                self.text = s
                self.cursor_position = len(self.text)
                for fn in list(self._handlers):
                    fn(self)

        class FakeTextArea:
            def __init__(self, **kw):
                self.buffer = FakeBuffer()

            @property
            def text(self):
                return self.buffer.text

        class FakeKB:
            def __init__(self):
                self.handlers = {}

            def add(self, *keys, **kw):
                def deco(fn):
                    for k in keys:
                        self.handlers[k] = (fn, kw.get("filter"))
                    return fn
                return deco

        class FakeApp:
            def __init__(self, **kw):
                self.kw = kw
                self.is_running = True
                self.exited = []
                self.output = types.SimpleNamespace(get_size=lambda: types.SimpleNamespace(columns=100, rows=28))

            def invalidate(self):
                pass

            def exit(self, result=None):
                self.exited.append(result)
                self.is_running = False

            def run(self, **kw):
                return self.exited[-1] if self.exited else None

        fake_mods = {
            "Application": FakeApp,
            "Condition": lambda fn: fn,
            "KeyBindings": FakeKB,
            "Keys": types.SimpleNamespace(BracketedPaste="<BracketedPaste>"),
            "Dimension": types.SimpleNamespace(exact=lambda n: n),
            "FormattedTextControl": lambda fn, **kw: fn,
            "HSplit": lambda children, **kw: children,
            "Layout": lambda root, **kw: root,
            "Window": lambda content=None, **kw: content,
            "UIContent": lambda **kw: kw,
            "Style": types.SimpleNamespace(from_dict=lambda d: d),
            "TextArea": FakeTextArea,
        }
        with mock.patch.multiple(picker, **fake_mods):
            papp = PickerApp(view)
            ctrl = PickerControl(view)
            content = ctrl.create_content(100, 20)
            self.assertEqual(content["line_count"], 20)

            kb = papp.app.kw["key_bindings"]
            ev = types.SimpleNamespace(app=papp.app, current_buffer=papp.input.buffer, data="貼上第一行\r\n第二行")

            kb.handlers["down"][0](ev)
            self.assertEqual(view.selected_item.id, "20261028-100000-s1")

            kb.handlers["up"][0](ev)
            self.assertEqual(view.selected_item.id, "20261028-110000-s2")

            kb.handlers["<BracketedPaste>"][0](ev)
            self.assertEqual(papp.input.text, "貼上第一行 第二行")
            self.assertEqual(papp.app.exited, [])

            papp.input.buffer.set_text("第一筆\n換行")
            self.assertEqual(papp.input.text, "第一筆 換行")

            papp.input.buffer.set_text("第一筆")
            self.assertEqual(view.selected_item.id, "20261028-100000-s1")
            kb.handlers["enter"][0](ev)
            self.assertEqual(papp.app.exited[-1], PickResult(action="resume", session_id="20261028-100000-s1"))

            kb.handlers["c-n"][0](ev)
            self.assertEqual(papp.app.exited[-1], PickResult(action="new", session_id=None))

            kb.handlers["escape"][0](ev)
            self.assertEqual(papp.app.exited[-1], PickResult(action="cancel", session_id=None))

    @unittest.skipUnless(HAVE_PTK, "prompt_toolkit not installed")
    def test_real_prompt_toolkit_app_interactive_keys(self):
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        self.make_session("20261028-100000-s1", updated=100.0, first_msg="舊專案整理")
        self.make_session("20261028-110000-s2", updated=200.0, first_msg="新專案開發")

        with create_pipe_input() as pipe:
            view = PickerView(self.sessions_root, str(self.work))
            app = PickerApp(view, input=pipe, output=DummyOutput())
            res_holder = {}
            t = threading.Thread(target=lambda: res_holder.update(res=app.run()), daemon=True)
            t.start()
            time.sleep(0.15)
            pipe.send_text("舊專案\r")
            t.join(5)
            self.assertEqual(res_holder.get("res"), PickResult(action="resume", session_id="20261028-100000-s1"))


if __name__ == "__main__":
    unittest.main()
