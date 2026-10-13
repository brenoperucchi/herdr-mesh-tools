"""Regressões da simulação offline de swap periódico condicionado a idle."""
import importlib.util
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from importlib.machinery import SourceFileLoader


BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load():
    path = os.path.join(BIN_DIR, "herdr-simulate-idle-swap")
    loader = SourceFileLoader("herdr_simulate_idle_swap", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


sim = _load()


class IdleSwapSimulationTests(unittest.TestCase):
    def setUp(self):
        self.base = sim.Pane(
            name="herdr-simulation-rev-1", pane_id="wS:p2", workspace_id="wS",
            tab_id="wS:t1", cwd="/home/brenoperucchi/Devs/herdr-simulation",
            session="session-1", kind="codex", model="gpt-6-astra",
            reasoning_effort="low", status="idle", state_change_seq=40,
        )

    def test_working_tick_defers_without_mutation(self):
        current = sim.Pane(**{**self.base.__dict__, "status": "working"})
        result = sim.periodic_swap_decision(self.base, current)
        self.assertEqual(result["decision"], "defer")
        self.assertEqual(result["mutations"], [])

    def test_idle_stable_tick_is_ready_and_preserves_profile(self):
        result = sim.periodic_swap_decision(self.base, self.base)
        self.assertEqual(result["decision"], "ready")
        self.assertEqual(result["model"], "gpt-6-astra")
        self.assertEqual(result["reasoning_effort"], "low")
        self.assertEqual(result["mutations"], [])

    def test_sequence_change_after_idle_checkpoint_aborts(self):
        current = sim.Pane(**{**self.base.__dict__, "state_change_seq": 41})
        result = sim.periodic_swap_decision(self.base, current)
        self.assertEqual(result["decision"], "abort")
        self.assertEqual(result["reason"], "state_change_seq_changed")

    def test_session_change_after_idle_checkpoint_aborts(self):
        current = sim.Pane(**{**self.base.__dict__, "session": "session-2"})
        result = sim.periodic_swap_decision(self.base, current)
        self.assertEqual(result["decision"], "abort")
        self.assertEqual(result["reason"], "identity_changed")

    def test_cli_is_offline_and_names_herdr_simulation(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(sim.main(["--space", "herdr-simulation", "--json"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["space"], "herdr-simulation")
        self.assertTrue(payload["offline"])
        self.assertEqual(
            [case["result"]["decision"] for case in payload["cases"]],
            ["defer", "ready", "abort", "abort"],
        )


if __name__ == "__main__":
    unittest.main()
