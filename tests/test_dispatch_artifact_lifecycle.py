#!/usr/bin/env python3
"""Regressões do vínculo entre lifecycle do Herdr e artefatos da rodada.

O Herdr pode devolver ``done`` antes de um agent terminar de publicar o
``answer.md``/``verdict.md`` que o dispatcher pediu. Esses testes modelam essa
ordem sem falar com um servidor, CLI, modelo ou GPU reais.
"""
import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load(name):
    path = os.path.join(BIN_DIR, name)
    loader = SourceFileLoader(name.replace("-", "_"), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class DispatchArtifactLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.core = _load("_herdr_dispatch.py")
        self.clock = FakeClock()
        self.time_patch = mock.patch.object(self.core.time, "time", self.clock.time)
        self.sleep_patch = mock.patch.object(self.core.time, "sleep", self.clock.sleep)
        self.time_patch.start()
        self.sleep_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.addCleanup(self.sleep_patch.stop)

    @staticmethod
    def _done_proc():
        proc = mock.Mock()
        proc.poll.return_value = 0
        proc.communicate.return_value = (
            json.dumps({"result": {"agent": {"agent_status": "done"}}}),
            "",
        )
        return proc

    @staticmethod
    def _popen_for(proc):
        def side_effect(args, **kwargs):
            assert args[3] == "scout"
            return proc
        return side_effect

    def test_done_waits_until_answer_is_published(self):
        """A resposta criada depois do ``done`` deve ser a que fecha a rodada."""
        with tempfile.TemporaryDirectory() as tmp:
            answer = Path(tmp) / "answer.md"
            proc = self._done_proc()

            def sleep_and_publish(seconds):
                if not answer.exists():
                    answer.write_text("resposta completa\n", encoding="utf-8")
                self.clock.sleep(seconds)

            with mock.patch.object(self.core.subprocess, "Popen", side_effect=self._popen_for(proc)), \
                 mock.patch.object(self.core.time, "sleep", side_effect=sleep_and_publish):
                result, info, settle_ts = self.core.dispatch_and_wait_all(
                    {"scout": "prompt"}, 10,
                    required_artifacts={"scout": str(answer)},
                )

        self.assertEqual(result["scout"], "done")
        self.assertIn("required_artifact", info["scout"])
        self.assertEqual(info["scout"]["required_artifact"]["bytes"], 18)
        self.assertIn("lifecycle_settle_ts", info["scout"])
        self.assertIn("scout", settle_ts)
        self.assertGreater(self.clock.t, 0)

    def test_done_without_artifact_is_explicit_failure_at_deadline(self):
        """Nunca registrar ``done``/sucesso quando o artefato não apareceu."""
        with tempfile.TemporaryDirectory() as tmp:
            answer = Path(tmp) / "answer.md"
            proc = self._done_proc()
            with mock.patch.object(self.core.subprocess, "Popen", side_effect=self._popen_for(proc)):
                result, info, _ = self.core.dispatch_and_wait_all(
                    {"scout": "prompt"}, 4,
                    required_artifacts={"scout": str(answer)},
                )

        self.assertEqual(result["scout"], "artifact_missing")
        self.assertEqual(info["scout"]["lifecycle_status"], "done")
        self.assertIn("lifecycle_settle_ts", info["scout"])
        self.assertEqual(info["scout"]["artifact_path"], str(answer))

    def test_existing_nonempty_artifact_is_not_reused(self):
        """Um arquivo velho não pode validar um novo lifecycle ``done``."""
        with tempfile.TemporaryDirectory() as tmp:
            answer = Path(tmp) / "answer.md"
            answer.write_text("rodada antiga\n", encoding="utf-8")
            proc = self._done_proc()
            with mock.patch.object(self.core.subprocess, "Popen", side_effect=self._popen_for(proc)):
                result, _, _ = self.core.dispatch_and_wait_all(
                    {"scout": "prompt"}, 4,
                    required_artifacts={"scout": str(answer)},
                )

        self.assertEqual(result["scout"], "artifact_missing")

    def test_metrics_writer_publishes_complete_json_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            metrics = Path(tmp) / "metrics.json"
            metrics.write_text('{"old": true}\n', encoding="utf-8")
            with mock.patch.object(self.core.os, "replace", wraps=os.replace) as replace:
                self.core.write_json_atomic(str(metrics), {"new": True, "bytes": 42})

            with metrics.open(encoding="utf-8") as stream:
                self.assertEqual(json.load(stream), {"new": True, "bytes": 42})
            self.assertEqual(replace.call_count, 1)
            self.assertEqual(
                [p.name for p in Path(tmp).iterdir() if p.name.startswith(".metrics.json.")],
                [],
            )


if __name__ == "__main__":
    unittest.main()
