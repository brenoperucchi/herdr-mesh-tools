"""Mecânica de arquivos do herdr-rename-space (_herdr_rename.py), numa HOME falsa."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
sys.path.insert(0, BIN)
import _herdr_rename as rn  # noqa: E402

CWD_LINHA = '{"type":"user","cwd":"%s","sessionId":"s"}\n'


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.velho = f"{self.home}/Devs/claude-bridge"
        self.novo = f"{self.home}/Devs/guardian"
        self.rt = f"{self.home}/.claude-bridge"          # runtime: so' o historico e' absorvido
        self.bkp = f"{self.home}/bkp"
        self.mapa = rn.Mapa([(self.velho, self.novo), (self.rt, self.novo)])

    def escreve(self, rel, texto):
        p = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(texto)
        return p

    def projeto_claude(self, cwd, sid="s1", extra=""):
        return self.escreve(f".claude/projects/{rn.codifica(cwd)}/{sid}.jsonl", CWD_LINHA % cwd + extra)


class MapaTests(Base):
    def test_so_prefixo_de_caminho_inteiro(self):
        self.assertEqual(self.mapa.aplica(self.velho), self.novo)
        self.assertEqual(self.mapa.aplica(self.velho + "/.herdr/review"), self.novo + "/.herdr/review")
        self.assertIsNone(self.mapa.aplica(self.velho + "-x"))
        self.assertIsNone(self.mapa.aplica(self.velho + "x/y"))

    def test_prefixo_mais_longo_vence(self):
        m = rn.Mapa([("/a/gw", "/a/gw"), ("/a/gw/.fc", "/a/gw"), ("/a/x", "/a/y")])
        self.assertEqual(m.aplica("/a/gw/.fc"), "/a/gw")
        self.assertEqual(m.aplica("/a/gw/.fc/z"), "/a/gw/z")
        self.assertIsNone(m.aplica("/a/gw/outro"))   # par identidade e' ignorado

    def test_reescreve_so_campos_cwd_e_project(self):
        t, n = self.mapa.reescreve_texto(f'{{"cwd":"{self.velho}/sub","texto":"{self.velho}"}}')
        self.assertEqual(n, 1)
        self.assertIn(f'"cwd":"{self.novo}/sub"', t)
        self.assertIn(f'"texto":"{self.velho}"', t)      # conteudo historico nao e' tocado


class ClaudeTests(Base):
    def test_move_pasta_reescreve_cwd_e_preserva_mtime(self):
        p = self.projeto_claude(self.velho, extra=CWD_LINHA % (self.velho + "/.herdr/review"))
        os.utime(p, (1_700_000_000, 1_700_000_000))
        self.projeto_claude(self.velho + "/.herdr/review", sid="s2")
        decoy = self.projeto_claude(self.velho + "-x", sid="d")           # outro projeto, nome parecido
        rel = rn.migra_historico_claude(self.home, self.mapa, self.bkp, True)
        destino = f"{self.home}/.claude/projects/{rn.codifica(self.novo)}/s1.jsonl"
        self.assertTrue(os.path.exists(destino))
        self.assertFalse(os.path.exists(p))
        txt = open(destino).read()
        self.assertIn(self.novo, txt)
        self.assertNotIn(self.velho, txt)
        self.assertEqual(int(os.stat(destino).st_mtime), 1_700_000_000)
        sub = f"{self.home}/.claude/projects/{rn.codifica(self.novo + '/.herdr/review')}/s2.jsonl"
        self.assertTrue(os.path.exists(sub))
        self.assertTrue(os.path.exists(decoy))                           # intocado
        self.assertEqual(len(rel["pastas"]), 2)

    def test_absorve_historico_do_runtime_sem_sobrescrever(self):
        self.projeto_claude(self.velho, sid="mesmo", extra="A\n")
        self.projeto_claude(self.rt, sid="mesmo", extra="B\n")           # mesma id: conflito
        self.projeto_claude(self.rt, sid="so-no-runtime")
        rel = rn.migra_historico_claude(self.home, self.mapa, self.bkp, True)
        d = f"{self.home}/.claude/projects/{rn.codifica(self.novo)}"
        self.assertTrue(os.path.exists(f"{d}/so-no-runtime.jsonl"))
        self.assertIn("A\n", open(f"{d}/mesmo.jsonl").read())            # o que ja estava ganhou
        self.assertEqual(len(rel["conflitos"]), 1)
        # o original em conflito continua no lugar de origem
        self.assertTrue(os.path.exists(f"{self.home}/.claude/projects/{rn.codifica(self.rt)}/mesmo.jsonl"))

    def test_dry_run_nao_escreve(self):
        p = self.projeto_claude(self.velho)
        antes = open(p).read()
        rel = rn.migra_historico_claude(self.home, self.mapa, self.bkp, False)
        self.assertEqual(open(p).read(), antes)
        self.assertEqual(rel["cwd_trocados"], 1)
        self.assertFalse(os.path.exists(self.bkp))

    def test_history_jsonl_e_claude_json(self):
        self.escreve(".claude/history.jsonl", json.dumps({"project": self.velho, "display": "x"}) + "\n"
                     + json.dumps({"project": "/outro", "display": "y"}) + "\n")
        self.escreve(".claude.json", json.dumps(
            {"projects": {self.velho: {"hasTrustDialogAccepted": True}, self.rt: {"k": 1}, "/outro": {}}}, indent=2))
        rel = rn.migra_historico_claude(self.home, self.mapa, self.bkp, True)
        self.assertEqual(rel["history_trocas"], 1)
        cj = rn.migra_claude_json(self.home, self.mapa, {self.velho}, self.bkp, True)
        dados = json.load(open(f"{self.home}/.claude.json"))["projects"]
        self.assertTrue(dados[self.novo]["hasTrustDialogAccepted"])
        self.assertNotIn(self.velho, dados)
        self.assertIn(self.rt, dados)            # a pasta do runtime continua existindo: a chave fica
        self.assertIn("/outro", dados)
        self.assertEqual(len(cj["chaves"]), 2)
        self.assertTrue(os.path.exists(f"{self.bkp}/.claude.json"))

    def test_claude_json_principal_ganha_da_absorvida(self):
        # regressao 2026-10-14: a chave absorvida (confianca False) veio antes no arquivo e sobrescreveu a
        # principal (True), e o Claude voltou pedindo confianca na pasta nova
        self.escreve(".claude.json", json.dumps({"projects": {
            self.rt: {"hasTrustDialogAccepted": False}, self.velho: {"hasTrustDialogAccepted": True}}}, indent=2))
        rn.migra_claude_json(self.home, self.mapa, {self.velho}, self.bkp, True)
        d = json.load(open(f"{self.home}/.claude.json"))["projects"]
        self.assertTrue(d[self.novo]["hasTrustDialogAccepted"])

    def test_claude_json_destino_existente_vence(self):
        self.escreve(".claude.json", json.dumps(
            {"projects": {self.velho: {"a": 1, "t": "velho"}, self.novo: {"t": "novo"}}}, indent=2))
        rn.migra_claude_json(self.home, self.mapa, {self.velho}, self.bkp, True)
        d = json.load(open(f"{self.home}/.claude.json"))["projects"]
        self.assertEqual(d[self.novo], {"a": 1, "t": "novo"})


class CodexTests(Base):
    def banco(self):
        p = f"{self.home}/.codex/state_5.sqlite"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        db = sqlite3.connect(p)
        db.execute("create table threads (id text primary key, rollout_path text, cwd text, updated_at integer)")
        db.execute("create table project_roots (project_id text, position integer, path text)")
        return db

    def test_threads_rollouts_e_config(self):
        roll = self.escreve("r1.jsonl", CWD_LINHA % self.rt + CWD_LINHA % (self.velho + "/x"))
        db = self.banco()
        db.executemany("insert into threads values (?,?,?,?)", [
            ("a", roll, self.rt, 5), ("b", "", self.velho + "-x", 5), ("c", "", self.velho + "/sub", 5)])
        db.execute("insert into project_roots values ('p',0,?)", (self.velho,))
        db.commit()
        db.close()
        cfg = self.escreve(".codex/config.toml",
                           f'[projects."{self.velho}"]\ntrust_level = "trusted"\n\n[projects."{self.rt}"]\n'
                           f'trust_level = "trusted"\n\n[projects."/outro"]\ntrust_level = "x"\n')
        rel = rn.migra_codex(self.home, self.mapa, self.bkp, True, renomear_config={self.velho})
        db = sqlite3.connect(f"{self.home}/.codex/state_5.sqlite")
        cwds = dict(db.execute("select id, cwd from threads"))
        db.close()
        self.assertEqual(cwds, {"a": self.novo, "b": self.velho + "-x", "c": self.novo + "/sub"})
        self.assertEqual(rel["threads"], 2)
        txt = open(roll).read()
        self.assertNotIn(self.rt, txt)
        self.assertIn(self.novo + "/x", txt)
        cfg_txt = open(cfg).read()
        self.assertEqual(cfg_txt.count(f'[projects."{self.novo}"]'), 1)
        self.assertIn(f'[projects."{self.rt}"]', cfg_txt)             # a pasta de runtime continua com a confianca dela
        self.assertIn('[projects."/outro"]', cfg_txt)
        self.assertTrue(os.path.exists(f"{self.bkp}/state_5.sqlite"))

    def test_dry_run_do_codex_nao_muda_nada(self):
        db = self.banco()
        db.execute("insert into threads values ('a','',?,1)", (self.rt,))
        db.commit()
        db.close()
        rel = rn.migra_codex(self.home, self.mapa, self.bkp, False)
        self.assertEqual(rel["threads"], 1)
        db = sqlite3.connect(f"{self.home}/.codex/state_5.sqlite")
        self.assertEqual(db.execute("select cwd from threads").fetchone()[0], self.rt)
        db.close()


class TextoTests(Base):
    def test_substitui_so_caminho_inteiro(self):
        t, n = rn.substitui_caminho(f"WorkingDirectory={self.velho}\nExec={self.velho}/bin\nX={self.velho}-x", self.velho, self.novo)
        self.assertEqual(n, 2)
        self.assertIn(f"{self.velho}-x", t)

    def test_units_e_symlinks(self):
        u = self.escreve(".config/systemd/user/guardian-painel.service", f"[Service]\nWorkingDirectory={self.velho}\n")
        self.escreve(".config/systemd/user/outra.service", "[Service]\nWorkingDirectory=/x\n")
        achadas = rn.units_afetadas(self.home, self.velho, self.novo, self.bkp, True)
        self.assertEqual([a for a, _ in achadas], ["guardian-painel.service"])
        self.assertIn(self.novo, open(u).read())
        q = self.escreve(".config/containers/systemd/cic-browser.container",
                         f"[Container]\nVolume={self.velho}/extension:/extension:ro\n")
        self.assertEqual(rn.units_afetadas(self.home, self.velho, self.novo, self.bkp, True)[0][0],
                         "cic-browser.container")
        self.assertIn(self.novo + "/extension", open(q).read())
        os.makedirs(f"{self.home}/.local/bin")
        os.symlink(f"{self.velho}/bin/x", f"{self.home}/.local/bin/x")
        achados = rn.symlinks_para(self.home, self.velho, self.novo, True)
        self.assertEqual(len(achados), 1)
        self.assertEqual(os.readlink(f"{self.home}/.local/bin/x"), f"{self.novo}/bin/x")


if __name__ == "__main__":
    unittest.main()


class NomesTests(Base):
    def test_nomes_de_agent_nas_units_so_como_token(self):
        u = self.escreve(".config/systemd/user/guardian-narrador.service",
                         "ExecStart=x --agents dre-exec,xdre-exec,cic-exec,dre-exec2 --y dre-exec\n")
        achadas = rn.units_com_nomes(self.home, [("dre-exec", "khr-dre-exec")], self.bkp, True)
        self.assertEqual(achadas, [("guardian-narrador.service", 2)])
        self.assertEqual(open(u).read(),
                         "ExecStart=x --agents khr-dre-exec,xdre-exec,cic-exec,dre-exec2 --y khr-dre-exec\n")


class SpacesBlocoTests(unittest.TestCase):
    """edita_spaces do CLI: label, exec/scout, slug e cwd do bloco certo da tabela SPACES."""

    TABELA = '''SPACES = [
    ("outro", f"{HOME}/Devs/outro", ("outro-exec", "codex", X),
        ("outro-scout", "codex", S), "outro", {"lean": True}),
    # comentario antigo
    ("claude-bridge", f"{HOME}/Devs/claude-bridge", ("claude-bridge-exec", "claude", EXEC_ARGS),
        ("claude-bridge-scout", "codex", SCOUT_ARGS), "claude-bridge", {"lean": True}),
    ("cic", f"{HOME}/Devs/cic", ("cic-exec", "claude", EXEC_ARGS), None, "cic", {"lean": True}),
]
'''

    def carrega(self, tabela):
        import types
        from importlib.machinery import SourceFileLoader
        mod = SourceFileLoader("herdr_rename_space_t", os.path.join(BIN, "herdr-rename-space")).load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        mod.BOOTSTRAP = os.path.join(self.tmp.name, "herdr-bootstrap")
        mod.HOME = "/home/u"
        open(mod.BOOTSTRAP, "w").write(tabela)
        return mod, types

    def test_troca_so_o_bloco_do_space(self):
        mod, types = self.carrega(self.TABELA)
        a = types.SimpleNamespace(de="claude-bridge", label="guardian", slug="guardian")
        f = {"slug_velho": "claude-bridge"}
        r = mod.edita_spaces(a, f, "/home/u/Devs/guardian", True, os.path.join(self.tmp.name, "bkp"))
        novo = open(mod.BOOTSTRAP).read()
        self.assertTrue(r["achou"] and r["muda"])
        self.assertIn('("guardian", f"{HOME}/Devs/guardian", ("guardian-exec", "claude", EXEC_ARGS),', novo)
        self.assertIn('("guardian-scout", "codex", SCOUT_ARGS), "guardian", {"lean": True}),', novo)
        self.assertIn('("outro", f"{HOME}/Devs/outro", ("outro-exec"', novo)      # vizinhos intactos
        self.assertIn('("cic", f"{HOME}/Devs/cic", ("cic-exec"', novo)
        self.assertNotIn("claude-bridge", novo)

    def test_label_inexistente_nao_escreve(self):
        mod, types = self.carrega(self.TABELA)
        a = types.SimpleNamespace(de="nao-existe", label="x", slug="x")
        r = mod.edita_spaces(a, {"slug_velho": "nao-existe"}, "/home/u/Devs/x", True, os.path.join(self.tmp.name, "b"))
        self.assertFalse(r["achou"])
        self.assertEqual(open(mod.BOOTSTRAP).read(), self.TABELA)

    def test_so_renomeia_o_cwd_inalterado(self):
        mod, types = self.carrega(self.TABELA)
        a = types.SimpleNamespace(de="cic", label="cic", slug="cic")
        r = mod.edita_spaces(a, {"slug_velho": "cic"}, "/home/u/Devs/CIC", True, os.path.join(self.tmp.name, "b"))
        self.assertIn('("cic", f"{HOME}/Devs/CIC", ("cic-exec"', open(mod.BOOTSTRAP).read())
