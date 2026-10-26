"""O split de revisor no bootstrap leva --cwd da tabela SPACES (nao herda a pasta do exec)."""
import importlib.util
import os
import re
import unittest
from importlib.machinery import SourceFileLoader

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load():
    loader = SourceFileLoader("herdr_bootstrap_splitcwd_test", os.path.join(BIN, "herdr-bootstrap"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class SplitCwdTests(unittest.TestCase):
    def test_split_leva_o_cwd_da_tabela(self):
        b = _load()
        argv = b.split_argv("w9:p1", "right", "/home/u/Devs/claude-bridge")
        self.assertEqual(argv, ["pane", "split", "w9:p1", "--direction", "right",
                                "--cwd", "/home/u/Devs/claude-bridge"])

    def test_nenhum_split_do_bootstrap_sai_sem_cwd(self):
        # guarda contra voltar a chamar `pane split` direto, sem --cwd
        src = open(os.path.join(BIN, "herdr-bootstrap"), encoding="utf-8").read()
        chamadas = re.findall(r'run\("pane",\s*"split"[^)]*\)', src)
        self.assertEqual(chamadas, [], "use split_argv(): pane split sem --cwd herda a pasta do exec")

    def test_tab_do_scout_ja_leva_cwd(self):
        src = open(os.path.join(BIN, "herdr-bootstrap"), encoding="utf-8").read()
        trecho = re.search(r'run\("tab",\s*"create"[^)]*\)', src).group(0)
        self.assertIn('"--cwd"', trecho)


if __name__ == "__main__":
    unittest.main()
