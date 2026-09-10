#!/usr/bin/env python3
"""Regression tests for fail-closed project binding in the dispatchers.

The live claude-bridge workspace once had agents registered under the runtime
directory instead of the Git repository. A dispatcher must refuse before it
creates a round directory or sends a prompt when those roots differ.
"""
import importlib.util
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


class DispatchProjectBindingTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load("herdr-review-dispatch")
        self.core = self.mod.core

    def test_different_project_roots_are_rejected_before_dispatch(self):
        with mock.patch.object(
            self.core,
            "project_root",
            side_effect=["/runtime/.claude-bridge", "/repo/claude-bridge"],
        ):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                self.core.validate_dispatch_cwd("/runtime/.claude-bridge")

    def test_same_project_root_is_accepted(self):
        with mock.patch.object(
            self.core,
            "project_root",
            side_effect=["/repo/claude-bridge", "/repo/claude-bridge"],
        ):
            self.assertEqual(
                self.core.validate_dispatch_cwd("/repo/claude-bridge"),
                "/repo/claude-bridge",
            )

    def test_reviewer_foreground_root_is_checked_too(self):
        with mock.patch.object(
            self.core,
            "get_agent_info",
            return_value={
                "cwd": "/repo/claude-bridge",
                "foreground_cwd": "/runtime/.claude-bridge",
            },
        ), mock.patch.object(
            self.core,
            "project_root",
            side_effect=[
                "/repo/claude-bridge",
                "/runtime/.claude-bridge",
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "foreground_cwd"):
                self.core.validate_agent_project("claude-bridge-rev", "/repo/claude-bridge")

    def test_reviewer_project_is_accepted_when_both_roots_match(self):
        with mock.patch.object(
            self.core,
            "get_agent_info",
            return_value={
                "cwd": "/repo/claude-bridge",
                "foreground_cwd": "/repo/claude-bridge/subdir",
            },
        ), mock.patch.object(
            self.core,
            "project_root",
            side_effect=[
                "/repo/claude-bridge",
                "/repo/claude-bridge",
            ],
        ):
            info = self.core.validate_agent_project("claude-bridge-rev", "/repo/claude-bridge")
            self.assertEqual(info["cwd"], "/repo/claude-bridge")


if __name__ == "__main__":
    unittest.main()


class SessionIsolationTests(unittest.TestCase):
    """Achado 2026-09-10: `HERDR_SESSION` NAO e' variavel do Herdr -- `herdr
    --help` documenta so HERDR_CONFIG_PATH. Exportar a var nao isolava nada:
    medido, `HERDR_SESSION=testlab herdr agent list` devolveu os 41 agents de
    PRODUCAO enquanto `herdr --session testlab agent list` devolveu 0.

    Era perigoso porque o README ensinava a var como forma de testar isolado e
    o herdr-add-space imprimia "MODO TESTE session=..." ao ve-la -- confianca
    de isolamento com os comandos indo pra mesh real. Estes testes travam a
    traducao var -> flag."""

    def setUp(self):
        self.core = _load("_herdr_dispatch.py")

    def test_no_env_means_no_session_flag(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERDR_SESSION", None)
            argv = self.core.herdr_argv("agent", "list")
        self.assertNotIn("--session", argv)
        self.assertEqual(argv[1:], ["agent", "list"])

    def test_env_becomes_session_flag_before_subcommand(self):
        """A flag tem que vir ANTES do subcomando: `herdr --session x agent
        list`. Depois do subcomando o Herdr a trata como argumento e ignora o
        isolamento."""
        with mock.patch.dict(os.environ, {"HERDR_SESSION": "testlab"}):
            argv = self.core.herdr_argv("agent", "list")
        self.assertEqual(argv[1:], ["--session", "testlab", "agent", "list"])
