"""Assentamento de Codex na espera do dispatcher (Herdr >= 0.9.2, #4563).

O Herdr deixa todo Codex `unknown` depois de um turno, entao o `--wait --until
idle/done` nunca resolve (llm37, 2026-09-30: rev-1 TIMEOUT 1200s com o veredito de
8558 bytes ja publicado). A rodada assenta quando o artefato obrigatorio e' novo
e pronto E a tela fica ociosa por CODEX_SETTLE_POLLS consultas seguidas. Timeout
nunca vira aprovacao: sem as duas condicoes, o resultado continua `timeout`.
"""
import importlib.util
import os
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

from test_blocked_grace import BIN_DIR, FakeClock

OCIOSA = "Worked for 3m 0s • 7:06 PM\n› Ask Codex to do anything\n  gpt-6.1-sol xhigh · ~/x\n  ? for shortcuts"
TRABALHANDO = "• Working (12s • esc to interrupt)\n› Ask Codex to do anything\n  gpt-6.1-sol xhigh · ~/x"
ARTEFATO = {"bytes": 8558, "mtime_ns": 2, "inode": 7}


def _load():
    loader = SourceFileLoader("herdr_review_dispatch_settle", os.path.join(BIN_DIR, "herdr-review-dispatch"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class CodexSettleTests(unittest.TestCase):
    def setUp(self):
        self.core = _load().core
        delivery_guard = mock.patch.object(self.core, "require_can_send", return_value=None)
        delivery_guard.start()
        self.addCleanup(delivery_guard.stop)
        self.clock = FakeClock()
        for patcher in (
            mock.patch.object(self.core.time, "time", self.clock.time),
            mock.patch.object(self.core.time, "sleep", self.clock.sleep),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_dispatch(self, telas, artefato_pronto, agent="codex", status="unknown", timeout_s=120):
        """`telas`: lista de telas devolvidas a cada consulta (a ultima se repete).
        `artefato_pronto(chamada)`: o artefato esta pronto na N-esima checagem?"""
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.communicate.return_value = ("", "")
        info = {"name": "rev", "agent": agent, "agent_status": status,
                "interactive_ready": True, "pane_id": "wK:p6"}
        telas_it = iter(telas)
        ultima = {"tela": telas[-1]}

        def tela(_info):
            ultima["tela"] = next(telas_it, ultima["tela"])
            return ultima["tela"]

        chamadas = {"n": 0}

        def pronto(path, baseline):
            chamadas["n"] += 1
            return ARTEFATO if artefato_pronto(chamadas["n"]) else None

        with mock.patch.object(self.core.subprocess, "Popen", return_value=proc), \
             mock.patch.object(self.core, "get_agent_info", return_value=dict(info)), \
             mock.patch.object(self.core, "_read_detection_screen", side_effect=tela), \
             mock.patch.object(self.core, "_artifact_snapshot", return_value=None), \
             mock.patch.object(self.core, "_artifact_changed_and_ready", side_effect=pronto):
            return self.core.dispatch_and_wait_all(
                {"rev": "prompt"}, timeout_s=timeout_s, required_artifacts={"rev": "/x/verdict.md"})

    def test_artefato_pronto_e_tela_ociosa_assenta_sem_esperar_o_timeout(self):
        result, info, _ = self.run_dispatch([OCIOSA], lambda n: True)
        self.assertEqual(result["rev"], "idle")
        self.assertEqual(info["rev"]["settled_by"], "codex_artifact_and_screen")
        self.assertEqual(info["rev"]["required_artifact"], ARTEFATO)
        self.assertLess(self.clock.t, 20, "nao deveria consumir o timeout inteiro")

    def test_exige_consultas_seguidas_antes_de_assentar(self):
        self.run_dispatch([OCIOSA], lambda n: True)
        self.assertGreaterEqual(self.clock.t, 2 * (self.core.CODEX_SETTLE_POLLS - 1))

    def test_sem_artefato_nao_assenta_e_vira_timeout(self):
        result, _, _ = self.run_dispatch([OCIOSA], lambda n: False, timeout_s=60)
        self.assertEqual(result["rev"], "timeout")
        self.assertAlmostEqual(self.clock.t, 60, delta=3)

    def test_artefato_pronto_mas_tela_trabalhando_nao_assenta(self):
        result, _, _ = self.run_dispatch([TRABALHANDO], lambda n: True, timeout_s=60)
        self.assertEqual(result["rev"], "timeout")

    def test_tela_trabalhando_no_meio_zera_a_contagem(self):
        telas = [OCIOSA, OCIOSA, TRABALHANDO] + [OCIOSA] * 20
        result, _, _ = self.run_dispatch(telas, lambda n: True)
        self.assertEqual(result["rev"], "idle")
        # com a contagem zerada pelo "trabalhando", precisa de 2 + 1 + 3 consultas
        self.assertGreaterEqual(self.clock.t, 2 * (2 + 1 + self.core.CODEX_SETTLE_POLLS - 1))

    def test_artefato_que_aparece_depois_assenta_depois_dele(self):
        result, _, _ = self.run_dispatch([OCIOSA], lambda n: n > 5)
        self.assertEqual(result["rev"], "idle")

    def test_outro_kind_nao_assenta_por_tela_e_artefato(self):
        result, _, _ = self.run_dispatch([OCIOSA], lambda n: True, agent="claude", status="working", timeout_s=60)
        self.assertEqual(result["rev"], "timeout")

    def test_codex_sem_interactive_ready_nao_assenta(self):
        with mock.patch.object(self.core, "inferir_idle", return_value=False):
            result, _, _ = self.run_dispatch([OCIOSA], lambda n: True, timeout_s=60)
        self.assertEqual(result["rev"], "timeout")


if __name__ == "__main__":
    unittest.main()
