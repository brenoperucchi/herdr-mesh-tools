"""herdr-can-send: o agent pergunta "posso enviar?" em vez de decidir lendo o pane.

Motivo (strata-exec, 2026-10-01): `herdr pane read` puro nao mostra o faint, entao a
sugestao automatica do Claude Code e um rascunho humano ficam identicos para um
modelo. Ele parou e perguntou ao humano tres vezes; a producao do llm-bench ficou
parada esperando o "FIM DA JANELA". A checagem do mesh ja le o estilo; faltava um
comando chamavel.
"""
import importlib.util
import json
import os
import sys
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

import _herdr_dispatch as core  # noqa: E402

ESC = "\x1b"
TXT = "pode mandar os resultados para o mfc-exec"
SUGESTAO_ANSI = f"❯ {ESC}[0m{ESC}[2m{TXT}{ESC}[0m"
HUMANO_ANSI = f"❯ {ESC}[0m{TXT}"


def _load():
    loader = SourceFileLoader("herdr_can_send_test", os.path.join(BIN_DIR, "herdr-can-send"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _fake_run(detection, ansi="", detection_rc=0):
    def fake(args, **kw):
        r = mock.Mock(returncode=0, stderr="", stdout="")
        if "get" in args:
            r.stdout = json.dumps({"result": {"pane": {"scroll": {"offset_from_bottom": 0}}}})
        elif "read" in args and "detection" in args:
            r.returncode, r.stdout = detection_rc, detection
        elif "read" in args:
            r.stdout = ansi
        return r
    return fake


class ClassifyTests(unittest.TestCase):
    def classe(self, detection, ansi="", rc=0):
        with mock.patch.object(core.subprocess, "run", side_effect=_fake_run(detection, ansi, rc)):
            return core.classify_composition("wQ:pP")

    def test_sugestao_do_claude_em_faint(self):
        tipo, texto, _ = self.classe(f"● ok\n❯ {TXT}\n⏵⏵ bypass", SUGESTAO_ANSI)
        self.assertEqual((tipo, texto), ("sugestao", TXT))

    def test_texto_digitado_e_humano(self):
        tipo, _, _ = self.classe(f"● ok\n❯ {TXT}\n⏵⏵ bypass", HUMANO_ANSI)
        self.assertEqual(tipo, "humano")

    def test_caixa_vazia(self):
        self.assertEqual(self.classe("● ok\n❯\n⏵⏵ bypass")[0], "vazia")

    def test_placeholder_do_codex(self):
        self.assertEqual(self.classe("• ok\n› Ask Codex to do anything\n  gpt")[0], "placeholder")

    def test_dialogo_pendente(self):
        self.assertEqual(self.classe("Do you trust the contents of this directory?\n❯ 1. Yes")[0], "dialogo")

    def test_falha_de_leitura_e_indeterminada(self):
        self.assertEqual(self.classe("", rc=1)[0], "indeterminado")


class DecidirTests(unittest.TestCase):
    def setUp(self):
        self.m = _load()
        self.info = {"name": "x", "pane_id": "w1:p1", "agent": "claude", "agent_status": "idle"}

    def decide(self, tipo, status="idle", texto="", allow_working=False):
        with mock.patch.object(core, "effective_status", return_value=status), \
             mock.patch.object(core, "classify_composition", return_value=(tipo, texto, "m")):
            return self.m.decidir(self.info, allow_working)[0]

    def test_codigos(self):
        self.assertEqual(self.decide("vazia"), 0)
        self.assertEqual(self.decide("placeholder"), 0)
        self.assertEqual(self.decide("sugestao", texto="t"), 0)
        self.assertEqual(self.decide("humano", texto="t"), 1)
        self.assertEqual(self.decide("dialogo"), 2)
        self.assertEqual(self.decide("indeterminado"), 4)

    def test_working_e_blocked_barram(self):
        self.assertEqual(self.decide("vazia", status="working"), 3)
        self.assertEqual(self.decide("vazia", status="blocked"), 3)

    def test_allow_working_so_libera_working(self):
        self.assertEqual(self.decide("vazia", status="working", allow_working=True), 0)
        self.assertEqual(self.decide("vazia", status="blocked", allow_working=True), 3)

    def test_rascunho_humano_barra_mesmo_com_allow_working(self):
        self.assertEqual(self.decide("humano", status="working", texto="t", allow_working=True), 1)

    def test_sugestao_nao_e_confundida_com_rascunho_no_texto(self):
        with mock.patch.object(core, "effective_status", return_value="idle"), \
             mock.patch.object(core, "classify_composition", return_value=("sugestao", TXT, "m")):
            code, rotulo, detalhe, *_ = self.m.decidir(self.info)
        self.assertEqual((code, rotulo), (0, "PODE ENVIAR"))
        self.assertIn("nao e rascunho", detalhe)


if __name__ == "__main__":
    unittest.main()
