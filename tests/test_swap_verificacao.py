"""Contagem de recusas na verificacao pos-swap.

Regressao real: a leitura do pane traz o ECO da instrucao enviada, e essa
instrucao contem a frase literal "nao esta no handoff". Contar no texto cru
inflava o total em 1 -- observado em dois swaps seguidos (2026-09-12): 6/5 num
caso, e 5/5 num caso cuja verdade era 4/5, mascarando uma confabulacao real.
"""

import importlib.util
import os
import unittest
from importlib.machinery import SourceFileLoader

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load(name):
    path = os.path.join(BIN_DIR, name)
    loader = SourceFileLoader(name.replace("-", "_"), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


swap = _load("herdr-swap")

ECO = (
    "> Responda numeradas, em sequencia, direto. Nao releia o projeto nem\n"
    "  pesquise: quero o que voce retem do handoff. Se a resposta NAO estiver\n"
    "  no handoff, escreva exatamente 'nao esta no handoff' "
    + swap.FIM_DA_INSTRUCAO + ".\n\n"
)


class ContaRecusasTests(unittest.TestCase):
    def test_nao_conta_o_eco_da_instrucao(self):
        """Eco presente, zero recusas de verdade -> 0, nao 1."""
        self.assertEqual(swap.conta_recusas(ECO + "1. resposta boa\n2. outra boa\n"), 0)

    def test_caso_real_llm_bench_quatro_de_cinco(self):
        """O swap que reportava 5/5 quando a verdade era 4/5."""
        out = ECO + (
            "1. resposta boa\n"
            "6. nao esta no handoff\n"
            "7. A alternativa e' o servidor reservar metade do contexto.\n"
            "8. nao esta no handoff\n"
            "9. nao esta no handoff\n"
            "10. nao esta no handoff\n"
        )
        self.assertEqual(swap.conta_recusas(out), 4)

    def test_todas_recusadas(self):
        out = ECO + "".join("%d. nao esta no handoff\n" % i for i in range(1, 6))
        self.assertEqual(swap.conta_recusas(out), 5)

    def test_variantes_de_acento_e_caixa(self):
        out = ECO + "1. Nao esta no handoff\n2. nao está no handoff\n3. NAO ESTA NO HANDOFF\n"
        self.assertEqual(swap.conta_recusas(out), 3)

    def test_sem_eco_conta_tudo(self):
        """Se o marco nao aparecer (pane truncou o eco), nao perde recusas."""
        self.assertEqual(swap.conta_recusas("1. nao esta no handoff\n2. nao esta no handoff\n"), 2)

    def test_usa_o_ultimo_eco_nao_o_primeiro(self):
        """Duas verificacoes no mesmo pane: so a ultima conta."""
        out = ECO + "1. nao esta no handoff\n" + ECO + "1. nao esta no handoff\n2. boa\n"
        self.assertEqual(swap.conta_recusas(out), 1)


if __name__ == "__main__":
    unittest.main()
