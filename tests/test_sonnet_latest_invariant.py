"""Regra de 2026-11-18: nenhum agent abre, troca ou restaura com Sonnet que
nao seja a ultima versao. A "ultima" fica em _herdr_dispatch.SONNET_LATEST; a proxima
versao e' uma troca so naquela constante e nenhum teste muda.

Achado que originou a regra: EXEC_ARGS_SONNET_5 fixava `claude-sonnet-5` (sem o -5) e
herdr-mesh-tools-exec rodava nele sem ninguem notar.
"""
import importlib.util
import os
import sys
import unittest
from importlib.machinery import SourceFileLoader

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
sys.path.insert(0, BIN_DIR)
import _herdr_dispatch as core  # noqa: E402


def _load(name):
    loader = SourceFileLoader(name.replace("-", "_"), os.path.join(BIN_DIR, name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


boot = _load("herdr-bootstrap")
restart = _load("herdr-agent-restart")
swap = _load("herdr-swap")


def _is_sonnet(model):
    return bool(model) and "sonnet" in str(model).casefold()


def _model(args):
    for i, a in enumerate(args):
        if a == "--model" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--model="):
            return a.split("=", 1)[1]
    return None


def _all_profiles():
    for name in dir(boot):
        val = getattr(boot, name)
        if name.endswith("ARGS") or "_ARGS_" in name or name.startswith("REV"):
            if isinstance(val, list) and all(isinstance(x, str) for x in val):
                yield name, val
    for entry in boot.SPACES:
        for part in entry[2:]:
            if isinstance(part, tuple) and len(part) >= 3 and isinstance(part[2], list):
                yield part[0], part[2]


class SonnetLatestInvariant(unittest.TestCase):
    def test_constant_is_a_fully_pinned_sonnet(self):
        self.assertTrue(core.SONNET_LATEST.startswith("claude-sonnet-"))
        self.assertNotIn(core.SONNET_LATEST, ("claude-sonnet-5", "sonnet-5", "sonnet"))

    def test_every_profile_and_table_row_with_sonnet_uses_the_latest(self):
        seen = 0
        for name, args in _all_profiles():
            m = _model(args)
            if _is_sonnet(m):
                seen += 1
                with self.subTest(perfil=name):
                    self.assertEqual(m, core.SONNET_LATEST)
        self.assertGreater(seen, 0, "nenhum perfil Sonnet encontrado: o teste ficou cego")

    def test_matrix_allows_no_other_sonnet(self):
        for role, models in boot.ALLOWED_MODELS_BY_ROLE.items():
            for m in models:
                if _is_sonnet(m):
                    with self.subTest(papel=role, modelo=m):
                        self.assertEqual(m, core.SONNET_LATEST)

    def test_swap_translates_status_sonnet_to_the_latest(self):
        for shown in ("Sonnet 5", "sonnet"):
            with self.subTest(status=shown):
                args = swap._runtime_start_args(
                    "claude", {"observed": True, "kind": "claude", "model": shown,
                               "reasoning_effort": "low"})
                self.assertEqual(_model(args), core.SONNET_LATEST)

    def test_restart_refuses_older_sonnet_aliases(self):
        for old in ("sonnet-5", "claude-sonnet-5"):
            with self.subTest(modelo=old):
                self.assertFalse(restart.modelo_valido(old))
        self.assertTrue(restart.modelo_valido(core.SONNET_LATEST))


if __name__ == "__main__":
    unittest.main()
