"""herdr-reviewers-sweep: so revisor idle entra na limpeza; o resto fica como esta."""
import importlib.util
import os
import sys
import unittest
from importlib.machinery import SourceFileLoader

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)


def _load():
    loader = SourceFileLoader("herdr_reviewers_sweep_test", os.path.join(BIN_DIR, "herdr-reviewers-sweep"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _a(name, status):
    return {"name": name, "agent_status": status}


class Selecionar(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def sel(self, agents, groups=None):
        return self.m.selecionar(agents, lambda a: a["agent_status"], groups)

    def test_so_idle_entra(self):
        por_slug, pulados = self.sel([_a("mfc-rev-1", "idle"), _a("mfc-rev-2", "working"),
                                      _a("seed-rev-1", "done"), _a("seed-rev-2", "blocked")])
        self.assertEqual({s: [a["name"] for a in v] for s, v in por_slug.items()}, {"mfc": ["mfc-rev-1"]})
        self.assertEqual({n for n, _ in pulados}, {"mfc-rev-2", "seed-rev-1", "seed-rev-2"})

    def test_exec_scout_e_outros_nunca_entram(self):
        por_slug, pulados = self.sel([_a("mfc-exec", "idle"), _a("mfc-scout", "idle"),
                                      _a("revisor-solto", "idle"), _a("khr-dre-rev-2", "idle")])
        self.assertEqual(list(por_slug), ["khr-dre"])
        self.assertEqual(pulados, [])

    def test_sufixo_rev_sem_numero(self):
        por_slug, _ = self.sel([_a("herdr-rev", "idle")])
        self.assertEqual(list(por_slug), ["herdr"])

    def test_grupo_so_fecha_com_todos_idle(self):
        grupos = {"caio": {"caio-rev-1", "caio-rev-2"}}
        por_slug, pulados = self.sel([_a("caio-rev-1", "idle"), _a("caio-rev-2", "working")], grupos)
        self.assertEqual(por_slug, {})
        self.assertIn("caio-rev-1", dict(pulados))
        por_slug, _ = self.sel([_a("caio-rev-1", "idle"), _a("caio-rev-2", "idle")], grupos)
        self.assertEqual(sorted(a["name"] for a in por_slug["caio"]), ["caio-rev-1", "caio-rev-2"])


if __name__ == "__main__":
    unittest.main()
