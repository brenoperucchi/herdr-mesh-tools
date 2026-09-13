"""Guardas do handoff gerado por LLM local.

A guarda de truncamento e' a razao de este caminho existir com seguranca. Medido
em 2026-09-12 na mesma maquina, mesmo modelo, mesma sessao: com o prompt inteiro
o modelo nao inventou nenhum identificador; com metade do prompt descartada em
silencio ele fabricou numeros de rodada e afirmou ter executado um script que o
projeto proibe. A diferenca nao foi o modelo, foi o truncamento -- e nada na
resposta HTTP avisa.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load(name):
    path = os.path.join(BIN_DIR, name)
    loader = SourceFileLoader(name.replace("-", "_").replace(".py", ""), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


local = _load("_herdr_local_handoff.py")


class TruncamentoTests(unittest.TestCase):
    def test_colado_no_teto_levanta(self):
        """O caso real: 49.154 avaliados contra teto de 49.152."""
        met = {"prompt_chars": 380_000, "prompt_eval_count": 49_154}
        with self.assertRaises(local.Truncou) as ctx:
            local.confere_truncamento(met)
        self.assertIn("49,154", str(ctx.exception))

    def test_folgado_passa(self):
        """A rodada limpa: 30.276 de teto 49.152."""
        met = {"prompt_chars": 95_868, "prompt_eval_count": 30_276}
        self.assertEqual(local.confere_truncamento(met)["truncamento"], "nao")

    def test_quase_no_teto_ainda_levanta(self):
        """98% do teto ja e' suspeito: o corte nem sempre cai no numero exato."""
        met = {"prompt_chars": 200_000,
               "prompt_eval_count": int(local.TETO_POR_REQUISICAO * 0.99)}
        with self.assertRaises(local.Truncou):
            local.confere_truncamento(met)

    def test_sem_contador_nao_afirma_que_passou(self):
        """Ausencia de prova nao e' prova de ausencia -- nao pode virar 'nao'."""
        met = {"prompt_chars": 1000, "prompt_eval_count": None}
        r = local.confere_truncamento(met)
        self.assertIn("indeterminado", r["truncamento"])
        self.assertNotEqual(r["truncamento"], "nao")


class OrcamentoTests(unittest.TestCase):
    def test_orcamento_cabe_no_teto(self):
        """O orcamento em chars, convertido de volta, tem que ficar sob o teto."""
        chars = local.orcamento_chars()
        tokens = chars / local.CHARS_POR_TOKEN
        self.assertLess(tokens, local.TETO_POR_REQUISICAO)

    def test_razao_e_conservadora(self):
        """3,0 chars/token contra 3,17 medido: erra pro lado que corta log,
        nao pro lado que trunca em silencio."""
        self.assertLessEqual(local.CHARS_POR_TOKEN, 3.17)


class RecorteTests(unittest.TestCase):
    def test_fica_com_as_mais_recentes(self):
        msgs = ["a" * 100, "b" * 100, "c" * 100]
        r = local.recorta(msgs, 250)
        self.assertEqual(len(r), 2)
        self.assertTrue(r[-1].startswith("c"), "a ultima mensagem tem que sobreviver")

    def test_cabe_no_limite(self):
        msgs = ["x" * 50 for _ in range(20)]
        r = local.recorta(msgs, 200)
        self.assertLessEqual(len("\n\n".join(r)), 200)

    def test_vazio_quando_nada_cabe(self):
        self.assertEqual(local.recorta(["y" * 500], 10), [])


class LeituraDeLogTests(unittest.TestCase):
    def test_le_user_e_assistant_e_ignora_o_resto(self):
        linhas = [
            {"message": {"role": "user", "content": "pergunta"}},
            {"message": {"role": "assistant",
                         "content": [{"type": "text", "text": "resposta"},
                                     {"type": "tool_use", "name": "Bash"}]}},
            {"message": {"role": "system", "content": "ignorar"}},
            {"nao_e_mensagem": True},
            "linha invalida que nao e json",
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            for l in linhas:
                fh.write((json.dumps(l) if not isinstance(l, str) else l) + "\n")
            caminho = fh.name
        try:
            msgs = local.mensagens_do_log(caminho)
            self.assertEqual(len(msgs), 2)
            self.assertIn("pergunta", msgs[0])
            self.assertIn("resposta", msgs[1])
            self.assertNotIn("Bash", msgs[1], "bloco tool_use nao e' texto")
            self.assertFalse(any("ignorar" in m for m in msgs))
        finally:
            os.unlink(caminho)

    def test_log_vazio_nao_estoura(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            caminho = fh.name
        try:
            self.assertEqual(local.mensagens_do_log(caminho), [])
        finally:
            os.unlink(caminho)


class CabecalhoTests(unittest.TestCase):
    def test_avisa_que_nao_e_do_agent(self):
        """Quem le tem que saber que isto e' resumo de terceiro."""
        txt = local.AVISO_CABECALHO.format(modelo="m", log="/l.jsonl")
        self.assertIn("não pelo agent que saiu", txt)
        self.assertIn("confira todo hash", txt)
        self.assertIn("/l.jsonl", txt)


if __name__ == "__main__":
    unittest.main()


class CortaVerificacaoTests(unittest.TestCase):
    """O modelo local ignora a proibicao de escrever verificacao (medido 2x).

    Nao e' capricho de formato: um GABARITO escrito por quem nao viveu a sessao
    e' invencao com cara de fonte autoritativa, no campo que ninguem relê.
    """

    def test_corta_a_partir_de_positivas(self):
        txt = "# Handoff\n\nConteudo real.\n\n## POSITIVAS\n1. p\n   GABARITO: g\n"
        limpo, cortou = local.corta_verificacao(txt)
        self.assertTrue(cortou)
        self.assertIn("Conteudo real.", limpo)
        self.assertNotIn("GABARITO", limpo)

    def test_corta_secao_teste_de_verificacao(self):
        txt = "# H\n\nCorpo.\n\n## Teste de Verificação\n\n### POSITIVAS\n1. x\n"
        limpo, cortou = local.corta_verificacao(txt)
        self.assertTrue(cortou)
        self.assertNotIn("POSITIVAS", limpo)
        self.assertIn("Corpo.", limpo)

    def test_corta_no_primeiro_cabecalho_nao_no_ultimo(self):
        txt = "# H\n\nCorpo.\n\n## POSITIVAS\n1. a\n\n## NEGATIVAS\n1. b\n"
        limpo, _ = local.corta_verificacao(txt)
        self.assertNotIn("NEGATIVAS", limpo)
        self.assertNotIn("POSITIVAS", limpo)

    def test_handoff_sem_verificacao_fica_intacto(self):
        txt = "# Handoff\n\nSo o handoff, nada mais.\n"
        limpo, cortou = local.corta_verificacao(txt)
        self.assertFalse(cortou)
        self.assertEqual(limpo, txt)

    def test_nao_corta_mencao_no_meio_de_frase(self):
        """'as positivas foram X' em prosa nao e' cabecalho de secao."""
        txt = "# H\n\nAs positivas do trimestre foram boas.\n\nMais corpo.\n"
        limpo, cortou = local.corta_verificacao(txt)
        self.assertFalse(cortou)
        self.assertIn("Mais corpo.", limpo)

    def test_pega_variante_em_negrito(self):
        txt = "# H\n\nCorpo.\n\n**POSITIVAS**\n1. a\n"
        _, cortou = local.corta_verificacao(txt)
        self.assertTrue(cortou)
