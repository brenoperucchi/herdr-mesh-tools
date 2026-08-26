#!/usr/bin/env python3
"""Regression tests for the blocked-vs-transient settle logic shared by
herdr-review-dispatch (wait_for_settle, multi-agent) and herdr-swap-exec
(dispatch_and_wait, single-agent via `agent prompt --wait`).

These formalize the ad-hoc simulations used to validate BLOCKED_GRACE_S so
the same scenarios are checked on every change instead of re-derived by hand
each time — this repo's own history has two fixes in sequence on the same
settle loop (round-1 fix locked in blocked immediately; round-2 fix over-
corrected into consuming the full timeout), so the loop is exactly the kind
of code that regresses quietly without a repeatable check.

Run: python3 -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load(name):
    path = os.path.join(BIN_DIR, name)
    loader = SourceFileLoader(name.replace("-", "_"), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class FakeClock:
    """Deterministic stand-in for time.time()/time.sleep() — advances only
    when sleep() is called, so tests run instantly regardless of the
    simulated timeout/grace values."""

    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class WaitForSettleTests(unittest.TestCase):
    """herdr-review-dispatch: wait_for_settle(), multi-agent concurrent."""

    def setUp(self):
        self.mod = _load("herdr-review-dispatch")
        self.clock = FakeClock()
        for patcher in (
            mock.patch.object(self.mod.time, "time", self.clock.time),
            mock.patch.object(self.mod.time, "sleep", self.clock.sleep),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _dispatch_info(seqs):
        return {name: {"seq_before": seq} for name, seq in seqs.items()}

    def test_sustained_blocked_reports_early_not_full_timeout(self):
        dispatch_info = self._dispatch_info({"rev": 5})
        fake_api = mock.Mock(return_value={"agent": {"agent_status": "blocked", "state_change_seq": 6}})
        with mock.patch.object(self.mod, "api", fake_api):
            result, settle_ts = self.mod.wait_for_settle(["rev"], dispatch_info, timeout_s=1200)
        self.assertEqual(result["rev"], "blocked")
        self.assertIn("rev", settle_ts)
        self.assertGreaterEqual(self.clock.t, self.mod.BLOCKED_GRACE_S)
        self.assertLess(self.clock.t, 1200, "bloqueio sustentado nao deveria esperar o --timeout inteiro")

    def test_transient_blip_settles_normally_not_blocked(self):
        dispatch_info = self._dispatch_info({"rev": 5})
        statuses = iter([
            {"agent_status": "blocked", "state_change_seq": 6},
            {"agent_status": "working", "state_change_seq": 6},
            {"agent_status": "idle", "state_change_seq": 7},
        ])
        fake_api = mock.Mock(side_effect=lambda *a: {"agent": next(statuses)})
        with mock.patch.object(self.mod, "api", fake_api):
            result, _ = self.mod.wait_for_settle(["rev"], dispatch_info, timeout_s=1200)
        self.assertEqual(result["rev"], "idle", "blip transitorio nao deveria ser reportado como blocked")

    def test_real_timeout_when_nothing_settles(self):
        dispatch_info = self._dispatch_info({"rev": 5})
        fake_api = mock.Mock(return_value={"agent": {"agent_status": "working", "state_change_seq": 5}})
        with mock.patch.object(self.mod, "api", fake_api):
            result, _ = self.mod.wait_for_settle(["rev"], dispatch_info, timeout_s=60)
        self.assertEqual(result["rev"], "timeout")
        self.assertAlmostEqual(self.clock.t, 60, delta=3)

    def test_two_agents_one_stuck_one_settles_independently(self):
        dispatch_info = self._dispatch_info({"rev": 5, "rev2": 9})

        def fake_api(*args):
            name = args[2]
            if name == "rev":
                return {"agent": {"agent_status": "blocked", "state_change_seq": 6}}
            return {"agent": {"agent_status": "idle", "state_change_seq": 10}}

        with mock.patch.object(self.mod, "api", fake_api):
            result, _ = self.mod.wait_for_settle(["rev", "rev2"], dispatch_info, timeout_s=1200)
        self.assertEqual(result["rev"], "blocked")
        self.assertEqual(result["rev2"], "idle")
        self.assertLess(self.clock.t, 1200, "um agent travado nao deveria atrasar a detecao do outro")


class DispatchAndWaitTests(unittest.TestCase):
    """herdr-swap-exec: dispatch_and_wait(), single-agent via `agent prompt --wait`."""

    def setUp(self):
        self.mod = _load("herdr-swap-exec")
        self.clock = FakeClock()
        for patcher in (
            mock.patch.object(self.mod.time, "time", self.clock.time),
            mock.patch.object(self.mod.time, "sleep", self.clock.sleep),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _never_exits_proc():
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.communicate.return_value = ("", "")
        return proc

    def test_sustained_blocked_reports_early_not_full_timeout(self):
        with mock.patch.object(self.mod.subprocess, "Popen", return_value=self._never_exits_proc()), \
             mock.patch.object(self.mod, "get_agent", return_value={"agent_status": "blocked"}):
            status, info = self.mod.dispatch_and_wait("x", "prompt", timeout_s=180)
        self.assertEqual(status, "blocked")
        self.assertGreaterEqual(self.clock.t, self.mod.BLOCKED_GRACE_S)
        self.assertLess(self.clock.t, 180)

    def test_real_timeout_when_never_settles(self):
        with mock.patch.object(self.mod.subprocess, "Popen", return_value=self._never_exits_proc()), \
             mock.patch.object(self.mod, "get_agent", return_value={"agent_status": "working"}):
            status, info = self.mod.dispatch_and_wait("x", "prompt", timeout_s=60)
        self.assertEqual(status, "timeout")
        self.assertAlmostEqual(self.clock.t, 60, delta=3)

    def test_transient_blip_settles_normally(self):
        proc = mock.Mock()
        proc.poll.side_effect = [None, None, None, 0, 0, 0]
        proc.communicate.return_value = (json.dumps({"result": {"agent": {"agent_status": "idle"}}}), "")
        statuses = iter(["blocked", "working", "working", "working", "working"])
        with mock.patch.object(self.mod.subprocess, "Popen", return_value=proc), \
             mock.patch.object(self.mod, "get_agent", side_effect=lambda name: {"agent_status": next(statuses, "working")}):
            status, info = self.mod.dispatch_and_wait("x", "prompt", timeout_s=180)
        self.assertEqual(status, "idle", "blip transitorio nao deveria ser reportado como blocked")

    def test_agent_blocked_on_submission_is_reported_as_blocked(self):
        proc = mock.Mock()
        proc.poll.return_value = 1
        proc.communicate.return_value = (json.dumps({"error": {"code": "agent_blocked", "message": "..."}}), "")
        with mock.patch.object(self.mod.subprocess, "Popen", return_value=proc):
            status, info = self.mod.dispatch_and_wait("x", "prompt", timeout_s=60)
        self.assertEqual(status, "blocked")

    def test_prompt_stalled_is_reported_distinctly_from_timeout(self):
        proc = mock.Mock()
        proc.poll.return_value = 1
        proc.communicate.return_value = (json.dumps({"error": {"code": "agent_prompt_stalled", "message": "no change"}}), "")
        with mock.patch.object(self.mod.subprocess, "Popen", return_value=proc):
            status, info = self.mod.dispatch_and_wait("x", "prompt", timeout_s=60)
        self.assertEqual(status, "stalled")


if __name__ == "__main__":
    unittest.main()
