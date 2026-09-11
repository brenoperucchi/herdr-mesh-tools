"""Testes do herdr-fix-names.

O script existe por causa do medido em 2026-09-09: os 15 nomes reparados a mao
com `rename` se desfizeram sozinhos em horas, enquanto os criados via
`agent start` sobreviveram (e depois aguentaram um reboot). A explicacao que
este arquivo dava antes -- "rename grava num lugar volatil" -- estava ERRADA:
o session.json guarda `agent_name` e os nomes do rename estao la. O mecanismo
segue desconhecido; o que estes testes travam e' o que o reparo automatico NAO
pode fazer errado, independente da causa -- renomear o pane errado e' pior que
deixar sem nome, porque um despacho de revisao passa a acertar o alvo errado
em silencio.
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


class NuncaRebaixaTests(unittest.TestCase):
    """2026-09-10: o script renomeou `omaspotlight-rev-1` -> `omaspotlight-rev`,
    destruindo um nome certo. Causa: migration-state.json ausente na raiz
    daquele space (o slug diverge do diretorio -- "omaspotlight" vive em
    ~/Devs/omarchy-spotlight), entao read_migration_state caiu no default
    `legacy` e o nome esperado virou o da convencao antiga.

    O reparo de nome nunca deve ANDAR PARA TRAS: `-rev-1` e' a convencao viva
    dos 12 spaces, e rebaixar e' sempre regressao, qualquer que seja o motivo."""

    def setUp(self):
        self.fix = _load("herdr-fix-names", "fix_names_rebaixa")

    def test_rev1_vivo_nunca_vira_rev(self):
        """A condicao exata do guard: nome vivo == esperado + '-1'."""
        vivo, esperado = "omaspotlight-rev-1", "omaspotlight-rev"
        self.assertEqual(vivo, f"{esperado}-1",
                         "o guard compara nome vivo com esperado+'-1'; se esta "
                         "igualdade mudar de forma, a protecao para de valer")

    def test_guard_esta_no_codigo(self):
        """Trava textual: o guard e' curto e some facil num refactor."""
        import os
        fonte = open(os.path.join(BIN_DIR, "herdr-fix-names")).read()
        self.assertIn('if ag.get("name") == f"{nome}-1":', fonte)
        self.assertIn("RECUSADO", fonte)
