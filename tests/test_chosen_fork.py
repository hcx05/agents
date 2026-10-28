"""Fork under a chosen session id (`--resume A --fork-session --session-id B`): adapter rules, fake-CLI behaviour and the
probe experiment. Fake CLI only; the real-CLI behaviour is checked on Windows with `probe.py claude --tests fork-chosen`."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import protocol  # noqa: E402
from agent.adapters.agy import AgyAdapter  # noqa: E402
from agent.adapters.base import TurnRequest, TurnResult  # noqa: E402
from agent.adapters.claude import ClaudeAdapter  # noqa: E402
from agent.adapters.fake import FakeAdapter  # noqa: E402
from agent.config import AgentCfg  # noqa: E402
from tests.test_models_fork import FORBIDDEN, SpyAdapter, claude_parse, ev  # noqa: E402

A = "11111111-2222-3333-4444-555555555555"
B = "99999999-8888-7777-6666-555555555555"
OK = lambda sid: ev(type="result", subtype="success", is_error=False, session_id=sid, result="r",  # noqa: E731
                    structured_output={"say": "", "actions": []})


def fork_req(**kw):
    base = dict(agent_id="a", prompt="x", cwd=".", session_id=A, schema={"type": "object"}, fork=True, new_session_id=B)
    base.update(kw)
    return TurnRequest(**base)


class TestChosenForkBuild(unittest.TestCase):
    ad = ClaudeAdapter(AgentCfg("a", "manager", "claude"))

    def test_arguments(self):
        inv = self.ad.build(fork_req(), ["x"])
        self.assertEqual(inv.args[inv.args.index("--resume") + 1], A)
        self.assertEqual(inv.args[inv.args.index("--session-id") + 1], B)
        self.assertIn("--fork-session", inv.args)
        self.assertFalse(FORBIDDEN & set(inv.args))

    def test_a_plain_fork_and_a_plain_resume_never_carry_session_id(self):
        self.assertNotIn("--session-id", self.ad.build(fork_req(new_session_id=None), ["x"]).args)
        self.assertNotIn("--session-id", self.ad.build(TurnRequest("a", "x", ".", B), ["x"]).args)

    def test_bad_combinations_are_refused_before_any_process_starts(self):
        for kw in (dict(fork=False), dict(session_id=None), dict(new_session_id="not-a-uuid"), dict(new_session_id=A),
                   dict(new_session_id=A.upper()), dict(new_session_id='x"; calc')):
            with self.assertRaises(ValueError, msg=kw):
                self.ad.build(fork_req(**kw), ["x"])

    def test_agy_still_cannot_fork(self):
        with self.assertRaises(ValueError):
            AgyAdapter(AgentCfg("a", "worker", "agy")).build(fork_req(), ["x"])


class TestChosenForkParser(unittest.TestCase):
    def test_exact_chosen_id_succeeds_and_the_engine_is_never_told_an_id(self):
        res, seen = claude_parse([ev(type="system", subtype="hook_started", session_id=A),
                                  ev(type="system", subtype="init", session_id=B, model="m"), OK(B)], fork_req())
        self.assertTrue(res.ok)
        self.assertEqual((res.session_id, seen), (B, []))

    def test_any_other_new_id_is_refused_and_not_adopted(self):
        other = "00000000-0000-0000-0000-000000000000"
        res, seen = claude_parse([ev(type="system", subtype="init", session_id=other), OK(other)], fork_req())
        self.assertEqual((res.ok, res.error_kind, res.session_id, seen), (False, "fork_failed", None, []))

    def test_chosen_id_followed_by_a_different_id_is_refused(self):
        res, seen = claude_parse([ev(type="system", subtype="init", session_id=B), OK("zzz")], fork_req())
        self.assertEqual((res.ok, res.error_kind, res.session_id), (False, "fork_failed", None))

    def test_a_cli_that_ignores_the_flag_and_stays_on_the_original_is_refused(self):
        res, _ = claude_parse([ev(type="system", subtype="init", session_id=A), OK(A)], fork_req())
        self.assertEqual((res.ok, res.error_kind, res.session_id), (False, "fork_failed", None))

    def test_no_session_id_at_all_is_refused(self):
        res, _ = claude_parse([ev(type="result", subtype="success", is_error=False, result="r",
                                  structured_output={"say": "", "actions": []})], fork_req())
        self.assertEqual((res.ok, res.error_kind), (False, "fork_failed"))

    def test_already_in_use_is_its_own_error_kind(self):
        res, seen = claude_parse([], fork_req(), stderr=[f"Error: Session ID {B} is already in use."], exit_code=1)
        self.assertEqual((res.ok, res.error_kind, res.session_id, seen), (False, "fork_exists", None, []))

    def test_an_early_id_event_followed_by_failure_is_not_a_success(self):
        res, _ = claude_parse([ev(type="system", subtype="init", session_id=B),
                               ev(type="result", subtype="error_during_execution", is_error=True, session_id=B,
                                  errors=["boom"])], fork_req())
        self.assertFalse(res.ok)

    def test_login_failure_still_reported_as_auth_error(self):
        res, _ = claude_parse([ev(type="system", subtype="init", session_id=B),
                               ev(type="result", subtype="success", is_error=True, session_id=B,
                                  result="Not logged in · Please run /login", api_error_status=401)], fork_req())
        self.assertEqual(res.error_kind, "auth_error")

    def test_the_old_adopt_first_new_id_mode_is_unchanged_when_no_id_is_chosen(self):
        res, seen = claude_parse([ev(type="system", subtype="init", session_id="NEW"), OK("NEW")], fork_req(new_session_id=None))
        self.assertEqual((res.ok, seen), (True, ["NEW"]))


class TestChosenForkWithFakeCli(unittest.TestCase):
    R = {"reply": {"say": "ok", "actions": []}}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent chosen "))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fs = self.tmp / "fs"
        self.fs.mkdir()
        os.environ["FAKE_STATE"] = str(self.fs)
        os.environ["FAKE_SCENARIO"] = str(self.tmp / "scn.json")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_STATE", "FAKE_SCENARIO")])
        self.ad = FakeAdapter(AgentCfg("a", "manager", "fake"))

    def scenario(self, *steps):
        (self.tmp / "scn.json").write_text(json.dumps({"a": list(steps)}), encoding="utf-8")

    def run_turn(self, **kw):
        saved = []
        base = dict(agent_id="a", prompt="hello", cwd=str(self.tmp), session_id=None, timeout=30)
        base.update(kw)
        return self.ad.run(TurnRequest(**base), threading.Event(), saved.append), saved

    def calls(self):
        return [json.loads(l) for l in (self.fs / "calls.jsonl").read_text(encoding="utf-8").splitlines()]

    def sfile(self, sid):
        return self.fs / "sessions" / (sid + ".json")

    def origin(self):
        _, saved = self.run_turn()
        return saved[0], self.sfile(saved[0]).read_bytes()

    def test_fork_takes_exactly_the_chosen_id_and_leaves_the_original_byte_identical(self):
        self.scenario(self.R, self.R, self.R)
        orig, before = self.origin()
        b = str(uuid.uuid4())
        r, saved = self.run_turn(session_id=orig, fork=True, new_session_id=b)
        self.assertTrue(r.ok, r.error)
        self.assertEqual((r.session_id, saved), (b, []))
        self.assertEqual(json.loads(self.sfile(b).read_text(encoding="utf-8"))["forked_from"], orig)
        self.assertEqual(self.sfile(orig).read_bytes(), before)
        r3, saved3 = self.run_turn(session_id=b)                                  # later turns: plain resume of B
        self.assertTrue(r3.ok, r3.error)
        self.assertEqual((r3.session_id, saved3), (b, []))
        self.assertNotIn("--fork-session", self.calls()[-1]["argv"])
        self.assertNotIn("--session-id", self.calls()[-1]["argv"])
        self.assertEqual(self.sfile(orig).read_bytes(), before)
        self.assertEqual([c["session"] for c in self.calls()].count(orig), 1)     # the original was only ever created

    def test_the_same_id_twice_is_refused_without_touching_either_session_or_the_scenario(self):
        self.scenario(self.R, self.R)                                             # NO third step: a duplicate must not consume one
        orig, before = self.origin()
        b = str(uuid.uuid4())
        self.assertTrue(self.run_turn(session_id=orig, fork=True, new_session_id=b)[0].ok)
        fork_bytes = self.sfile(b).read_bytes()
        r, saved = self.run_turn(session_id=orig, fork=True, new_session_id=b)
        self.assertEqual((r.ok, r.error_kind, saved), (False, "fork_exists", []))
        self.assertEqual(self.sfile(b).read_bytes(), fork_bytes)
        self.assertEqual(self.sfile(orig).read_bytes(), before)
        self.assertIn("already in use", self.calls()[-1]["rejected"])

    def test_a_cli_that_ignores_session_id_is_refused_and_nothing_is_adopted(self):
        self.scenario(self.R, dict(self.R, fork_ignores_session_id=True))
        orig, before = self.origin()
        r, saved = self.run_turn(session_id=orig, fork=True, new_session_id=str(uuid.uuid4()))
        self.assertEqual((r.ok, r.error_kind, r.session_id, saved), (False, "fork_failed", None, []))
        self.assertEqual(self.sfile(orig).read_bytes(), before)

    def test_a_cli_that_keeps_the_original_id_is_refused(self):
        self.scenario(self.R, dict(self.R, fork_keeps_id=True))
        orig, _ = self.origin()
        r, saved = self.run_turn(session_id=orig, fork=True, new_session_id=str(uuid.uuid4()))
        self.assertEqual((r.ok, r.error_kind, saved), (False, "fork_failed", []))

    def test_fork_from_a_missing_original_is_session_lost_and_creates_nothing(self):
        self.scenario(self.R)
        b = str(uuid.uuid4())
        r, saved = self.run_turn(session_id=str(uuid.uuid4()), fork=True, new_session_id=b)
        self.assertEqual((r.ok, r.error_kind, saved), (False, "session_lost", []))
        self.assertFalse(self.sfile(b).exists())

    def test_session_id_without_fork_is_rejected_by_the_cli_like_the_real_one(self):
        self.scenario(self.R, self.R)
        orig, _ = self.origin()
        import subprocess
        p = subprocess.run([sys.executable, str(Path(self.ad.command_prefix()[-1])), "--resume", orig, "--session-id",
                            str(uuid.uuid4()), "--fake-agent", "a"], input="x", capture_output=True, text=True,
                           env=dict(os.environ))
        self.assertEqual(p.returncode, 1)
        self.assertIn("--fork-session", p.stderr)


class TestChosenForkProbe(unittest.TestCase):
    ORIG = A

    def setUp(self):
        import probe
        self.probe = probe
        self.tmp = Path(tempfile.mkdtemp(prefix="agent chosen probe "))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp)})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        probe.RESULTS.clear()
        probe.OPTS.update(session_id=None, confirm_fork=False)
        self.proj = self.tmp / "projects" / "-p"
        self.proj.mkdir(parents=True)
        self.transcript = self.proj / f"{self.ORIG}.jsonl"
        self.transcript.write_bytes(b'{"entrypoint":"cli"}\n')
        os.utime(self.transcript, (1, 1))

    def statuses(self):
        return dict(((n, st) for n, st, _ in self.probe.RESULTS))

    def test_stops_inside_a_claude_code_shell_before_any_cli_call(self):
        spy = SpyAdapter()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "abc"}):
            self.probe.OPTS.update(session_id=self.ORIG, confirm_fork=True)
            self.probe.probe_fork_chosen_headless(spy, ["claude"], str(self.tmp))
            self.probe.probe_fork_chosen_interactive(spy, ["claude"], str(self.tmp))
        self.assertEqual(spy.requests, [])
        self.assertEqual(list(self.statuses().values()), ["STOPPED", "STOPPED"])

    def test_interactive_variant_needs_a_known_idle_session_and_confirmation(self):
        spy = SpyAdapter()
        probe = self.probe
        for opts, needle in ((dict(session_id=None), "--session-id"), (dict(session_id="nope"), "--session-id"),
                             (dict(session_id=B), "no single transcript"), (dict(session_id=self.ORIG), "--confirm-fork")):
            probe.RESULTS.clear()
            probe.OPTS.update(session_id=None, confirm_fork=False)
            probe.OPTS.update(opts)
            probe.probe_fork_chosen_interactive(spy, ["claude"], str(self.tmp))
            self.assertEqual(list(self.statuses().values()), ["STOPPED"], opts)
            self.assertIn(needle, probe.RESULTS[0][2])
        probe.RESULTS.clear()
        os.utime(self.transcript, None)
        probe.OPTS.update(session_id=self.ORIG, confirm_fork=True)
        probe.probe_fork_chosen_interactive(spy, ["claude"], str(self.tmp))
        self.assertIn("/exit", probe.RESULTS[0][2])
        self.assertEqual(spy.requests, [])

    def scripted(self, duplicate_refused=True, honours_id=True):
        """An adapter standing in for a CLI: fork under the chosen id, refuse a duplicate, resume the fork."""
        outer = self
        class Scripted:
            requests = []

            def run(self, req, cancel, on_session, on_raw=None):
                self.requests.append(req)
                if req.fork and not [r for r in self.requests[:-1] if r.fork]:
                    sid = req.new_session_id if honours_id else str(uuid.uuid4())
                    (outer.proj / f"{sid}.jsonl").write_bytes(b"fork copy")
                    return TurnResult(True, text='{"say":"forked"}', session_id=sid if honours_id else None) if honours_id \
                        else TurnResult(False, error_kind="fork_failed", error="not the chosen id")
                if req.fork:
                    return TurnResult(False, error_kind="fork_exists", error="in use") if duplicate_refused \
                        else TurnResult(True, text='{"say":"again"}', session_id=req.new_session_id)
                return TurnResult(True, text='{"say":"PINEAPPLE-42"}', session_id=req.session_id)
        return Scripted()

    def run_check(self, ad):
        self.probe.chosen_fork_check(ad, str(self.tmp), self.ORIG, self.transcript, "fc", code_word="PINEAPPLE")
        return self.statuses()

    def test_all_checks_pass_with_a_cli_that_behaves(self):
        ad = self.scripted()
        st = self.run_check(ad)
        self.assertNotIn("FAIL", st.values(), self.probe.RESULTS)
        self.assertEqual(st["fc: reported session id is exactly the chosen id B"], "PASS")
        self.assertEqual(st["fc: creating the same B again is refused"], "PASS")
        self.assertEqual(st["fc: fork B has the earlier conversation"], "PASS")
        self.assertEqual(st["fc: original transcript still unchanged after all calls"], "PASS")
        # the original id is only ever the --resume source of a fork request
        self.assertEqual([(r.session_id == self.ORIG, r.fork) for r in ad.requests], [(True, True), (True, True), (False, False)])
        self.assertEqual(self.transcript.read_bytes(), b'{"entrypoint":"cli"}\n')

    def test_a_duplicate_that_is_accepted_is_a_failure(self):
        st = self.run_check(self.scripted(duplicate_refused=False))
        self.assertEqual(st["fc: creating the same B again is refused"], "FAIL")

    def test_a_cli_that_does_not_honour_the_chosen_id_fails_and_stops_without_any_workaround(self):
        ad = self.scripted(honours_id=False)
        st = self.run_check(ad)
        self.assertEqual(st["fc: --resume A --fork-session --session-id B accepted"], "FAIL")
        self.assertEqual(len(ad.requests), 1)                                      # no retry, no "accept any id" fallback


class TestProbeSourceHasNoPermissionBypass(unittest.TestCase):
    def test_probe_and_adapter_never_mention_forbidden_flags_in_code(self):
        root = Path(__file__).resolve().parent.parent
        for rel in ("probe.py", "agent/adapters/claude.py"):
            text = "\n".join(l for l in (root / rel).read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
            for flag in ("--dangerously-skip-permissions", "--permission-mode", "--allowedTools", "--bare"):
                self.assertNotIn(f'"{flag}"', text, (rel, flag))


if __name__ == "__main__":
    unittest.main()
