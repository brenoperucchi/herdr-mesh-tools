"""Sugestao fantasma (autosuggest) do Claude Code vs rascunho humano.

O Claude Code desenha a sugestao na caixa em SGR 2 (faint); texto digitado nao e'
faint. A checagem de composicao so libera o envio quando TODO caractere visivel do
conteudo da caixa esta em faint. Recorrencia de 2026-10-01: com o `tui default`, os
panes tem scrollback e ficam rolados; a leitura `visible` (viewport do usuario) nao
mostrava a caixa e a checagem desistia, tratando a sugestao como rascunho humano.
"""
import json
import os
import sys
import unittest
from unittest import mock

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

import _herdr_dispatch as core  # noqa: E402

ESC = "\x1b"
SUGESTAO = f"❯ {ESC}[0m{ESC}[2mpode começar pelos rascunhos das issues{ESC}[0m"
HUMANO = f"❯ {ESC}[0mpode começar pelos rascunhos das issues"
PARCIAL = f"❯ {ESC}[0m{ESC}[2mpode começar pelos{ESC}[0m rascunhos das issues"
TRUECOLOR = f"❯ {ESC}[0m{ESC}[38;2;177;185;249mpode começar pelos rascunhos das issues{ESC}[0m"
COMPOSE = "pode começar pelos rascunhos das issues"


def _run(offset, linhas_visible="", linhas_recent="", falha_recent=False):
    """subprocess.run falso: `pane get` devolve o offset; `pane read` devolve o ANSI
    da fonte pedida, e guarda qual fonte foi usada."""
    usadas = []

    def fake(args, **kw):
        r = mock.Mock(returncode=0, stderr="")
        if "get" in args:
            r.stdout = json.dumps({"result": {"pane": {"scroll": {"offset_from_bottom": offset}}}})
        elif "read" in args:
            fonte = args[args.index("--source") + 1]
            usadas.append(fonte)
            if fonte == "recent" and falha_recent:
                r.returncode, r.stdout = 1, ""
            else:
                r.stdout = linhas_visible if fonte == "visible" else linhas_recent
        return r
    return fake, usadas


class FaintSpansTests(unittest.TestCase):
    def test_texto_em_faint_e_marcado(self):
        limpo, marcas = core._faint_spans(f"{ESC}[2mabc{ESC}[0m")
        self.assertEqual((limpo, marcas), ("abc", [True, True, True]))

    def test_truecolor_nao_e_lido_como_faint(self):
        # `ESC[38;2;r;g;bm` tem um `2` que NAO e' faint: foi o falso positivo que
        # liberava entrada humana (um regex ingenuo casava 3 de 4 casos).
        _, marcas = core._faint_spans(f"{ESC}[38;2;177;185;249mabc{ESC}[0m")
        self.assertEqual(marcas, [False, False, False])

    def test_256_cores_e_reset_parcial_nao_sao_faint(self):
        _, m1 = core._faint_spans(f"{ESC}[38;5;242mabc")
        _, m2 = core._faint_spans(f"{ESC}[2ma{ESC}[22mb")
        self.assertEqual(m1, [False, False, False])
        self.assertEqual(m2, [True, False])


class ComposeIsRenderedChromeTests(unittest.TestCase):
    def check(self, offset, visible="", recent="", falha_recent=False):
        fake, usadas = _run(offset, visible, recent, falha_recent)
        with mock.patch.object(core.subprocess, "run", side_effect=fake):
            return core._compose_is_rendered_chrome("w9:p9", COMPOSE), usadas

    def test_sugestao_faint_no_viewport_normal_passa(self):
        ok, usadas = self.check(0, visible=SUGESTAO)
        self.assertTrue(ok)
        self.assertEqual(usadas, ["visible"])

    def test_texto_humano_nao_faint_nao_passa(self):
        self.assertFalse(self.check(0, visible=HUMANO)[0])

    def test_faint_parcial_nao_passa(self):
        self.assertFalse(self.check(0, visible=PARCIAL)[0])

    def test_cor_truecolor_nao_passa_como_chrome(self):
        self.assertFalse(self.check(0, visible=TRUECOLOR)[0])

    def test_pane_rolado_usa_recent_e_a_sugestao_passa(self):
        # o caso da recorrencia: viewport rolado, caixa fora do `visible`
        ok, usadas = self.check(7, visible="historico antigo sem caixa", recent=SUGESTAO)
        self.assertTrue(ok)
        self.assertEqual(usadas, ["recent"])

    def test_pane_rolado_com_texto_humano_continua_barrando(self):
        self.assertFalse(self.check(7, recent=HUMANO)[0])

    def test_pane_rolado_e_leitura_recent_falha_e_conservador(self):
        self.assertFalse(self.check(7, recent=SUGESTAO, falha_recent=True)[0])

    def test_pane_rolado_sem_a_caixa_nas_ultimas_linhas_e_conservador(self):
        self.assertFalse(self.check(7, recent="so historico, sem marcador")[0])

    def test_sem_info_de_scroll_usa_recent(self):
        fake, usadas = _run(None, linhas_recent=SUGESTAO)
        # offset ausente: trata como rolado (nao assume viewport no fundo)
        def fake2(args, **kw):
            r = fake(args, **kw)
            if "get" in args:
                r.stdout = json.dumps({"result": {"pane": {}}})
            return r
        with mock.patch.object(core.subprocess, "run", side_effect=fake2):
            self.assertTrue(core._compose_is_rendered_chrome("w9:p9", COMPOSE))
        self.assertEqual(usadas, ["recent"])


class DiagnosticoTests(unittest.TestCase):
    """O bloqueio precisa dizer POR QUE nao reconheceu a sugestao e deixar rastro."""

    def verdict(self, offset, visible="", recent=""):
        fake, _ = _run(offset, visible, recent)
        with mock.patch.object(core.subprocess, "run", side_effect=fake):
            return core._compose_chrome_verdict("w9:p9", COMPOSE)

    def test_sugestao_inteira_em_faint_explica_o_porque(self):
        ok, motivo = self.verdict(0, visible=SUGESTAO)
        self.assertTrue(ok)
        self.assertIn("faint", motivo)

    def test_faint_parcial_diz_quais_caracteres_nao_eram_faint(self):
        ok, motivo = self.verdict(0, visible=PARCIAL)
        self.assertFalse(ok)
        self.assertIn("NAO estao em faint", motivo)
        self.assertIn("rascunhos", motivo)

    def test_texto_diferente_na_tela_mostra_os_dois_textos(self):
        outra = f"❯ {ESC}[0m{ESC}[2moutra coisa{ESC}[0m"
        ok, motivo = self.verdict(0, visible=outra)
        self.assertFalse(ok)
        self.assertIn("difere do detection", motivo)
        self.assertIn("outra coisa", motivo)

    def test_sem_linha_da_caixa_diz_que_nao_localizou(self):
        ok, motivo = self.verdict(0, visible="sem marcador")
        self.assertFalse(ok)
        self.assertIn("nao localizada", motivo)

    def test_pane_rolado_aparece_no_motivo(self):
        ok, motivo = self.verdict(7, recent=HUMANO)
        self.assertFalse(ok)
        self.assertIn("[recent]", motivo)

    def test_bloqueio_vai_para_o_log_e_a_mensagem_traz_o_diag(self):
        import tempfile
        detection = f"● resposta\n❯ {COMPOSE}\n⏵⏵ bypass"
        fake, _ = _run(0, linhas_visible=HUMANO)

        def fake_run(args, **kw):
            if "read" in args and "detection" in args:
                return mock.Mock(returncode=0, stdout=detection, stderr="")
            return fake(args, **kw)

        with tempfile.TemporaryDirectory() as home, \
             mock.patch.dict(os.environ, {"HERDR_MESH_STATE_DIR": home}), \
             mock.patch.object(core.subprocess, "run", side_effect=fake_run):
            busy, why = core.pane_looks_busy_with_human_input("w9:p9")
            log = os.path.join(home, "compose-guard.log")
            conteudo = open(log, encoding="utf-8").read()
        self.assertTrue(busy)
        self.assertIn("[diag:", why)
        self.assertIn("w9:p9", conteudo)
        self.assertIn("NAO estao em faint", conteudo)


if __name__ == "__main__":
    unittest.main()
