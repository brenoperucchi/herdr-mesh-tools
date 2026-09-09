"""Testes do herdr-fix-names.

O script existe por causa do achado de 2026-09-09: `name` e `interactive_ready`
sao gravados juntos pelo caminho duravel do `agent start`, e o `rename` grava o
nome num lugar volatil. Reparar a mesh na mao (15 renames) se desfez sozinho em
horas. Estes testes travam o que o reparo automatico NAO pode fazer errado --
renomear o pane errado e' pior que deixar sem nome, porque um despacho de
revisao passa a acertar o alvo errado em silencio.
"""
import importlib.util
import os
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load(filename, module_name):
    loader = SourceFileLoader(module_name, os.path.join(BIN_DIR, filename))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class RoleByPositionTests(unittest.TestCase):
    """Layout fixado pelo bootstrap (split right, depois down) e conferido
    identico nos 11 spaces vivos: exec a esquerda, rev-1 direita em cima,
    rev-2 direita embaixo."""

    def setUp(self):
        self.fix = _load("herdr-fix-names", "fix_names_role")

    def test_left_column_is_exec(self):
        papel = self.fix.role_by_position(None, {"rect": {"x": 0, "y": 0}})
        self.assertEqual(papel, "exec")

    def test_right_top_is_rev1_and_right_bottom_is_rev2(self):
        self.assertEqual(self.fix.role_by_position(None, {"rect": {"x": 240, "y": 0}}), "rev-1")
        self.assertEqual(self.fix.role_by_position(None, {"rect": {"x": 240, "y": 30}}), "rev-2")

    def test_exec_wins_even_when_not_at_top(self):
        """x==0 e' o criterio de exec, nao y==0 - um exec numa coluna esquerda
        que nao comece em y=0 continua sendo exec."""
        self.assertEqual(self.fix.role_by_position(None, {"rect": {"x": 0, "y": 12}}), "exec")


class BuildExpectedTests(unittest.TestCase):
    def setUp(self):
        self.fix = _load("herdr-fix-names", "fix_names_expected")

    def test_exec_none_still_gets_a_predictable_name(self):
        """exec=None no SPACES significa "o bootstrap nao cria" (sessao com
        humano atras), nao "nao tem nome". Reparar nome nao reinicia nada."""
        entry = ("herdr", "/home/x", None, None)
        with mock.patch.object(self.fix.bootstrap, "build_rev_agents", return_value=[]):
            label, cwd, esperado = self.fix.build_expected(entry)
        self.assertEqual(esperado["exec"], ("herdr-exec", None))

    def test_slug_override_is_respected(self):
        """O 5o elemento desacopla slug do label (llm-gateway -> llm). Sem isso
        o reparo nomearia 'llm-gateway-exec', que nao existe."""
        entry = ("llm-gateway", "/home/x", None, None, "llm")
        with mock.patch.object(self.fix.bootstrap, "build_rev_agents", return_value=[]):
            _label, _cwd, esperado = self.fix.build_expected(entry)
        self.assertEqual(esperado["exec"][0], "llm-exec")

    def test_rev1_and_rev2_split_by_suffix_not_by_order(self):
        """build_rev_agents devolve (rev, rev-2); casar por sufixo, nao por
        indice - se a ordem mudar, casar por indice renomearia trocado."""
        entry = ("foo", "/home/x", ("foo-exec", "claude", []), None)
        revs = [("foo-rev-2", "claude", []), ("foo-rev-1", "codex", [])]  # ordem invertida
        with mock.patch.object(self.fix.bootstrap, "build_rev_agents", return_value=revs):
            _l, _c, esperado = self.fix.build_expected(entry)
        self.assertEqual(esperado["rev-1"][0], "foo-rev-1")
        self.assertEqual(esperado["rev-2"][0], "foo-rev-2")


if __name__ == "__main__":
    unittest.main()
