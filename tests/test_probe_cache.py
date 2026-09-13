"""Cache da sonda de model/effort.

O ponto delicado nao e' guardar valor, e' a ORDEM DE CONFIANCA: uma passagem da
sonda nao pode apagar uma confirmacao boa com um palpite pior. Sem isso, um
Claude confirmado as 22h viraria `(unknown)` na proxima varredura em que o log
ainda nao tivesse resposta nova.
"""

import datetime
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


pc = _load("_herdr_probe_cache.py")
probe = _load("herdr-probe")


def _e(origem, visto_em, model="m", effort="e"):
    return {"model": model, "effort": effort, "origem": origem, "visto_em": visto_em}


class OrdemDeConfiancaTests(unittest.TestCase):
    def test_pane_ganha_de_thread_mesmo_sendo_mais_velho(self):
        antigo = _e("pane", "2026-09-12T20:00:00+00:00")
        novo = _e("thread", "2026-09-12T23:00:00+00:00")
        self.assertEqual(pc.melhor(antigo, novo)["origem"], "pane")

    def test_pior_nao_apaga_confirmacao(self):
        """A regressao que este teste existe pra impedir."""
        conf = _e("thread-confirmado", "2026-09-12T22:00:00+00:00", model="opus-5")
        palpite = _e("thread", "2026-09-12T22:30:00+00:00", model="?")
        self.assertEqual(pc.melhor(conf, palpite)["model"], "opus-5")

    def test_empate_fica_com_o_mais_recente(self):
        a = _e("pane", "2026-09-12T20:00:00+00:00", model="velho")
        b = _e("pane", "2026-09-12T21:00:00+00:00", model="novo")
        self.assertEqual(pc.melhor(a, b)["model"], "novo")

    def test_sem_anterior_aceita_qualquer_um(self):
        n = _e("thread", "2026-09-12T20:00:00+00:00")
        self.assertEqual(pc.melhor(None, n), n)


class IdadeTests(unittest.TestCase):
    def test_calcula_minutos(self):
        ref = datetime.datetime(2026, 9, 12, 23, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(pc.idade_min(_e("pane", "2026-09-12T22:30:00+00:00"), ref), 30)

    def test_timestamp_invalido_nao_estoura(self):
        self.assertIsNone(pc.idade_min({"visto_em": "nao e data"}))
        self.assertIsNone(pc.idade_min({}))


class PersistenciaTests(unittest.TestCase):
    def test_grava_e_le(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "probe.json")
            pc.gravar({"x": _e("pane", pc.agora())}, p)
            self.assertEqual(list(pc.ler(p)), ["x"])

    def test_arquivo_corrompido_vira_dict_vazio(self):
        """A sonda escreve enquanto alguem pode estar listando; leitura ruim
        nao pode derrubar o listing."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{ isto nao e json")
            p = fh.name
        try:
            self.assertEqual(pc.ler(p), {})
        finally:
            os.unlink(p)

    def test_ausente_vira_dict_vazio(self):
        self.assertEqual(pc.ler("/nao/existe/probe.json"), {})


class RodapeCodexTests(unittest.TestCase):
    def test_le_rodape_real(self):
        txt = "  gpt-6-astra medium · ~/Devs/miqueias/MFC · titulo\n"
        self.assertEqual(probe.do_rodape(txt), ("gpt-6-astra", "medium"))

    def test_pega_o_ultimo_nao_o_primeiro(self):
        """Prosa acima do rodape pode citar modelos; o rodape e' o que vale."""
        txt = ("falamos de gpt-5.6-sol xhigh no meio da conversa\n"
               "  gpt-6-astra low · ~/Devs/x\n")
        self.assertEqual(probe.do_rodape(txt), ("gpt-6-astra", "low"))

    def test_pane_claude_nao_casa(self):
        """Claude nao imprime modelo na tela -- nao pode inventar um."""
        txt = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← 9 agents\n"
        self.assertEqual(probe.do_rodape(txt), (None, None))

    def test_texto_vazio(self):
        self.assertEqual(probe.do_rodape(""), (None, None))


if __name__ == "__main__":
    unittest.main()
