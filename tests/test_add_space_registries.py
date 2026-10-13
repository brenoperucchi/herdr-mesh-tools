"""herdr-add-space: todo space entra no inventario; so entra no Guardian com --guardian.

Tudo em diretorio temporario (HERDR_INVENTORY_DIR / HERDR_GUARDIAN_FASES): os arquivos
reais de ~/Devs/inventory e ~/Devs/claude-bridge nunca sao tocados.
"""
import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")

CATEGORIAS = """{
  "_doc": "teste",
  "categories": ["trading", "SaaS", "agentes"],
  "projects": {
    "~/Devs/a": ["trading"],
    "~/Devs/b": ["SaaS", "agentes"]
  },
  "prefixes": {
    "~/Devs/Khronos": ["SaaS"]
  }
}
"""
PROJETOS = """{
  "_doc": "teste",
  "~/Devs/a": "Projeto A."
}
"""
FASES = """{
  "_nota": "teste",
  "foco": [
    "mfc-exec",
    "acervo-exec"
  ],
  "projetos": {
    "acervo-exec": {"fase": 3}
  }
}
"""


class RegistrosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(d, "inv", "config"))
        self.cat = os.path.join(d, "inv", "config", "categories.json")
        self.proj = os.path.join(d, "inv", "config", "projects.json")
        self.fases = os.path.join(d, "visao-fases.json")
        for p, t in ((self.cat, CATEGORIAS), (self.proj, PROJETOS), (self.fases, FASES)):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(t)
        os.environ["HERDR_INVENTORY_DIR"] = os.path.join(d, "inv")
        os.environ["HERDR_GUARDIAN_FASES"] = self.fases
        loader = SourceFileLoader("herdr_add_space_registros_test", os.path.join(BIN_DIR, "herdr-add-space"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.a = importlib.util.module_from_spec(spec)
        loader.exec_module(self.a)
        self.cwd = self.a.HOME + "/Devs/novo"

    def tearDown(self):
        os.environ.pop("HERDR_INVENTORY_DIR", None)
        os.environ.pop("HERDR_GUARDIAN_FASES", None)
        self.tmp.cleanup()

    def ler(self, p):
        with open(p, encoding="utf-8") as fh:
            return fh.read()

    def test_categorias_vem_do_inventario(self):
        self.assertEqual(self.a.categorias_validas(), ["trading", "SaaS", "agentes"])

    def test_inventario_acrescenta_so_a_linha_nova(self):
        antes = self.ler(self.cat)
        self.a.registrar_inventario(self.cwd, "agentes", "")
        depois = self.ler(self.cat)
        self.assertEqual(json.loads(depois)["projects"]["~/Devs/novo"], ["agentes"])
        # o resto do arquivo (comentario, prefixes, formato de uma linha por projeto) fica igual
        self.assertEqual(json.loads(depois)["prefixes"], json.loads(antes)["prefixes"])
        self.assertIn('    "~/Devs/b": ["SaaS", "agentes"],\n    "~/Devs/novo": ["agentes"]\n  },', depois)
        self.assertEqual(self.ler(self.proj), PROJETOS)  # sem descricao, nao escreve

    def test_inventario_com_descricao_escreve_projects_json(self):
        self.a.registrar_inventario(self.cwd, "SaaS", "App de teste.")
        self.assertEqual(json.loads(self.ler(self.proj))["~/Devs/novo"], "App de teste.")
        self.assertEqual(json.loads(self.ler(self.proj))["~/Devs/a"], "Projeto A.")

    def test_inventario_idempotente(self):
        self.a.registrar_inventario(self.cwd, "SaaS", "x")
        uma = (self.ler(self.cat), self.ler(self.proj))
        self.a.registrar_inventario(self.cwd, "agentes", "y")
        self.assertEqual((self.ler(self.cat), self.ler(self.proj)), uma)

    def test_sem_guardian_nao_mexe_no_foco(self):
        self.a.registrar_guardian("novo-exec", False)
        self.assertEqual(self.ler(self.fases), FASES)

    def test_com_guardian_entra_no_foco_sem_mexer_no_resto(self):
        self.a.registrar_guardian("novo-exec", True)
        d = json.loads(self.ler(self.fases))
        self.assertEqual(d["foco"], ["mfc-exec", "acervo-exec", "novo-exec"])
        self.assertEqual(d["projetos"], {"acervo-exec": {"fase": 3}})
        self.assertIn('    "acervo-exec",\n    "novo-exec"\n  ],', self.ler(self.fases))

    def test_com_guardian_idempotente(self):
        self.a.registrar_guardian("novo-exec", True)
        uma = self.ler(self.fases)
        self.a.registrar_guardian("novo-exec", True)
        self.assertEqual(self.ler(self.fases), uma)

    def test_sem_guardian_nunca_remove_quem_ja_estava(self):
        self.a.registrar_guardian("mfc-exec", False)
        self.assertEqual(self.ler(self.fases), FASES)

    def test_foco_vazio(self):
        with open(self.fases, "w", encoding="utf-8") as fh:
            fh.write('{\n  "foco": [\n  ],\n  "projetos": {}\n}\n')
        self.a.registrar_guardian("novo-exec", True)
        self.assertEqual(json.loads(self.ler(self.fases))["foco"], ["novo-exec"])

    def test_nao_deixa_arquivo_temporario(self):
        self.a.registrar_inventario(self.cwd, "SaaS", "x")
        self.a.registrar_guardian("novo-exec", True)
        sobras = [f for d, _, fs in os.walk(self.tmp.name) for f in fs if f.endswith(".herdr-tmp")]
        self.assertEqual(sobras, [])


if __name__ == "__main__":
    unittest.main()
