"""Todo lancamento Codex do mesh leva --no-daemon (Herdr 0.9.4, #4756): sem a flag o hook de fim de
turno nao funciona e o agent fica em `unknown`."""
import importlib.util
import os
import sys
import unittest
from importlib.machinery import SourceFileLoader

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
sys.path.insert(0, BIN)
import _herdr_dispatch as core  # noqa: E402


def _load(nome):
    loader = SourceFileLoader(nome.replace("-", "_") + "_nodaemon_test", os.path.join(BIN, nome))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class CodexLaunchFlagsTests(unittest.TestCase):
    def test_acrescenta_as_duas_flags(self):
        out = core.codex_launch_flags(["--model", "gpt-6.1-sol"])
        self.assertEqual(out[:2], ["--model", "gpt-6.1-sol"])
        self.assertIn("--no-daemon", out)
        self.assertIn("agents.max_depth=0", out)

    def test_nao_duplica(self):
        out = core.codex_launch_flags(["--no-daemon", "-c", "agents.max_depth=0"])
        self.assertEqual(out.count("--no-daemon"), 1)
        self.assertEqual(sum("agents.max_depth" in a for a in out), 1)

    def test_nao_altera_a_lista_de_entrada(self):
        entrada = ["--model", "m"]
        core.codex_launch_flags(entrada)
        self.assertEqual(entrada, ["--model", "m"])


class AgentRestartTests(unittest.TestCase):
    def setUp(self):
        self.r = _load("herdr-agent-restart")

    def test_restart_codex_leva_no_daemon_depois_do_resume(self):
        a = self.r.start_args("codex", "sid-1", "gpt-6.1-sol", "low", old_argv=None)
        self.assertEqual(a[:2], ["resume", "sid-1"])
        self.assertEqual(a.count("--no-daemon"), 1)

    def test_restart_nao_duplica_a_flag_do_argv_antigo(self):
        old = ["codex", "resume", "sid-0", "-m", "x", "--no-daemon", "--no-alt-screen"]
        a = self.r.start_args("codex", "sid-1", "gpt-6.1-sol", "low", old_argv=old)
        self.assertEqual(a.count("--no-daemon"), 1)
        self.assertIn("--no-alt-screen", a)  # flags soltas antigas continuam preservadas

    def test_restart_claude_nao_leva_a_flag(self):
        a = self.r.start_args("claude", "sid-1", "claude-opus-5-5", "high")
        self.assertNotIn("--no-daemon", a)


if __name__ == "__main__":
    unittest.main()
