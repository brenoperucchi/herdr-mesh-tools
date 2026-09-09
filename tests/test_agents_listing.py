"""Testes do herdr-agents (leitura de model/effort REAL do processo).

O script existe na forma atual por causa do achado de 2026-09-09: a coluna
antiga chamada MODEL mostrava o *kind* (claude/codex), nao o modelo. Isso
escondeu por dias que dois revisores rodavam Fable 5.1 -- o modelo mais caro do
catalogo, o dobro do Opus pretendido -- porque tinham subido sem `--model` e
herdaram um default que o `/model` interativo havia reescrito.

Dai a distincao que estes testes travam: PINADO (veio no argv do processo) vs
HERDADO (entre parenteses, veio do config global e muda sozinho).
"""
import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load():
    loader = SourceFileLoader("herdr_agents_test", os.path.join(BIN_DIR, "herdr-agents"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class FromArgvTests(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def test_claude_style_flags(self):
        argv = ["claude", "--model", "opus", "--effort", "high"]
        self.assertEqual(self.m.from_argv(argv), ("opus", "high"))

    def test_codex_style_effort_comes_from_dash_c(self):
        """Codex nao usa --effort: o esforco vem como `-c
        model_reasoning_effort=<v>`. Ler so --effort perderia todo o lado Codex."""
        argv = ["codex", "--model", "gpt-5.6-terra", "-c", "model_reasoning_effort=max",
                "-c", "agents.max_depth=0"]
        self.assertEqual(self.m.from_argv(argv), ("gpt-5.6-terra", "max"))

    def test_bare_launch_reports_nothing_pinned(self):
        """Processo relancado sem flags (o que o reboot de 2026-09-09 fez com
        todo mundo) tem que aparecer como NAO pinado, pra cair no ramo de
        'herdado' e ser exibido entre parenteses."""
        self.assertEqual(self.m.from_argv(["codex"]), (None, None))


class ClaudeDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def _settings(self, data):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".claude"))
        with open(os.path.join(d, ".claude", "settings.json"), "w") as f:
            json.dump(data, f)
        return d

    def test_exact_key_wins_over_substring(self):
        """`model` vem como "opus[1m]"; a chave canonica e' "claude-opus-5".
        Casar por substring sem prioridade deixaria a ordem do dict decidir em
        silencio qual effortLevel vence."""
        home = self._settings({
            "model": "opus[1m]",
            "modelSettings": {
                "claude-sonnet-5": {"effortLevel": "xhigh"},
                "claude-opus-5": {"effortLevel": "high"},
            },
        })
        with mock.patch.object(self.m, "HOME", home):
            model, effort = self.m.defaults_claude()
        self.assertEqual(model, "opus[1m]")
        self.assertEqual(effort, "high")

    def test_ambiguous_substring_returns_no_effort_instead_of_guessing(self):
        """Dois candidatos por substring e nenhum exato: melhor devolver None
        (aparece como '?') do que escolher um e mentir com confianca."""
        home = self._settings({
            "model": "foo",
            "modelSettings": {"a-foo-1": {"effortLevel": "low"}, "b-foo-2": {"effortLevel": "max"}},
        })
        with mock.patch.object(self.m, "HOME", home):
            _model, effort = self.m.defaults_claude()
        self.assertIsNone(effort)


if __name__ == "__main__":
    unittest.main()
