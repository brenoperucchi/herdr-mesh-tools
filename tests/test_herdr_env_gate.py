#!/usr/bin/env python3
"""Regression tests for core.require_herdr_pane() and its wiring into the
four dispatcher entrypoints (herdr-review-dispatch, herdr-ask, herdr-swap,
herdr-migrate-rev).

Achado 2026-09-01 (relatado pelo dre-exec, confirmado ao vivo contra um
Codex real - herdr-rev): `$HERDR_ENV` le VAZIO de dentro do sandbox de
execucao de shell do Codex (`codex-code-mode-host`), mesmo o processo do
agent tendo a variavel de verdade no proprio `environ` (`env | grep -c
'^HERDR'` devolveu 0 rodado por dentro do Codex, contra 59/59 variaveis
identicas comparando /proc/<pid>/environ do agent e do filho direto
codex-code-mode-host). Os quatro scripts abaixo tratavam `$HERDR_ENV` vazio
como prova de estar fora do Herdr e recusavam rodar com `sys.exit(1)` - uma
mensagem de erro literalmente falsa quando chamados por um `*-exec` Codex
(ex: claude-bridge-exec, content-insights-collector-exec) de dentro do
proprio sandbox.
"""
import importlib.util
import os
import sys
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load(name, module_name=None):
    path = os.path.join(BIN_DIR, name)
    loader = SourceFileLoader(module_name or name.replace("-", "_"), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class RequireHerdrPaneTests(unittest.TestCase):
    def setUp(self):
        self.core = _load("_herdr_dispatch.py", "herdr_env_gate_core")

    def test_passes_immediately_when_herdr_env_is_1_without_touching_the_cli(self):
        def fail_if_called(*args):
            raise AssertionError(f"api() nao deveria ser chamado com HERDR_ENV=1: {args!r}")

        with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), \
             mock.patch.object(self.core, "api", side_effect=fail_if_called):
            self.core.require_herdr_pane()  # nao deve levantar nada

    def test_falls_back_to_agent_list_and_passes_when_it_succeeds(self):
        # Simula o sandbox do Codex: HERDR_ENV vazio, mas o servidor real
        # responde - deve seguir, nao recusar.
        env = dict(os.environ)
        env.pop("HERDR_ENV", None)
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(self.core, "api", return_value={"agents": []}) as mocked:
            self.core.require_herdr_pane()
        mocked.assert_called_once_with("agent", "list")

    def test_exits_when_herdr_env_is_empty_and_agent_list_also_fails(self):
        env = dict(os.environ)
        env.pop("HERDR_ENV", None)
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(self.core, "api", side_effect=RuntimeError("agent list: no_active_session")):
            with self.assertRaises(SystemExit) as ctx:
                self.core.require_herdr_pane()
        self.assertEqual(ctx.exception.code, 1)


class DispatcherWiringTests(unittest.TestCase):
    """Confirma que os quatro scripts chamam core.require_herdr_pane() em
    main(), em vez do check direto de os.environ que causava o achado -
    acoplamento fraco de verdade (herdr-9/herdr-11 ja mostraram que
    reimplementar a mesma checagem em quatro lugares diverge sozinho)."""

    SCRIPTS = ("herdr-review-dispatch", "herdr-ask", "herdr-swap", "herdr-migrate-rev")

    ARGV = {
        "herdr-review-dispatch": ["herdr-review-dispatch", "foo"],
        "herdr-ask": ["herdr-ask", "foo", "--question-file", "/tmp/nao-precisa-existir.md"],
        "herdr-swap": ["herdr-swap", "foo", "rev", "codex"],
        "herdr-migrate-rev": ["herdr-migrate-rev", "foo"],
    }

    def test_each_dispatcher_calls_the_shared_gate_before_doing_anything_else(self):
        for name in self.SCRIPTS:
            with self.subTest(script=name):
                mod = _load(name, f"herdr_env_gate_wiring_{name.replace('-', '_')}")
                sentinel = RuntimeError(f"sentinel: {name} chamou require_herdr_pane")
                with mock.patch.object(mod.core, "require_herdr_pane", side_effect=sentinel), \
                     mock.patch.object(sys, "argv", self.ARGV[name]):
                    with self.assertRaises(RuntimeError) as ctx:
                        mod.main()
                self.assertIs(ctx.exception, sentinel, f"{name}.main() nao chamou core.require_herdr_pane() cedo o suficiente")


if __name__ == "__main__":
    unittest.main()
