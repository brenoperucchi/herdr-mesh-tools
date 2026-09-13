"""Swap em duas fases e auto-swap.

O swap normal fecha o antigo assim que o novo responde. Isso e' certo quando
ninguem esta conversando com o antigo. Quando o agent trocado E' aquele com quem
o usuario esta falando, o ponto de nao retorno cai no meio de uma conversa viva
e nao ha como conferir se o substituto pegou o contexto ANTES de perder o
original -- dai as duas fases.

O auto-swap (o agent trocando a si mesmo) e' o caso limite: dois guards mudam de
significado e pedir handoff vira impossivel.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load(name):
    loader = SourceFileLoader(name.replace("-", "_").replace(".py", ""),
                              os.path.join(BIN_DIR, name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


swap = _load("herdr-swap")


class EstadoPendenteTests(unittest.TestCase):
    """A fase 1 grava o que a fase 2 precisa. Se isso se perder, o mesh fica
    com dois agents do mesmo papel vivos e ninguem sabe qual promover."""

    def test_grava_e_le(self):
        with tempfile.TemporaryDirectory() as d:
            p = swap.swap_state_path(d, "herdr-exec")
            dados = {"name": "herdr-exec", "temp_name": "tmpabc",
                     "old_pane_id": "w7:p0", "new_pane_id": "w7:p9",
                     "tab_original": "w7:t1", "tab_temp": "w7:t9"}
            swap.grava_swap_state(p, dados)
            self.assertEqual(swap.le_swap_state(p), dados)

    def test_ausente_devolve_none(self):
        """Sem estado, --finalizar tem que recusar em vez de adivinhar."""
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(swap.le_swap_state(swap.swap_state_path(d, "x")))

    def test_corrompido_devolve_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = swap.swap_state_path(d, "x")
            open(p, "w").write("{ nao e json")
            self.assertIsNone(swap.le_swap_state(p))

    def test_nome_do_arquivo_isola_por_agent(self):
        """Dois swaps pendentes em spaces diferentes nao podem se sobrescrever."""
        with tempfile.TemporaryDirectory() as d:
            self.assertNotEqual(swap.swap_state_path(d, "a-exec"),
                                swap.swap_state_path(d, "b-exec"))

    def test_estado_e_oculto(self):
        """Fica no mesmo dir dos handoffs; nao pode virar ruido na listagem."""
        p = swap.swap_state_path("/tmp/x", "herdr-exec")
        self.assertTrue(os.path.basename(p).startswith("."))


class NomeProvisorioTests(unittest.TestCase):
    def test_cabe_no_limite_do_agent_start(self):
        """`agent start` corta em 32 chars. `f"{name}-new"` ja estourou com
        slug real (content-insights-collector-exec-new = 35)."""
        import hashlib
        import time
        for nome in ("herdr-exec", "content-insights-collector-exec", "x"):
            t = "tmp" + hashlib.sha1(f"{nome}{time.time()}".encode()).hexdigest()[:10]
            with self.subTest(nome=nome):
                self.assertLessEqual(len(t), 32)


class FlagsTests(unittest.TestCase):
    """As flags novas precisam existir com os nomes que a mensagem de erro do
    auto-swap manda o usuario digitar."""

    def test_flags_existem(self):
        import argparse
        fonte = open(os.path.join(BIN_DIR, "herdr-swap"), encoding="utf-8").read()
        for f in ("--em-tab-novo", "--finalizar", "--handoff-pronto"):
            self.assertIn(f'"{f}"', fonte)

    def test_auto_swap_exige_as_duas_condicoes(self):
        """Sem --handoff-pronto o prompt travaria; sem --em-tab-novo o swap
        fecharia o pane que esta executando o proprio swap."""
        fonte = open(os.path.join(BIN_DIR, "herdr-swap"), encoding="utf-8").read()
        self.assertIn("auto-swap exige --handoff-pronto", fonte)
        self.assertIn("auto-swap exige --em-tab-novo", fonte)

    def test_blocked_continua_barrando_no_auto_swap(self):
        """'working' e' artefato da propria chamada; 'blocked' e' dialogo real."""
        fonte = open(os.path.join(BIN_DIR, "herdr-swap"), encoding="utf-8").read()
        self.assertIn('("idle", "done", "working") if auto else ("idle", "done")', fonte)


if __name__ == "__main__":
    unittest.main()
