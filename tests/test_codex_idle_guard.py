"""Guardas de disparo com Codex `unknown` (Herdr >= 0.9.2, #4563).

O Herdr deixa todo Codex `unknown` depois de um turno. As tres guardas de
disparo (herdr-ask, herdr-review-dispatch e o preflight de _herdr_dispatch)
passam a aceitar esse caso SO quando a tela esta ociosa; working, rascunho
humano e qualquer outro agent continuam barrados.
"""
import os
import sys
import unittest
from unittest import mock

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

import _herdr_dispatch as core  # noqa: E402

OCIOSA = "Worked for 3m 0s • 7:06 PM\n› Ask Codex to do anything\n  gpt-6.1-sol xhigh · ~/x\n  ? for shortcuts"
TRABALHANDO = "• Working (12s • esc to interrupt)\n› Ask Codex to do anything\n  gpt-6.1-sol xhigh · ~/x"
RASCUNHO = "Worked for 5s • 7:06 PM\n› revisar o parser\n  gpt-6.1-sol xhigh · ~/x"


def _info(**kw):
    base = {"name": "mfc-rev-1", "agent": "codex", "pane_id": "w5:pP", "agent_status": "unknown",
            "interactive_ready": True, "cwd": "/x", "agent_session": None}
    base.update(kw)
    return base


class EffectiveStatusTests(unittest.TestCase):
    def status(self, tela, **kw):
        with mock.patch.object(core, "_read_detection_screen", return_value=tela):
            return core.effective_status(_info(**kw))

    def test_codex_unknown_com_tela_ociosa_vale_idle(self):
        self.assertEqual(self.status(OCIOSA), "idle")

    def test_codex_unknown_trabalhando_continua_unknown(self):
        self.assertEqual(self.status(TRABALHANDO), "unknown")

    def test_codex_unknown_com_rascunho_humano_continua_unknown(self):
        self.assertEqual(self.status(RASCUNHO), "unknown")

    def test_sem_interactive_ready_nao_infere(self):
        self.assertEqual(self.status(OCIOSA, interactive_ready=None), "unknown")

    def test_outro_kind_nao_infere(self):
        self.assertEqual(self.status(OCIOSA, agent="claude"), "unknown")

    def test_status_que_o_herdr_ja_informa_nao_e_tocado(self):
        for st in ("working", "blocked", "idle", "done"):
            self.assertEqual(self.status(OCIOSA, agent_status=st), st)

    def test_falha_de_leitura_nunca_vira_idle(self):
        with mock.patch.object(core, "_read_detection_screen", side_effect=OSError("boom")):
            self.assertEqual(core.effective_status(_info()), "unknown")


class PreflightTests(unittest.TestCase):
    def observe(self, tela):
        expected = _info()
        with mock.patch.object(core, "get_agent_info", return_value=_info()), \
             mock.patch.object(core, "_read_detection_screen", return_value=tela), \
             mock.patch.object(core, "pane_looks_busy_with_human_input", return_value=(False, None)):
            return core._dispatch_observe("mfc-rev-1", expected)

    def test_preflight_aceita_codex_unknown_ocioso(self):
        self.assertEqual(self.observe(OCIOSA)["name"], "mfc-rev-1")

    def test_preflight_recusa_codex_unknown_trabalhando(self):
        with self.assertRaises(core.DispatchPreflightError):
            self.observe(TRABALHANDO)

    def test_preflight_recusa_codex_unknown_com_rascunho(self):
        with self.assertRaises(core.DispatchPreflightError):
            self.observe(RASCUNHO)

    def test_preflight_ainda_recusa_dialogo_pendente_mesmo_com_tela_ociosa(self):
        expected = _info()
        with mock.patch.object(core, "get_agent_info", return_value=_info()), \
             mock.patch.object(core, "_read_detection_screen", return_value=OCIOSA), \
             mock.patch.object(core, "pane_looks_busy_with_human_input", return_value=(True, "dialogo")):
            with self.assertRaises(core.DispatchPreflightError):
                core._dispatch_observe("mfc-rev-1", expected)


class GuardWiringTests(unittest.TestCase):
    """As duas guardas de linha de comando precisam chamar a funcao efetiva,
    nao o agent_status cru (senao voltam a recusar todo Codex)."""

    def _fonte(self, nome):
        with open(os.path.join(BIN_DIR, nome), encoding="utf-8") as f:
            return f.read()

    def test_herdr_ask_usa_status_efetivo(self):
        src = self._fonte("herdr-ask")
        self.assertIn("core.effective_agent_status(name)", src)

    def test_herdr_review_dispatch_usa_status_efetivo(self):
        src = self._fonte("herdr-review-dispatch")
        self.assertIn("core.effective_status(info)", src)


if __name__ == "__main__":
    unittest.main()
