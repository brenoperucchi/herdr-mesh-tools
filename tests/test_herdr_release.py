"""herdr-release: desliga revisores/scout DEPOIS da rodada, so quando e' seguro.

Quem roda da o aceite (exec ou Breno); o comando confere sozinho o que de fato da errado:
agent trabalhando, rascunho humano, e rodada sem veredito. REQUEST_CHANGES so avisa.
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

import _herdr_dispatch as core  # noqa: E402


def _load():
    loader = SourceFileLoader("herdr_release_test", os.path.join(BIN_DIR, "herdr-release"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _agent(name, cwd, kind="codex", status="idle"):
    return {"name": name, "pane_id": f"w1:{name}", "agent": kind, "agent_status": status, "cwd": cwd,
            "agent_session": {"value": f"sess-{name}"}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.r = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def resultado(self, tipo, slug, rodada, agent, nome, texto, ts=None):
        d = os.path.join(self.root, ".herdr", tipo, f"{slug}-{rodada}", agent)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, nome)
        with open(p, "w", encoding="utf-8") as f:
            f.write(texto)
        if ts:
            os.utime(p, (ts, ts)); os.utime(d, (ts, ts))
        return p

    def pasta_sem_resultado(self, tipo, slug, rodada, agent):
        d = os.path.join(self.root, ".herdr", tipo, f"{slug}-{rodada}", agent)
        os.makedirs(d, exist_ok=True)


class ResultStateTests(Fixture):
    def test_sem_rodada(self):
        self.assertEqual(self.r.result_state(self.root, "zed", "zed-rev-1")[0], "sem_rodada")

    def test_veredito_presente_e_ok(self):
        self.resultado("review", "zed", 1, "zed-rev-1", "verdict.md", "# APPROVE\n")
        self.assertEqual(self.r.result_state(self.root, "zed", "zed-rev-1")[::2], ("ok", False))

    def test_answer_do_ask_tambem_conta(self):
        self.resultado("ask", "zed", 1, "zed-rev-1", "answer.md", "resposta")
        self.assertEqual(self.r.result_state(self.root, "zed", "zed-rev-1")[0], "ok")

    def test_pasta_sem_arquivo_ou_vazio_e_pendente(self):
        self.pasta_sem_resultado("review", "zed", 1, "zed-rev-1")
        self.assertEqual(self.r.result_state(self.root, "zed", "zed-rev-1")[0], "pendente")
        self.resultado("review", "zed", 2, "zed-rev-1", "verdict.md", "")
        self.assertEqual(self.r.result_state(self.root, "zed", "zed-rev-1")[0], "pendente")

    def test_olha_a_rodada_mais_recente_nao_a_antiga(self):
        agora = time.time()
        self.resultado("review", "zed", 1, "zed-rev-1", "verdict.md", "# APPROVE\n", ts=agora - 1000)
        self.pasta_sem_resultado("ask", "zed", 1, "zed-rev-1")   # mais nova, sem resposta
        self.assertEqual(self.r.result_state(self.root, "zed", "zed-rev-1")[0], "pendente")

    def test_request_changes_marca_achados_abertos(self):
        self.resultado("review", "zed", 1, "zed-rev-1", "verdict.md", "# REQUEST_CHANGES\n- P1")
        self.assertTrue(self.r.result_state(self.root, "zed", "zed-rev-1")[2])


class AvaliarTests(unittest.TestCase):
    def setUp(self):
        self.r = _load()
        self.ok = ("ok", "/x/verdict.md", False)
        self.vazia = ("vazia", "", "")

    def test_tudo_certo_nao_recusa(self):
        self.assertEqual(self.r.avaliar("idle", self.vazia, self.ok), ([], []))

    def test_working_blocked_e_desconhecido_recusam(self):
        for st in ("working", "blocked", "unknown"):
            self.assertTrue(self.r.avaliar(st, self.vazia, self.ok)[0], st)

    def test_rascunho_humano_dialogo_e_leitura_falha_recusam(self):
        for tipo in ("humano", "dialogo", "indeterminado"):
            self.assertTrue(self.r.avaliar("idle", (tipo, "texto", ""), self.ok)[0], tipo)

    def test_sugestao_e_placeholder_nao_recusam(self):
        for tipo in ("sugestao", "placeholder", "vazia"):
            self.assertEqual(self.r.avaliar("idle", (tipo, "t", ""), self.ok)[0], [], tipo)

    def test_rodada_sem_veredito_recusa(self):
        motivos, _ = self.r.avaliar("idle", self.vazia, ("pendente", "/x", False))
        self.assertTrue(any("veredito" in m for m in motivos))

    def test_sem_rodada_so_avisa(self):
        self.assertEqual(self.r.avaliar("idle", self.vazia, ("sem_rodada", None, False))[0], [])
        self.assertTrue(self.r.avaliar("idle", self.vazia, ("sem_rodada", None, False))[1])

    def test_request_changes_so_avisa_nao_bloqueia(self):
        motivos, avisos = self.r.avaliar("idle", self.vazia, ("ok", "/x", True))
        self.assertEqual(motivos, [])
        self.assertTrue(any("REQUEST_CHANGES" in a for a in avisos))


class LiberarTests(Fixture):
    def agentes(self):
        return [_agent("zed-exec", self.root, "claude"), _agent("zed-rev-1", self.root),
                _agent("zed-rev-2", self.root, "claude"), _agent("zed-scout", self.root)]

    def roda(self, composicao=("vazia", "", ""), status="idle", **kw):
        fechados = []

        def fake_run(args, **k):
            if "close" in args:
                fechados.append(args[-1])
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(core, "project_root", return_value=self.root), \
             mock.patch.object(core, "classify_composition", return_value=composicao), \
             mock.patch.object(core, "effective_status", return_value=status), \
             mock.patch.object(self.r.subprocess, "run", side_effect=fake_run):
            res = self.r.liberar("zed", self.agentes(), kw.pop("only", "all"), kw.pop("force", False),
                                 kw.pop("dry_run", False))
        return res, fechados

    def prepara_veredito(self):
        for n in ("zed-rev-1", "zed-rev-2", "zed-scout"):
            self.resultado("review", "zed", 1, n, "verdict.md", "# APPROVE\n")

    def test_nunca_toca_no_exec(self):
        self.prepara_veredito()
        res, fechados = self.roda()
        self.assertNotIn("w1:zed-exec", fechados)
        self.assertEqual(sorted(i["name"] for i in res), ["zed-rev-1", "zed-rev-2", "zed-scout"])

    def test_fecha_os_tres_quando_tudo_certo_e_registra_a_sessao(self):
        self.prepara_veredito()
        res, fechados = self.roda()
        self.assertEqual(sorted(fechados), ["w1:zed-rev-1", "w1:zed-rev-2", "w1:zed-scout"])
        self.assertTrue(all(i["acao"] == "fechado" for i in res))
        log = os.path.join(os.environ["HERDR_MESH_STATE_DIR"], "released.jsonl")
        regs = [json.loads(l) for l in open(log, encoding="utf-8")]
        self.assertEqual({r["session"] for r in regs}, {"sess-zed-rev-1", "sess-zed-rev-2", "sess-zed-scout"})

    def test_only_scout_fecha_so_o_scout(self):
        self.prepara_veredito()
        _, fechados = self.roda(only="scout")
        self.assertEqual(fechados, ["w1:zed-scout"])

    def test_recusa_sem_veredito_e_nao_fecha_nem_registra(self):
        self.pasta_sem_resultado("review", "zed", 1, "zed-rev-1")
        res, fechados = self.roda(only="reviewers")
        # so o rev-1 tem rodada pendente; o rev-2 nao tem rodada nenhuma (so avisa) e fecha
        self.assertNotIn("w1:zed-rev-1", fechados)
        rev1 = next(i for i in res if i["name"] == "zed-rev-1")
        self.assertEqual(rev1["acao"], "recusado")
        log = os.path.join(os.environ["HERDR_MESH_STATE_DIR"], "released.jsonl")
        sessoes = {json.loads(l)["session"] for l in open(log, encoding="utf-8")} if os.path.exists(log) else set()
        self.assertNotIn("sess-zed-rev-1", sessoes)

    def test_recusa_rascunho_humano(self):
        self.prepara_veredito()
        res, fechados = self.roda(composicao=("humano", "meu texto", ""))
        self.assertEqual(fechados, [])
        self.assertTrue(all(i["acao"] == "recusado" for i in res))

    def test_force_fecha_mesmo_com_recusa(self):
        self.pasta_sem_resultado("review", "zed", 1, "zed-rev-1")
        res, fechados = self.roda(only="reviewers", force=True, status="working")
        self.assertIn("w1:zed-rev-1", fechados)

    def test_dry_run_nao_fecha_nada(self):
        self.prepara_veredito()
        res, fechados = self.roda(dry_run=True)
        self.assertEqual(fechados, [])
        self.assertTrue(all(i["acao"].startswith("fecharia") for i in res))

    def test_achados_abertos_avisam_mas_fecham(self):
        self.resultado("review", "zed", 1, "zed-rev-1", "verdict.md", "REQUEST_CHANGES")
        res, fechados = self.roda(only="reviewers")
        item = next(i for i in res if i["name"] == "zed-rev-1")
        self.assertEqual(item["acao"], "fechado")
        self.assertTrue(item["avisos"])


class TargetsTests(unittest.TestCase):
    def test_slug_nao_pega_agent_de_outro_space(self):
        r = _load()
        ags = [{"name": "zed-rev-1"}, {"name": "zedx-rev-1"}, {"name": "zed-exec"}, {"name": "zed-scout"}]
        self.assertEqual([a["name"] for a in r.targets("zed", ags)], ["zed-rev-1", "zed-scout"])

    def test_entry_for_slug_usa_slug_explicito_ou_label(self):
        r = _load()
        spaces = [("MFC", "/m", None, None), ("llm-gateway", "/l", None, None, "llm")]
        self.assertEqual(r.entry_for_slug(spaces, "mfc")[0], "MFC")
        self.assertEqual(r.entry_for_slug(spaces, "llm")[0], "llm-gateway")
        self.assertIsNone(r.entry_for_slug(spaces, "nada"))


if __name__ == "__main__":
    unittest.main()
