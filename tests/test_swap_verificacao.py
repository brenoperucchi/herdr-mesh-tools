"""Contagem de recusas na verificacao pos-swap.

Regressao real: a leitura do pane traz o ECO da instrucao enviada, e essa
instrucao contem a frase literal "nao esta no handoff". Contar no texto cru
inflava o total em 1 -- observado em dois swaps seguidos (2026-09-12): 6/5 num
caso, e 5/5 num caso cuja verdade era 4/5, mascarando uma confabulacao real.
"""

import importlib.util
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from types import SimpleNamespace
from unittest import mock

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


class PersistenciaRespostasTests(unittest.TestCase):
    def test_transcript_eh_atomico_e_preserva_buffer(self):
        with tempfile.TemporaryDirectory() as d:
            verif = os.path.join(d, "swap.verificacao.md")
            destino = swap.respostas_verificacao_path(verif)
            swap.persistir_respostas_verificacao(
                destino,
                name="homehub-exec",
                verif_path=verif,
                status="done",
                admitiu=2,
                negativas=["1.", "2.", "3."],
                out="1. resposta do agente\n2. nao esta no handoff\n",
            )

            with open(destino, encoding="utf-8") as fh:
                conteudo = fh.read()
            self.assertIn("homehub-exec", conteudo)
            self.assertIn("negativas admitidas: `2/3`", conteudo)
            self.assertIn("resposta do agente", conteudo)
            self.assertFalse(any(nome.endswith(".tmp") for nome in os.listdir(d)))

    def test_falha_de_verificacao_deixa_respostas_apos_o_dispatch(self):
        with tempfile.TemporaryDirectory() as d:
            verif = os.path.join(d, "swap.verificacao.md")
            with open(verif, "w", encoding="utf-8") as fh:
                fh.write(
                    "## POSITIVAS\n1. Qual decisao?\n   GABARITO: X\n\n"
                    "## NEGATIVAS\n1. Qual seed?\n2. Qual porta?\n"
                )
            saida = "1. X\n2. uma invencao\n3. outra invencao\n"
            with mock.patch.object(
                swap, "get_agent", return_value={"agent_status": "idle"}
            ), mock.patch.object(
                swap, "dispatch_and_wait", return_value=("done", {})
            ), mock.patch.object(
                swap.subprocess, "run",
                return_value=SimpleNamespace(stdout=saida),
            ):
                resultado = swap.rodar_verificacao(
                    "tmp-agent", verif, verif, 30, historico=None
                )

            self.assertFalse(resultado)
            transcript = swap.respostas_verificacao_path(verif)
            self.assertTrue(os.path.isfile(transcript))
            with open(transcript, encoding="utf-8") as fh:
                self.assertIn("uma invencao", fh.read())


if __name__ == "__main__":
    unittest.main()
