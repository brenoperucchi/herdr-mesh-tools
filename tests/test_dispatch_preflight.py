#!/usr/bin/env python3
"""Regressões para a janela entre `agent get` e `agent prompt`.

Um revisor pode aparecer `idle` na leitura inicial e começar a processar uma
entrega anterior antes da submissão seguinte. O dispatcher precisa revalidar a
identidade sob lock e, em um retry ambíguo, esperar um agent que já ficou
`working` em vez de enviar o mesmo prompt de novo.
"""
import importlib.util
import json
import os
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock


BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load():
    path = os.path.join(BIN_DIR, "_herdr_dispatch.py")
    loader = SourceFileLoader("herdr_dispatch_preflight", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _agent_info(*, status="idle", seq=10, pane="w:p1", session="s1", revision=3):
    return {
        "agent": "codex",
        "agent_status": status,
        "pane_id": pane,
        "workspace_id": "w1",
        "tab_id": "t1",
        "cwd": "/repo/project",
        "foreground_cwd": "/repo/project",
        "agent_session": {"value": session},
        "revision": revision,
        "state_change_seq": seq,
        "interactive_ready": True,
    }


class DispatchPreflightTests(unittest.TestCase):
    def setUp(self):
        self.core = _load()

    def _settled_proc(self, status="idle"):
        proc = mock.Mock()
        proc.poll.return_value = 0
        proc.communicate.return_value = (
            json.dumps({"result": {"agent": {"agent_status": status}}}),
            "",
        )
        return proc

    def _stalled_proc(self):
        proc = mock.Mock()
        proc.poll.return_value = 1
        proc.communicate.return_value = (
            json.dumps({"error": {"code": "agent_prompt_stalled", "message": "no change"}}),
            "",
        )
        return proc

    def test_idle_snapshot_turning_working_aborts_before_first_prompt(self):
        expected = _agent_info(status="idle", seq=10)
        observed = _agent_info(status="working", seq=11)
        with mock.patch.object(self.core, "get_agent_info", return_value=observed), \
             mock.patch.object(self.core, "pane_looks_busy_with_human_input", return_value=(False, None)), \
             mock.patch.object(self.core.subprocess, "Popen") as popen:
            with self.assertRaises(self.core.DispatchPreflightError) as raised:
                self.core.dispatch_and_wait_all(
                    {"rev": "/tmp/.herdr/review/foo-1/rev/request.md"},
                    timeout_s=30,
                    expected_agents={"rev": expected},
                    check_composition=False,
                )

        self.assertIn("agent_status", raised.exception.reason)
        self.assertEqual(raised.exception.observed["agent_status"], "working")
        popen.assert_not_called()

    def test_working_snapshot_is_not_eligible_for_review_dispatch(self):
        """Uma leitura inicial que já chegou working não pode ser usada como
        autorização para mandar outra revisão em cima do turno concorrente."""
        expected = _agent_info(status="working", seq=11)
        with mock.patch.object(self.core, "get_agent_info", return_value=expected), \
             mock.patch.object(self.core, "pane_looks_busy_with_human_input", return_value=(False, None)), \
             mock.patch.object(self.core.subprocess, "Popen") as popen:
            with self.assertRaises(self.core.DispatchPreflightError) as raised:
                self.core.dispatch_and_wait_all(
                    {"rev": "/tmp/.herdr/review/foo-1/rev/request.md"},
                    timeout_s=30,
                    expected_agents={"rev": expected},
                    check_composition=False,
                )

        self.assertIn("esperado idle/done", raised.exception.reason)
        popen.assert_not_called()

    def test_identity_change_aborts_without_sending(self):
        expected = _agent_info(status="idle", seq=10, pane="w:p1", session="s1")
        observed = _agent_info(status="idle", seq=10, pane="w:p2", session="s2")
        with mock.patch.object(self.core, "get_agent_info", return_value=observed), \
             mock.patch.object(self.core, "pane_looks_busy_with_human_input", return_value=(False, None)), \
             mock.patch.object(self.core.subprocess, "Popen") as popen:
            with self.assertRaises(self.core.DispatchPreflightError) as raised:
                self.core.dispatch_and_wait_all(
                    {"rev": "/tmp/.herdr/review/foo-1/rev/request.md"},
                    timeout_s=30,
                    expected_agents={"rev": expected},
                    check_composition=False,
                )

        self.assertIn("pane_id", raised.exception.reason)
        self.assertIn("agent_session", raised.exception.reason)
        popen.assert_not_called()

    def test_working_before_retry_suppresses_duplicate_prompt(self):
        baseline = _agent_info(status="idle", seq=10)
        working = _agent_info(status="working", seq=11)
        first = self._stalled_proc()
        wait = self._settled_proc("idle")
        prompts = {"rev": "/tmp/.herdr/review/foo-1/rev/request.md"}

        processes = iter((first, wait))

        def popen_from_iterator(_args, **_kwargs):
            return next(processes)

        with mock.patch.object(self.core, "get_agent_info", side_effect=[baseline, working]), \
             mock.patch.object(self.core, "pane_looks_busy_with_human_input", return_value=(False, None)), \
             mock.patch.object(
                 self.core.subprocess,
                 "run",
                 return_value=mock.Mock(returncode=0, stdout="", stderr=""),
             ), mock.patch.object(self.core.subprocess, "Popen", side_effect=popen_from_iterator) as popen_mock, \
             mock.patch.object(self.core.time, "sleep"):
            result, info, _ = self.core.dispatch_and_wait_all(
                prompts,
                timeout_s=30,
                expected_agents={"rev": baseline},
                check_composition=False,
            )

        self.assertEqual(result["rev"], "idle")
        self.assertEqual(popen_mock.call_count, 2)
        commands = [call.args[0][1:4] for call in popen_mock.call_args_list]
        self.assertEqual(commands[0][:3], ["agent", "prompt", "rev"])
        self.assertEqual(commands[1][:3], ["agent", "wait", "rev"])
        self.assertEqual(sum(command[1] == "prompt" for command in commands), 1)
        self.assertEqual(info["rev"]["retry_preflight"]["agent_status"], "working")
        self.assertIn("resend_suppressed", info["rev"])

    def test_unchanged_idle_snapshot_allows_one_retry(self):
        baseline = _agent_info(status="idle", seq=10)
        first = self._stalled_proc()
        second = self._settled_proc("idle")
        processes = iter((first, second))

        with mock.patch.object(self.core, "get_agent_info", side_effect=[baseline, baseline]), \
             mock.patch.object(self.core, "pane_looks_busy_with_human_input", return_value=(False, None)), \
             mock.patch.object(
                 self.core.subprocess,
                 "run",
                 return_value=mock.Mock(returncode=0, stdout="", stderr=""),
             ), mock.patch.object(self.core.subprocess, "Popen", side_effect=lambda *_a, **_k: next(processes)) as popen_mock, \
             mock.patch.object(self.core.time, "sleep"):
            result, info, _ = self.core.dispatch_and_wait_all(
                {"rev": "/tmp/.herdr/review/foo-1/rev/request.md"},
                timeout_s=30,
                expected_agents={"rev": baseline},
                check_composition=False,
            )

        self.assertEqual(result["rev"], "idle")
        commands = [call.args[0][1:4] for call in popen_mock.call_args_list]
        self.assertEqual(commands[0][:3], ["agent", "prompt", "rev"])
        self.assertEqual(commands[1][:3], ["agent", "prompt", "rev"])
        self.assertNotIn("resend_suppressed", info["rev"])


if __name__ == "__main__":
    unittest.main()
