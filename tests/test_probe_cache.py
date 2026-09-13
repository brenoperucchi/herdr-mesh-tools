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
# do_rodape/amostra_um moram no herdr-agents, nao na sonda: o caminho do Claude
# precisa das funcoes de log daquele modulo, e o `--force` do listing usa a
# mesma funcao. A sonda e' so o laco por cima.
ha = _load("herdr-agents")


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
        self.assertEqual(ha.do_rodape(txt), ("gpt-6-astra", "medium"))

    def test_pega_o_ultimo_nao_o_primeiro(self):
        """Prosa acima do rodape pode citar modelos; o rodape e' o que vale."""
        txt = ("falamos de gpt-5.6-sol xhigh no meio da conversa\n"
               "  gpt-6-astra low · ~/Devs/x\n")
        self.assertEqual(ha.do_rodape(txt), ("gpt-6-astra", "low"))

    def test_pane_claude_nao_casa(self):
        """Claude nao imprime modelo na tela -- nao pode inventar um."""
        txt = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← 9 agents\n"
        self.assertEqual(ha.do_rodape(txt), (None, None))

    def test_texto_vazio(self):
        self.assertEqual(ha.do_rodape(""), (None, None))


if __name__ == "__main__":
    unittest.main()


class DivergenciaTests(unittest.TestCase):
    """argv e' a INTENCAO do lancamento; pane e' o FATO. Divergir nao e' erro --
    trocar modelo na mao e' legitimo -- mas e' o que o bootstrap recriaria
    diferente, entao precisa ser visivel."""

    def setUp(self):
        self.sonda = _load("herdr-probe")

    def _fake_ha(self, argv):
        class FakeHA:
            @staticmethod
            def api(*a, **k):
                return {"process_info": {"foreground_processes": [{"argv": argv}]}}
            from_argv = staticmethod(ha.from_argv)
        return FakeHA

    def test_acusa_effort_diferente(self):
        agents = [{"name": "x", "pane_id": "w1:p1"}]
        c = {"x": _e("pane", pc.agora(), model="gpt-6-astra", effort="medium")}
        argv = ["codex", "--model", "gpt-6-astra", "-c", "model_reasoning_effort=high"]
        fora = self.sonda.divergencias(self._fake_ha(argv), agents, c)
        self.assertEqual(len(fora), 1)
        self.assertIn("high", fora[0][1])
        self.assertIn("medium", fora[0][2])

    def test_acusa_modelo_diferente(self):
        agents = [{"name": "x", "pane_id": "w1:p1"}]
        c = {"x": _e("pane", pc.agora(), model="gpt-6-astra", effort="max")}
        argv = ["codex", "--model", "gpt-5.6-luna", "-c", "model_reasoning_effort=max"]
        self.assertEqual(len(self.sonda.divergencias(self._fake_ha(argv), agents, c)), 1)

    def test_igual_nao_acusa(self):
        agents = [{"name": "x", "pane_id": "w1:p1"}]
        c = {"x": _e("pane", pc.agora(), model="gpt-6-astra", effort="high")}
        argv = ["codex", "--model", "gpt-6-astra", "-c", "model_reasoning_effort=high"]
        self.assertEqual(self.sonda.divergencias(self._fake_ha(argv), agents, c), [])

    def test_ignora_entrada_que_nao_veio_do_pane(self):
        """So o pane e' fato. Comparar argv com um palpite de log daria ruido."""
        agents = [{"name": "x", "pane_id": "w1:p1"}]
        c = {"x": _e("thread", pc.agora(), model="outro", effort="low")}
        argv = ["codex", "--model", "gpt-6-astra", "-c", "model_reasoning_effort=high"]
        self.assertEqual(self.sonda.divergencias(self._fake_ha(argv), agents, c), [])

    def test_sem_argv_nao_acusa(self):
        """Processo sem model/effort no argv nao tem intencao declarada."""
        agents = [{"name": "x", "pane_id": "w1:p1"}]
        c = {"x": _e("pane", pc.agora(), model="gpt-6-astra", effort="high")}
        self.assertEqual(self.sonda.divergencias(self._fake_ha(["codex"]), agents, c), [])
