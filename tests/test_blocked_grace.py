#!/usr/bin/env python3
"""Regression tests for the blocked-vs-transient settle logic in
_herdr_dispatch.dispatch_and_wait_all() — shared by herdr-review-dispatch (N
concurrent agents) and herdr-swap-exec (1 agent, via a thin wrapper) — both
via native `agent prompt --wait`, one `herdr` subprocess per agent.

These formalize the ad-hoc simulations used to validate BLOCKED_GRACE_S so
the same scenarios are checked on every change instead of re-derived by hand
each time — this repo's own history has two fixes in sequence on the same
settle loop (round-1 fix locked in blocked immediately; round-2 fix over-
corrected into consuming the full timeout), so the loop is exactly the kind
of code that regresses quietly without a repeatable check.

Patches target self.mod.core.* (the _herdr_dispatch module, shared and
imported by both scripts as `core`), not attributes on the outer script
module — dispatch_and_wait_all()'s internals resolve names (get_agent_info,
subprocess, time) through _herdr_dispatch's own globals, so patching an
outer script's local alias of the same name has no effect on it.

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


class _BaseDispatchTests(unittest.TestCase):
    """Common setup: load a script, reach its `core` (_herdr_dispatch)
    module, patch core's clock. Subclasses set SCRIPT_NAME."""

    SCRIPT_NAME = None

    def setUp(self):
        self.mod = _load(self.SCRIPT_NAME)
        self.core = self.mod.core
        self.clock = FakeClock()
        for patcher in (
            mock.patch.object(self.core.time, "time", self.clock.time),
            mock.patch.object(self.core.time, "sleep", self.clock.sleep),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _never_exits_proc():
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.communicate.return_value = ("", "")
        return proc

    @staticmethod
    def _settles_proc(status="idle"):
        proc = mock.Mock()
        proc.poll.return_value = 0
        proc.communicate.return_value = (json.dumps({"result": {"agent": {"agent_status": status}}}), "")
        return proc

    def _popen_by_name(self, procs_by_name):
        def _side_effect(args, **kwargs):
            name = args[3]  # [HERDR, "agent", "prompt", name, text, ...]
            return procs_by_name[name]
        return _side_effect


class DispatchAndWaitAllTests(_BaseDispatchTests):
    """herdr-review-dispatch: dispatch_and_wait_all(), N agentes concorrentes."""

    SCRIPT_NAME = "herdr-review-dispatch"

    def test_sustained_blocked_reports_early_not_full_timeout(self):
        with mock.patch.object(self.core.subprocess, "Popen", side_effect=self._popen_by_name({"rev": self._never_exits_proc()})), \
             mock.patch.object(self.core, "get_agent_info", return_value={"agent_status": "blocked"}):
            result, info, settle_ts = self.core.dispatch_and_wait_all({"rev": "prompt"}, timeout_s=1200)
        self.assertEqual(result["rev"], "blocked")
        self.assertIn("rev", settle_ts)
        self.assertGreaterEqual(self.clock.t, self.core.BLOCKED_GRACE_S)
        self.assertLess(self.clock.t, 1200, "bloqueio sustentado nao deveria esperar o --timeout inteiro")

    def test_transient_blip_settles_normally_not_blocked(self):
        proc = mock.Mock()
        proc.poll.side_effect = [None, None, None, 0]
        proc.communicate.return_value = (json.dumps({"result": {"agent": {"agent_status": "idle"}}}), "")
        statuses = iter(["blocked", "working", "working", "working"])
        with mock.patch.object(self.core.subprocess, "Popen", side_effect=self._popen_by_name({"rev": proc})), \
             mock.patch.object(self.core, "get_agent_info", side_effect=lambda name: {"agent_status": next(statuses, "working")}):
            result, info, settle_ts = self.core.dispatch_and_wait_all({"rev": "prompt"}, timeout_s=1200)
        self.assertEqual(result["rev"], "idle", "blip transitorio nao deveria ser reportado como blocked")

    def test_real_timeout_when_nothing_settles(self):
        with mock.patch.object(self.core.subprocess, "Popen", side_effect=self._popen_by_name({"rev": self._never_exits_proc()})), \
             mock.patch.object(self.core, "get_agent_info", return_value={"agent_status": "working"}):
            result, info, settle_ts = self.core.dispatch_and_wait_all({"rev": "prompt"}, timeout_s=60)
        self.assertEqual(result["rev"], "timeout")
        self.assertAlmostEqual(self.clock.t, 60, delta=3)

    def test_two_agents_one_stuck_one_settles_independently(self):
        procs = {"rev": self._never_exits_proc(), "rev2": self._settles_proc("idle")}
        with mock.patch.object(self.core.subprocess, "Popen", side_effect=self._popen_by_name(procs)), \
             mock.patch.object(self.core, "get_agent_info", side_effect=lambda name: {"agent_status": "blocked" if name == "rev" else "idle"}):
            result, info, settle_ts = self.core.dispatch_and_wait_all({"rev": "p1", "rev2": "p2"}, timeout_s=1200)
        self.assertEqual(result["rev"], "blocked")
        self.assertEqual(result["rev2"], "idle")
        self.assertLess(self.clock.t, 1200, "um agent travado nao deveria atrasar a detecao do outro")

    def test_agent_blocked_on_submission_is_reported_as_blocked(self):
        proc = mock.Mock()
        proc.poll.return_value = 1
        proc.communicate.return_value = (json.dumps({"error": {"code": "agent_blocked", "message": "..."}}), "")
        with mock.patch.object(self.core.subprocess, "Popen", side_effect=self._popen_by_name({"rev": proc})):
            result, info, settle_ts = self.core.dispatch_and_wait_all({"rev": "prompt"}, timeout_s=60)
        self.assertEqual(result["rev"], "blocked")

    def test_prompt_stalled_is_reported_distinctly_from_timeout(self):
        proc = mock.Mock()
        proc.poll.return_value = 1
        proc.communicate.return_value = (json.dumps({"error": {"code": "agent_prompt_stalled", "message": "no change"}}), "")
        with mock.patch.object(self.core.subprocess, "Popen", side_effect=self._popen_by_name({"rev": proc})):
            result, info, settle_ts = self.core.dispatch_and_wait_all({"rev": "prompt"}, timeout_s=60)
        self.assertEqual(result["rev"], "stalled")


class DispatchAndWaitTests(_BaseDispatchTests):
    """herdr-swap-exec: dispatch_and_wait(), wrapper de 1 agente sobre
    dispatch_and_wait_all() — mesmo motor, mesmos cenarios, via a API do
    script (name, text, timeout) -> (status, info)."""

    SCRIPT_NAME = "herdr-swap-exec"

    def test_sustained_blocked_reports_early_not_full_timeout(self):
        with mock.patch.object(self.core.subprocess, "Popen", side_effect=self._popen_by_name({"x": self._never_exits_proc()})), \
             mock.patch.object(self.core, "get_agent_info", return_value={"agent_status": "blocked"}):
            status, info = self.mod.dispatch_and_wait("x", "prompt", timeout_s=180)
        self.assertEqual(status, "blocked")
        self.assertGreaterEqual(self.clock.t, self.core.BLOCKED_GRACE_S)
        self.assertLess(self.clock.t, 180)

    def test_real_timeout_when_never_settles(self):
        with mock.patch.object(self.core.subprocess, "Popen", side_effect=self._popen_by_name({"x": self._never_exits_proc()})), \
             mock.patch.object(self.core, "get_agent_info", return_value={"agent_status": "working"}):
            status, info = self.mod.dispatch_and_wait("x", "prompt", timeout_s=60)
        self.assertEqual(status, "timeout")
        self.assertAlmostEqual(self.clock.t, 60, delta=3)

    def test_transient_blip_settles_normally(self):
        proc = mock.Mock()
        proc.poll.side_effect = [None, None, None, 0, 0, 0]
        proc.communicate.return_value = (json.dumps({"result": {"agent": {"agent_status": "idle"}}}), "")
        statuses = iter(["blocked", "working", "working", "working", "working"])
        with mock.patch.object(self.core.subprocess, "Popen", side_effect=self._popen_by_name({"x": proc})), \
             mock.patch.object(self.core, "get_agent_info", side_effect=lambda name: {"agent_status": next(statuses, "working")}):
            status, info = self.mod.dispatch_and_wait("x", "prompt", timeout_s=180)
        self.assertEqual(status, "idle", "blip transitorio nao deveria ser reportado como blocked")

    def test_agent_blocked_on_submission_is_reported_as_blocked(self):
        proc = mock.Mock()
        proc.poll.return_value = 1
        proc.communicate.return_value = (json.dumps({"error": {"code": "agent_blocked", "message": "..."}}), "")
        with mock.patch.object(self.core.subprocess, "Popen", side_effect=self._popen_by_name({"x": proc})):
            status, info = self.mod.dispatch_and_wait("x", "prompt", timeout_s=60)
        self.assertEqual(status, "blocked")

    def test_prompt_stalled_is_reported_distinctly_from_timeout(self):
        proc = mock.Mock()
        proc.poll.return_value = 1
        proc.communicate.return_value = (json.dumps({"error": {"code": "agent_prompt_stalled", "message": "no change"}}), "")
        with mock.patch.object(self.core.subprocess, "Popen", side_effect=self._popen_by_name({"x": proc})):
            status, info = self.mod.dispatch_and_wait("x", "prompt", timeout_s=60)
        self.assertEqual(status, "stalled")


if __name__ == "__main__":
    unittest.main()
