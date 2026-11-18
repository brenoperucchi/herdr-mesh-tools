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
        patcher = mock.patch.object(core, "pane_shell_signal", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

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


def _shell_run(procs, tail="", read_rc=0):
    """process-info devolve `procs` (lista de argv); `pane read` devolve `tail`."""
    def fake(args, **kw):
        r = mock.Mock(returncode=0, stderr="", stdout="")
        if "process-info" in args:
            r.stdout = json.dumps({"result": {"process_info": {"foreground_processes": [
                {"argv": a, "name": os.path.basename(a[0])} for a in procs]}}})
        elif "read" in args:
            r.returncode, r.stdout = read_rc, tail
        return r
    return fake


class PaneVirouShellTests(unittest.TestCase):
    """Queda das 17:09 (18/11): tres revisores Codex morreram, o pane virou shell e o dispatcher digitou o prompt no shell."""

    def sinal(self, procs, tail="", read_rc=0):
        with mock.patch.object(core.subprocess, "run", side_effect=_shell_run(procs, tail, read_rc)):
            return core.pane_shell_signal("wQ:pP")

    def test_so_o_shell_em_primeiro_plano_e_pane_que_virou_shell(self):
        for argv in (["/usr/bin/bash"], ["-bash"], ["zsh"], ["/usr/bin/fish"]):
            with self.subTest(argv=argv):
                self.assertIn("pane virou shell", self.sinal([argv]))

    def test_agent_vivo_nao_e_shell_inclusive_wrapper_node(self):
        self.assertIsNone(self.sinal([["/x/codex", "resume", "s"]], "› Ask Codex to do anything"))
        self.assertIsNone(self.sinal([["node", "/x/codex.js"]], "› pronto"))
        self.assertIsNone(self.sinal([["claude", "--resume", "s"]], "❯"))
        self.assertIsNone(self.sinal([["bash"], ["claude"]], "❯"))            # shell E agent no grupo: agent vivo

    def test_linha_de_shell_no_fim_da_tela_recusa_so_com_processo_indeterminado(self):
        # r211: com process-info indisponivel/vazio, a ultima linha de shell sem caixa de agent prova a queda
        self.assertIn("pane virou shell", self.sinal([], "algo\n[ble: exit 130]\n"))
        self.assertIn("pane virou shell", self.sinal([], "bash: codexx: command not found"))
        with mock.patch.object(core, "api", side_effect=RuntimeError("sem process-info")), \
             mock.patch.object(core.subprocess, "run", side_effect=_shell_run([], "bash: x: command not found")):
            self.assertIn("pane virou shell", core.pane_shell_signal("wQ:pP"))

    def test_agent_vivo_que_cita_o_marcador_nao_e_declarado_morto(self):
        # r211 P2 (rev-1 e rev-2): as duas telas do parecer, com process-info legivel de agent
        codex = [["/usr/bin/codex", "resume", "s"]]
        for tela in ("• Diagnóstico concluído: bash: rgx: command not found\n\n› Ask Codex to do anything\n  gpt-6.1-sol",
                     "• O log anterior terminou em [ble: exit 130].\n\n› Ask Codex to do anything\n  gpt-6.1-sol",
                     "bash: rgx: command not found"):
            for procs in (codex, [["claude"]], [["node", "/x/codex.js"]], [["bash"], ["codex"]]):
                with self.subTest(tela=tela[:30], procs=procs[0][0]):
                    self.assertIsNone(self.sinal(procs, tela))

    def test_fallback_textual_nao_vale_com_caixa_de_agent_nem_citacao_no_corpo(self):
        # sem process-info: citacao no corpo da resposta (nao e' a ultima linha) ou caixa de composicao presente nao recusa
        self.assertIsNone(self.sinal([], "• deu [ble: exit 130] antes\n› Ask Codex to do anything\n  gpt"))
        self.assertIsNone(self.sinal([], "• deu command not found antes\nresposta continua aqui"))
        self.assertIsNone(self.sinal([], "● ok\n❯ texto\n⏵⏵ bypass"))

    def test_decidir_com_as_duas_telas_do_parecer_da_codigo_0_e_3(self):
        m = _load()
        info = {"name": "x", "pane_id": "w1:p1", "agent": "codex", "agent_status": "idle"}
        tela = "• Diagnóstico: bash: rgx: command not found\n\n› Ask Codex to do anything\n  gpt"
        for status, esperado in (("idle", 0), ("working", 3)):
            with mock.patch.object(core.subprocess, "run", side_effect=_shell_run([["/usr/bin/codex"]], tela)), \
                 mock.patch.object(core, "effective_status", return_value=status):
                self.assertEqual(m.decidir(info)[0], esperado)

    def test_prompt_com_seta_do_agent_nao_e_tratado_como_shell(self):
        self.assertIsNone(self.sinal([["claude"]], "● ok\n❯ texto\n⏵⏵ bypass"))

    def test_linha_antiga_fora_das_ultimas_linhas_nao_conta(self):
        self.assertIsNone(self.sinal([["claude"]], "[ble: exit 1]\n" + "\n".join(f"l{i}" for i in range(20))))

    def test_leitura_indeterminada_nao_bloqueia_aqui(self):
        with mock.patch.object(core, "api", side_effect=RuntimeError("x")), \
             mock.patch.object(core.subprocess, "run", side_effect=_shell_run([], "", read_rc=1)):
            self.assertIsNone(core.pane_shell_signal("wQ:pP"))

    def test_can_send_devolve_codigo_5_e_a_guarda_do_dispatcher_recusa(self):
        m = _load()
        info = {"name": "x", "pane_id": "w1:p1", "agent": "codex", "agent_status": "idle"}
        with mock.patch.object(core, "pane_shell_signal", return_value="pane virou shell, recrie o pane"), \
             mock.patch.object(core, "classify_composition", side_effect=AssertionError("nao deve ler a caixa")):
            code, rotulo, detalhe, tipo, _ = m.decidir(info)
        self.assertEqual((code, rotulo, tipo), (5, "NAO ENVIE", "shell"))
        self.assertIn("recrie o pane", detalhe)
        with mock.patch.object(core.subprocess, "run",
                               return_value=mock.Mock(returncode=5, stdout="NAO ENVIE: pane virou shell", stderr="")):
            with self.assertRaises(core.DeliveryRefusedError) as ctx:
                core.require_can_send("x", expected_pane="w1:p1")
        self.assertEqual(ctx.exception.code, 5)
