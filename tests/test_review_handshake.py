"""Regressao (2026-11-18, cerebro-195 e lastro-rust-280): um revisor Codex reciclado volta sem
agent_session, o dispatcher o recusava ("sessao do revisor nao observavel") e a rodada saia
parcial sem entrega. O primeiro turno controlado tem que dar a sessao, uma unica vez por
PROCESSO (um novo reciclo no mesmo terminal precisa poder repetir), sob a reserva do core."""
import contextlib
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
sys.path.insert(0, BIN)
import _herdr_review_handshake as hs  # noqa: E402

PROC = {"pid": 1, "starttime": "100", "boot_id": "b" * 36}
NATIVE = {"agent": "codex", "kind": "id", "source": "herdr:codex", "value": "sess-1"}


def info(session=None, status="idle", ready=True, terminal="T1"):
    return {"name": "x-rev-1", "agent": "codex", "pane_id": "w1:p1", "terminal_id": terminal,
            "agent_session": session, "interactive_ready": ready, "agent_status": status}


class Core:
    CLI_TIMEOUT_S = 5

    def __init__(self, gets, proc=None):
        self.gets = list(gets)
        self.proc = proc or PROC
        self.cansend = mock.Mock()
        self.locks = []
        self.require_can_send = self.cansend
        self.role_reinforcement_prompt = lambda *a, **k: "PROMPT"
        self.herdr_argv = lambda *a: ["herdr", *a]
        self.now_iso = lambda: "t"
        self.effective_status = lambda i: i["agent_status"]
        self._dispatch_differences = lambda a, b, include_lifecycle=True: []

    @contextlib.contextmanager
    def _dispatch_submission_locks(self, agents):
        self.locks.append(sorted(agents))
        yield

    def validate_agent_project(self, n, r):
        return self.gets[0] if len(self.gets) == 1 else self.gets.pop(0)

    def get_agent_info(self, name):
        return self.gets.pop(0) if len(self.gets) > 1 else self.gets[0]

    def write_json_atomic(self, path, payload):
        with open(path, "w") as f:
            json.dump(payload, f)


def run(core, root, groups=None, proc=None):
    rg = SimpleNamespace(group_for_agent=lambda n: groups)
    sent = []
    clock = iter(range(0, 10000))
    p = proc or core.proc
    with mock.patch.object(hs, "process_key", side_effect=lambda *a: dict(p)), \
         mock.patch.object(hs.subprocess, "run",
                           side_effect=lambda *a, **k: sent.append(1) or SimpleNamespace(returncode=0, stdout="", stderr="")):
        try:
            return hs.ensure_session("x-rev-1", root, "x", ["x-exec"], core, rg, timeout_s=50,
                                     sleep=lambda s: None, monotonic=lambda: next(clock)), len(sent)
        except RuntimeError:
            core.sent = len(sent)
            raise


class Handshake(unittest.TestCase):
    def test_recycled_codex_without_session_gets_one_first_turn_and_a_session(self):
        with tempfile.TemporaryDirectory() as root:
            core = Core([info(None), info(None), info(None), info(NATIVE, "working"), info(NATIVE)])
            result, sent = run(core, root)
            self.assertEqual(sent, 1)
            self.assertEqual(result["agent_session"], NATIVE)
            self.assertEqual(core.locks, [["x-rev-1"]])  # sob a reserva de submissao do core
            self.assertEqual(core.cansend.call_count, 2)  # antes e depois do I/O duravel

    def test_agent_with_session_is_untouched_and_takes_no_lock(self):
        with tempfile.TemporaryDirectory() as root:
            core = Core([info(NATIVE)])
            _, sent = run(core, root)
            self.assertEqual((sent, core.locks), (0, []))

    def test_same_process_that_stays_sessionless_is_refused_without_sending(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RuntimeError):
                run(Core([info(None)]), root)       # nunca assenta: ambiguo
            c2 = Core([info(None)])
            with self.assertRaisesRegex(RuntimeError, "não reenviar.*apague"):
                run(c2, root)
            self.assertEqual(c2.sent, 0)

    def test_new_recycle_on_same_terminal_gets_a_new_attempt(self):
        # achado r182/rev-2 P2: o restart mantem o terminal_id; so o processo muda.
        with tempfile.TemporaryDirectory() as root:
            core = Core([info(None), info(None), info(None), info(NATIVE)])
            run(core, root)
            core2 = Core([info(None), info(None), info(None), info(NATIVE)])
            _, sent = run(core2, root, proc={"pid": 2, "starttime": "200", "boot_id": "b"})
            self.assertEqual(sent, 1)

    def test_can_send_refusal_sends_nothing_and_does_not_burn_the_attempt(self):
        # achado r182/rev-1 e rev-2 P2: recusa transitoria (rascunho humano) nao consome.
        with tempfile.TemporaryDirectory() as root:
            core = Core([info(None)])
            core.cansend.side_effect = RuntimeError("NAO ENVIE")
            with self.assertRaises(RuntimeError):
                run(core, root)
            self.assertEqual(core.sent, 0)
            ok = Core([info(None), info(None), info(None), info(NATIVE)])
            _, sent = run(ok, root)
            self.assertEqual(sent, 1)

    def test_guard_refusal_after_durable_receipt_is_retryable_too(self):
        with tempfile.TemporaryDirectory() as root:
            core = Core([info(None)])
            core.cansend.side_effect = [None, RuntimeError("NAO ENVIE")]
            with self.assertRaises(RuntimeError):
                run(core, root)
            self.assertEqual(core.sent, 0)
            ok = Core([info(None), info(None), info(None), info(NATIVE)])
            _, sent = run(ok, root)
            self.assertEqual(sent, 1)

    def test_group_role_is_not_handshaken_here(self):
        with tempfile.TemporaryDirectory() as root:
            core = Core([info(None)])
            with self.assertRaisesRegex(RuntimeError, "hidratação inicial"):
                run(core, root, groups=object())
            self.assertEqual(core.sent, 0)

    def test_working_or_not_ready_codex_is_not_a_candidate(self):
        self.assertFalse(hs.needs_handshake(info(None, status="working")))
        self.assertFalse(hs.needs_handshake(info(None, ready=False)))
        self.assertFalse(hs.needs_handshake({**info(None), "agent": "claude"}))
        self.assertTrue(hs.needs_handshake(info(None)))

    def test_two_concurrent_dispatchers_send_at_most_one_prompt(self):
        # achado r182/rev-1 P2: a exclusao vem do lock REAL do core. Patches instalados UMA vez,
        # na thread principal (achado r183/rev-2 P1: patch dentro de thread vazava o mock).
        import threading
        import _herdr_dispatch as real
        with tempfile.TemporaryDirectory() as root:
            sent, errors = [], []
            fake_sp = SimpleNamespace(run=lambda *a, **k: sent.append(1) or SimpleNamespace(returncode=0),
                                      TimeoutExpired=hs.subprocess.TimeoutExpired)

            def worker():
                core = Core([info(None)])
                core.get_agent_info = lambda n: info(NATIVE) if sent else info(None)
                core.validate_agent_project = lambda n, r: info(NATIVE) if sent else info(None)
                core._dispatch_submission_locks = real._dispatch_submission_locks
                rg = SimpleNamespace(group_for_agent=lambda n: None)
                try:
                    hs.ensure_session("x-rev-1", root, "x", [], core, rg, timeout_s=5, sleep=lambda s: None)
                except RuntimeError as exc:
                    errors.append(str(exc))
            with mock.patch.object(real, "DISPATCH_LOCK_WAIT_S", 5), \
                 mock.patch.object(real.tempfile, "gettempdir", return_value=root), \
                 mock.patch.object(hs, "process_key", return_value=PROC), \
                 mock.patch.object(hs, "subprocess", fake_sp):
                threads = [threading.Thread(target=worker) for _ in range(2)]
                [t.start() for t in threads]
                [t.join() for t in threads]
            self.assertEqual(len(sent), 1, errors)

    # --- r183/rev-1 -------------------------------------------------------------------------
    def _write_receipt(self, root, core, proc, **fields):
        path = hs.receipt_path(root, "x-rev-1", info(None), proc)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(fields, f)
        return path

    def test_unknown_or_future_or_stateless_receipt_blocks_and_is_preserved(self):
        for fields in ({"version": 999, "state": "unknown_delivery"}, {"version": 1},
                       {"version": 1, "state": "weird"}, {"state": "aborted_before_send"}):
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as root:
                core = Core([info(None)])
                path = self._write_receipt(root, core, PROC, **fields)
                with self.assertRaisesRegex(RuntimeError, "não reenviar"):
                    run(core, root)
                self.assertEqual(core.sent, 0)
                self.assertEqual(json.load(open(path)), fields)

    def test_valid_aborted_before_send_receipt_is_retryable(self):
        with tempfile.TemporaryDirectory() as root:
            core = Core([info(None), info(None), info(None), info(None), info(NATIVE)])
            self._write_receipt(root, core, PROC, version=1, state="aborted_before_send")
            _, sent = run(core, root)
            self.assertEqual(sent, 1)

    def test_legacy_ambiguous_receipt_blocks_after_code_update(self):
        with tempfile.TemporaryDirectory() as root:
            core = Core([info(None)])
            legacy = hs.legacy_receipt_path(root, "x-rev-1", info(None))
            os.makedirs(os.path.dirname(legacy), exist_ok=True)
            json.dump({"version": 1, "state": "ambiguous"}, open(legacy, "w"))
            with self.assertRaisesRegex(RuntimeError, "formato anterior"):
                run(core, root)
            self.assertEqual(core.sent, 0)

    def test_legacy_complete_receipt_without_physical_link_blocks(self):
        # achado r184/rev-1 P2-2: perder a metadata da sessao nao prova troca de processo.
        with tempfile.TemporaryDirectory() as root:
            core = Core([info(None)])
            legacy = hs.legacy_receipt_path(root, "x-rev-1", info(None))
            os.makedirs(os.path.dirname(legacy), exist_ok=True)
            json.dump({"version": 1, "state": "complete"}, open(legacy, "w"))
            with self.assertRaisesRegex(RuntimeError, "formato anterior"):
                run(core, root)
            self.assertEqual(core.sent, 0)
            self.assertTrue(os.path.exists(legacy))

    def test_legacy_aborted_before_send_is_not_blocking(self):
        with tempfile.TemporaryDirectory() as root:
            core = Core([info(None), info(None), info(None), info(None), info(NATIVE)])
            legacy = hs.legacy_receipt_path(root, "x-rev-1", info(None))
            os.makedirs(os.path.dirname(legacy), exist_ok=True)
            json.dump({"version": 1, "state": "aborted_before_send"}, open(legacy, "w"))
            _, sent = run(core, root)
            self.assertEqual(sent, 1)

    def test_r183_receipt_of_the_same_incarnation_blocks_in_every_sent_state(self):
        # achado r184/rev-1 P2-1
        for state in ("delivery_attempted", "delivery_unconfirmed", "ambiguous", "complete"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as root:
                core = Core([info(None)])
                path = hs.intermediate_receipt_path(root, "x-rev-1", info(None), PROC)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                json.dump({"version": 1, "state": state, "process": PROC}, open(path, "w"))
                with self.assertRaisesRegex(RuntimeError, "formato anterior"):
                    run(core, root)
                self.assertEqual(core.sent, 0)
                self.assertTrue(os.path.exists(path))

    def test_r183_receipt_of_a_different_process_does_not_block_and_aborted_is_repeatable(self):
        with tempfile.TemporaryDirectory() as root:
            old = {**PROC, "pid": 77, "starttime": "5"}
            path = hs.intermediate_receipt_path(root, "x-rev-1", info(None), old)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            json.dump({"version": 1, "state": "ambiguous", "process": old}, open(path, "w"))
            core = Core([info(None), info(None), info(None), info(None), info(NATIVE)])
            _, sent = run(core, root)
            self.assertEqual(sent, 1)
        with tempfile.TemporaryDirectory() as root:
            path = hs.intermediate_receipt_path(root, "x-rev-1", info(None), PROC)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            json.dump({"version": 1, "state": "aborted_before_send"}, open(path, "w"))
            core = Core([info(None), info(None), info(None), info(None), info(NATIVE)])
            _, sent = run(core, root)
            self.assertEqual(sent, 1)

    def test_same_pid_and_starttime_in_another_boot_is_a_different_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_receipt(root, None, PROC, version=1, state="ambiguous")
            other_boot = {**PROC, "boot_id": "ffffffffffff-another-boot"}
            core = Core([info(None), info(None), info(None), info(None), info(NATIVE)], proc=other_boot)
            _, sent = run(core, root, proc=other_boot)
            self.assertEqual(sent, 1)

    def test_process_swapped_during_durable_io_sends_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            core = Core([info(None)])
            procs = iter([PROC, PROC, {**PROC, "pid": 22, "starttime": "999"}])
            sent = []
            fake_sp = SimpleNamespace(run=lambda *a, **k: sent.append(1) or SimpleNamespace(returncode=0),
                                      TimeoutExpired=hs.subprocess.TimeoutExpired)
            rg = SimpleNamespace(group_for_agent=lambda n: None)
            with mock.patch.object(hs, "process_key", side_effect=lambda *a: next(procs)), \
                 mock.patch.object(hs, "subprocess", fake_sp):
                with self.assertRaises(RuntimeError):
                    hs.ensure_session("x-rev-1", root, "x", [], core, rg, timeout_s=5, sleep=lambda s: None)
            self.assertEqual(sent, [])

    def test_process_swapped_after_send_never_completes_with_the_other_sessions(self):
        with tempfile.TemporaryDirectory() as root:
            core = Core([info(None), info(None), info(None), info(None), info(NATIVE)])
            procs = iter([PROC, PROC, PROC, {**PROC, "pid": 22, "starttime": "999"}])
            sent = []
            fake_sp = SimpleNamespace(run=lambda *a, **k: sent.append(1) or SimpleNamespace(returncode=0),
                                      TimeoutExpired=hs.subprocess.TimeoutExpired)
            rg = SimpleNamespace(group_for_agent=lambda n: None)
            with mock.patch.object(hs, "process_key", side_effect=lambda *a: next(procs, PROC)), \
                 mock.patch.object(hs, "subprocess", fake_sp):
                with self.assertRaises(RuntimeError):
                    hs.ensure_session("x-rev-1", root, "x", [], core, rg, timeout_s=5, sleep=lambda s: None)
            self.assertEqual(len(sent), 1)
            files = os.listdir(os.path.join(root, ".herdr", "review", "_handshake"))
            states = [json.load(open(os.path.join(root, ".herdr", "review", "_handshake", f)))["state"] for f in files]
            self.assertNotIn("complete", states)

    def test_target_pane_changed_between_read_and_reservation_aborts(self):
        with tempfile.TemporaryDirectory() as root:
            core = Core([info(None)])
            other = {**info(None), "pane_id": "w1:p9"}
            seq = iter([info(None), other])
            core.validate_agent_project = lambda n, r: next(seq)
            core._dispatch_differences = lambda a, b, include_lifecycle=True: (
                ["pane_id"] if a["pane_id"] != b["pane_id"] else [])
            with self.assertRaisesRegex(RuntimeError, "mudou entre"):
                run(core, root)
            self.assertEqual(core.sent, 0)

    # --- nivel 1 (r183/rev-2) -------------------------------------------------------------
    def _select(self, states, groups=()):
        core = SimpleNamespace(
            validate_agent_project=lambda n, r: states[n] if not isinstance(states[n], Exception) else (_ for _ in ()).throw(states[n]),
            effective_status=lambda i: i["agent_status"],
            _is_agent_not_found=lambda e: "not found" in str(e))
        rg = SimpleNamespace(group_for_agent=lambda n: object() if n in groups else None)
        def strict(n, r):  # como review_candidate: sem sessao => recusa
            if states[n].get("agent_session") is None:
                raise RuntimeError("sessão do revisor não observável")
            return states[n]
        return hs.select_level1_candidates(list(states), "/r", core, rg, strict)

    def test_level1_prefers_ready_over_recyclable(self):
        self.assertEqual(self._select({"a": info(None), "b": info(NATIVE)}), ["b"])

    def test_level1_picks_recyclable_codex_when_peer_is_working(self):
        self.assertEqual(self._select({"a": info(None), "b": info(NATIVE, "working")}), ["a"])

    def test_level1_recyclable_group_role_is_not_a_candidate(self):
        self.assertEqual(self._select({"a": info(None)}, groups=("a",)), [])

    def test_level1_falls_back_to_missing(self):
        self.assertEqual(self._select({"a": RuntimeError("agent not found")}), ["a"])


class NoGlobalLeak(unittest.TestCase):
    def test_zz_subprocess_run_is_the_real_one_after_this_module(self):
        # sentinela (r183/rev-2): nenhum teste deste arquivo pode deixar mock global.
        import subprocess
        self.assertFalse(isinstance(subprocess.run, mock.Base) or
                         subprocess.run.__module__ != "subprocess", subprocess.run)


if __name__ == "__main__":
    unittest.main()


class WaitQuiescent(unittest.TestCase):
    """llm-gateway-26: idle antes do turno de boot comecar nao pode valer como pronto."""

    def _run(self, seq):
        import _herdr_dispatch as real
        states = iter(seq)
        t = [0.0]

        def mono():
            return t[0]

        def slp(x):
            t[0] += x
        with mock.patch.object(real, "get_agent_info", side_effect=lambda n: next(states)), \
             mock.patch.object(real, "effective_status", side_effect=lambda i: i["agent_status"]):
            return real.wait_quiescent("n", quiet_s=2.0, timeout_s=60, sleep=slp, monotonic=mono)

    def test_idle_that_turns_working_is_not_accepted_until_it_settles_again(self):
        idle = lambda seq: {"agent_status": "idle", "state_change_seq": seq}
        work = {"agent_status": "working", "state_change_seq": 6}
        got = self._run([idle(5), work, work, idle(7)] + [idle(7)] * 10)
        self.assertEqual(got["state_change_seq"], 7)

    def test_stable_idle_returns(self):
        got = self._run([{"agent_status": "idle", "state_change_seq": 5}] * 20)
        self.assertEqual(got["state_change_seq"], 5)

    def test_never_settling_agent_raises_instead_of_dispatching(self):
        import _herdr_dispatch as real
        n = iter(range(10000))
        with self.assertRaises(real.AgentRestoreError):
            self._run([{"agent_status": "idle", "state_change_seq": next(n)} for _ in range(200)])
