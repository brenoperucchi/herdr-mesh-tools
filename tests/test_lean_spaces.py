"""Spaces enxutos (`--lean`): so o exec sobe por padrao; revisores e scout ficam
declarados e sobem sob demanda (`herdr-bootstrap --with-reviewers`, que os
dispatchers usam em ensure_required_agents).

Motivo (Breno, 2026-10-03): 75 agents ocupavam ~51 GB dos 94 GB da maquina, 38 GB
deles em revisores e scouts parados.
"""
import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)


def _load(nome):
    loader = SourceFileLoader(nome.replace("-", "_") + "_lean_test", os.path.join(BIN_DIR, nome))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class EntryOptionTests(unittest.TestCase):
    def setUp(self):
        self.b = _load("herdr-bootstrap")

    def test_entrada_antiga_sem_opcoes_nao_e_enxuta(self):
        self.assertFalse(self.b.is_lean(("X", "/x", ("x-exec", "claude", []), None)))
        self.assertFalse(self.b.is_lean(("X", "/x", ("x-exec", "claude", []), None, "x")))

    def test_marca_lean_no_sexto_elemento(self):
        e = ("X", "/x", ("x-exec", "claude", []), ("x-scout", "codex", []), "x", {"lean": True})
        self.assertTrue(self.b.is_lean(e))

    def test_with_reviewers_ignora_o_lean(self):
        e = ("X", "/x", ("x-exec", "claude", []), ("x-scout", "codex", []), "x", {"lean": True})
        self.assertFalse(self.b.is_lean(e, with_reviewers=True))

    def test_sexto_elemento_que_nao_e_dict_e_ignorado(self):
        e = ("X", "/x", ("x-exec", "claude", []), None, "x", "lean")
        self.assertEqual(self.b.entry_opts(e), {})


class BootstrapPlanTests(unittest.TestCase):
    """Roda main() em --dry-run sobre um workspace existente com 1 pane."""

    def plano(self, argv, lean):
        b = _load("herdr-bootstrap")
        with tempfile.TemporaryDirectory() as cwd:
            entry = ("Zed", cwd, ("zed-exec", "claude", ["--model", "m"]),
                     ("zed-scout", "codex", ["-m", "m"]), "zed") + (({"lean": True},) if lean else ())

            def api(*args):
                if args[:2] == ("workspace", "list"):
                    return {"workspaces": [{"workspace_id": "wZ", "label": "Zed"}]}
                if args[:2] == ("pane", "list"):
                    return {"panes": [{"pane_id": "wZ:p1", "workspace_id": "wZ", "tab_id": "wZ:t1"}]}
                if args[:2] == ("agent", "list"):
                    return {"agents": []}
                return {}

            out = io.StringIO()
            with mock.patch.object(b, "SPACES", [entry]), mock.patch.object(b, "api", side_effect=api), \
                 mock.patch.object(b, "ensure_notify_watchers", lambda *a, **k: None), \
                 mock.patch.object(sys, "argv", ["herdr-bootstrap", "--dry-run", *argv]), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                b.main()
        return out.getvalue()

    def test_enxuto_so_pede_o_exec_e_nao_cria_scout(self):
        saida = self.plano(["--space", "Zed"], lean=True)
        self.assertIn("enxuto", saida)
        self.assertEqual(saida.count("iniciando"), 1)
        self.assertIn("iniciando claude como 'zed-exec'", saida)
        self.assertIsNone(re.search(r"^  split \(", saida, re.M), "nao deveria dividir pane")
        self.assertNotIn("scout precisa de tab", saida)
        self.assertNotIn("iniciando codex", saida)

    def test_prompt_do_exec_enxuto_diz_que_revisores_sobem_sob_demanda(self):
        saida = self.plano(["--space", "Zed"], lean=True)
        self.assertIn("Space ENXUTO", saida)
        self.assertIn("sobe o que\nfaltar sozinho", saida)

    def test_with_reviewers_e_with_scout_juntos_trazem_tudo(self):
        saida = self.plano(["--space", "Zed", "--with-reviewers", "--with-scout"], lean=True)
        self.assertNotIn("space Zed: enxuto", saida)
        self.assertNotIn("Space ENXUTO", saida)
        self.assertIn("precisa de 3 panes", saida)
        self.assertIn("scout precisa de tab", saida)

    def test_with_reviewers_sozinho_nao_liga_o_scout(self):
        saida = self.plano(["--space", "Zed", "--with-reviewers"], lean=True)
        self.assertIn("precisa de 3 panes", saida)
        self.assertNotIn("scout precisa de tab", saida)

    def test_with_scout_sozinho_liga_so_o_scout(self):
        saida = self.plano(["--space", "Zed", "--with-scout"], lean=True)
        self.assertIn("scout precisa de tab", saida)
        self.assertIsNone(re.search(r"^  split \(", saida, re.M))

    def test_space_normal_continua_igual(self):
        saida = self.plano(["--space", "Zed"], lean=False)
        self.assertIn("precisa de 3 panes", saida)
        self.assertIn("scout precisa de tab", saida)


class AddSpaceEntryTests(unittest.TestCase):
    def setUp(self):
        self.a = _load("herdr-add-space")

    def entry(self, **kw):
        return self.a.render_entry("Digest", "/home/brenoperucchi/Devs/digest", "digest", "claude", "codex",
                                   "teste", **kw)

    def test_lean_emite_slug_explicito_e_a_marca(self):
        e = self.entry(lean=True)
        self.assertIn('"digest", {"lean": True}', e)
        ns = {}
        exec("HOME='/home/brenoperucchi'\nEXEC_ARGS=[]\nSCOUT_ARGS=[]\nSCOUT_ARGS_CLAUDE=[]\n"
             "EXEC_ARGS_CODEX=[]\nT = [\n" + e + "]", ns)
        self.assertEqual(ns["T"][0][5], {"lean": True})
        self.assertEqual(ns["T"][0][4], "digest")

    def test_sem_lean_a_entrada_nao_muda(self):
        e = self.entry()
        self.assertNotIn("lean", e)
        self.assertNotIn('"digest",', e.split("\n")[-2] if e.endswith("\n") else e)


if __name__ == "__main__":
    unittest.main()
