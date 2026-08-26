#!/usr/bin/env python3
"""Regression test for herdr-ask's write_request(): the isolation paragraph
("Esta consulta é CEGA...") must only appear when there's a real sibling
consultant. A live end-to-end run with --reviewer (single-consultant mode)
caught this producing a placeholder string ("(nenhum — consulta a 1
consultor só)") formatted straight into the prose as if it were a real path
— confusing, and technically instructs the agent to avoid a directory that
doesn't exist.

Run: python3 -m unittest discover -s tests -v
"""
import importlib.util
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load(name):
    path = os.path.join(BIN_DIR, name)
    loader = SourceFileLoader(name.replace("-", "_"), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class WriteRequestIsolationTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load("herdr-ask")
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.verdict_dir = os.path.join(self.tmpdir.name, "consultor")
        os.makedirs(self.verdict_dir)

    def _read_request(self):
        with open(os.path.join(self.verdict_dir, "request.md")) as f:
            return f.read()

    def test_single_reviewer_has_no_isolation_paragraph(self):
        self.mod.write_request(
            self.tmpdir.name, self.verdict_dir, None,
            "pergunta de teste", None, {},
        )
        content = self._read_request()
        self.assertNotIn("CEGA", content)
        self.assertNotIn("diretório irmão", content)
        self.assertNotIn("nenhum —", content, "placeholder nao deveria vazar pro texto do protocolo")

    def test_two_reviewers_has_isolation_paragraph_with_real_path(self):
        sibling = os.path.join(self.tmpdir.name, "outro-consultor")
        self.mod.write_request(
            self.tmpdir.name, self.verdict_dir, sibling,
            "pergunta de teste", None, {},
        )
        content = self._read_request()
        self.assertIn("CEGA", content)
        self.assertIn(sibling, content)


if __name__ == "__main__":
    unittest.main()
