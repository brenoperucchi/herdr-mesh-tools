"""Regressions for automatic recovery of missing mandatory Herdr agents.

The live HomeHub workspace lost its scout registration. Before this path,
herdr-ask/herdr-review-dispatch treated ``agent_not_found`` as a terminal
error before the bootstrap table could repair the space. These tests keep the
recovery idempotent, targeted, and fail-controlled.
"""
import importlib.util
import os
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load(filename, module_name):
    path = os.path.join(BIN_DIR, filename)
    loader = SourceFileLoader(module_name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class AgentRestoreTests(unittest.TestCase):
    def setUp(self):
        self.core = _load("_herdr_dispatch.py", "herdr_dispatch_agent_restore")
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    @staticmethod
    def _info(name, status="idle"):
        return {
            "name": name,
            "agent": "codex",
            "agent_status": status,
            "cwd": "/repo",
            "foreground_cwd": "/repo",
            "pane_id": f"pane-{name}",
            "workspace_id": "w1",
            "tab_id": "w1:t1",
            "agent_session": {"value": f"session-{name}"},
            "revision": 1,
            "state_change_seq": 1,
        }

    def test_missing_roles_are_bootstrapped_once_and_reobserved(self):
        missing = {"homehub-scout", "homehub-rev-2"}
        booted = {"value": False}
        calls = []

        def get(name):
            calls.append(name)
            if name in missing and not booted["value"]:
                raise RuntimeError(f"agent target {name}: agent_not_found")
            return self._info(name)

        def run(command, **kwargs):
            booted["value"] = True
            self.assertEqual(command[-2:], ["--slug", "homehub"])
            self.assertEqual(kwargs["cwd"], self.tmpdir.name)
            return subprocess.CompletedProcess(command, 0, stdout="HomeHub restaurado\n", stderr="")

        with mock.patch.object(self.core, "get_agent_info", side_effect=get), \
             mock.patch.object(self.core.subprocess, "run", side_effect=run) as bootstrap, \
             mock.patch.object(self.core.time, "sleep"):
            result = self.core.ensure_required_agents(
                "homehub", self.tmpdir.name,
                ["homehub-rev-1", "homehub-rev-2", "homehub-scout"],
            )

        self.assertTrue(result["attempted"])
        self.assertEqual(result["missing_before"], ["homehub-rev-2", "homehub-scout"])
        self.assertEqual(result["restored"], ["homehub-rev-2", "homehub-scout"])
        bootstrap.assert_called_once()

    def test_existing_roles_are_left_alone(self):
        with mock.patch.object(self.core, "get_agent_info", side_effect=self._info), \
             mock.patch.object(self.core.subprocess, "run") as bootstrap:
            result = self.core.ensure_required_agents(
                "homehub", self.tmpdir.name, ["homehub-rev-1", "homehub-rev-2"]
            )

        self.assertEqual(result, {"attempted": False, "missing_before": [], "restored": []})
        bootstrap.assert_not_called()

    def test_bootstrap_failure_is_a_controlled_restore_error(self):
        def missing(_name):
            raise RuntimeError("agent target homehub-scout: agent_not_found")

        failed = subprocess.CompletedProcess(
            ["herdr-bootstrap"], 1, stdout="", stderr="nenhum space na tabela\n"
        )
        with mock.patch.object(self.core, "get_agent_info", side_effect=missing), \
             mock.patch.object(self.core.subprocess, "run", return_value=failed):
            with self.assertRaises(self.core.AgentRestoreError) as ctx:
                self.core.ensure_required_agents("homehub", self.tmpdir.name, ["homehub-scout"])

        self.assertIn("bootstrap não conseguiu recuperar", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, subprocess.CalledProcessError)

    def test_homehub_declares_scout_with_the_initialization_profile(self):
        boot = _load("herdr-bootstrap", "herdr_bootstrap_agent_restore")
        entry = next(item for item in boot.SPACES if item[0] == "HomeHub")
        self.assertEqual(entry[3], ("homehub-scout", "codex", boot.SCOUT_ARGS))

    def test_homehub_rev2_default_also_pins_reasoning(self):
        boot = _load("herdr-bootstrap", "herdr_bootstrap_agent_restore_rev2")
        rev2 = next(item for item in boot.build_rev_agents("homehub", self.tmpdir.name)
                    if item[0] == "homehub-rev-2")
        self.assertEqual(rev2[2], ["--model", "opus-5", "--effort", "low"])

    def test_unnamed_scout_pane_is_a_base_not_a_new_default_agent(self):
        boot = _load("herdr-bootstrap", "herdr_bootstrap_agent_restore_base")
        agents = [{
            "workspace_id": "wR", "tab_id": "wR:t6", "pane_id": "wR:p8",
            "agent": "codex", "agent_status": "working", "name": None,
        }]
        base = boot.find_unnamed_role_base(agents, "wR", "wR:t1")
        self.assertEqual(base["pane_id"], "wR:p8")

    def test_unnamed_scout_base_preserves_swapped_family(self):
        boot = _load("herdr-bootstrap", "herdr_bootstrap_agent_restore_swapped")
        agents = [{
            "workspace_id": "wR", "tab_id": "wR:t6", "pane_id": "wR:p8",
            "agent": "claude", "agent_status": "working", "name": None,
        }]
        base = boot.find_unnamed_role_base(agents, "wR", "wR:t1")
        self.assertEqual(base["agent"], "claude")

    def test_unnamed_reviewer_restore_is_supported_without_restarting(self):
        boot = _load("herdr-bootstrap", "herdr_bootstrap_agent_restore_reviewer")
        agents = [{
            "workspace_id": "wR", "tab_id": "wR:t1", "pane_id": "wR:p3",
            "agent": "claude", "agent_status": "working", "name": None,
        }]
        base = boot.find_unnamed_reviewer_base(
            agents, "wR", "wR:t1", ["foo-rev-2"], set()
        )
        self.assertEqual(base[0]["pane_id"], "wR:p3")
        self.assertEqual(base[1], "foo-rev-2")

    def test_unnamed_reviewer_restore_refuses_ambiguous_slots(self):
        boot = _load("herdr-bootstrap", "herdr_bootstrap_agent_restore_ambiguous")
        agents = [
            {"workspace_id": "wR", "tab_id": "wR:t1", "pane_id": "wR:p2", "name": None},
            {"workspace_id": "wR", "tab_id": "wR:t1", "pane_id": "wR:p3", "name": None},
        ]
        self.assertIsNone(boot.find_unnamed_reviewer_base(
            agents, "wR", "wR:t1", ["foo-rev-1", "foo-rev-2"], set()
        ))


if __name__ == "__main__":
    unittest.main()
