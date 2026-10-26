"""Regra do Breno (2026-10-13): mensagem entre agents e' bidirecional; quem recebe um pedido responde
DIRETO ao remetente (herdr-can-send + herdr agent prompt). A regra precisa chegar a todo agent: no
reforco de papel (entrega ao vivo) e na skill `herdr` (versao duravel)."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import _herdr_dispatch as core  # noqa: E402


class ReplyRuleTests(unittest.TestCase):
    def test_todo_papel_recebe_a_regra_no_reforco(self):
        for name in ("x-exec", "x-rev-1", "x-rev-2", "x-scout"):
            p = core.role_reinforcement_prompt(name, "x", "/tmp/x", [])
            self.assertIn("BIDIRECIONAIS", p, name)
            self.assertIn("herdr-can-send", p, name)
            self.assertIn("Assunto:", p, name)

    def test_space_enxuto_tambem_recebe(self):
        p = core.role_reinforcement_prompt("x-exec", "x", "/tmp/x", [], lean=True)
        self.assertIn("BIDIRECIONAIS", p)

    def test_hidratacao_de_exec_carrega_a_regra(self):
        self.assertIn("BIDIRECIONAIS", core.exec_hydration_prompt("x-exec", "x", "/tmp/x", []))

    def test_skill_herdr_documenta_formato_e_excecao(self):
        s = open(os.path.join(ROOT, "skills", "herdr", "SKILL.md"), encoding="utf-8").read()
        self.assertIn("Mensagens entre agents são bidirecionais", s)
        self.assertIn("[de <seu-nome> para <remetente>] Assunto:", s)
        self.assertIn("decisão dele", s)


class PersistentReplyRuleTests(unittest.TestCase):
    def test_live_role_and_hydration_include_persistent_reply_route(self):
        for text in (core.role_reinforcement_prompt('x-exec', 'x', '/tmp/x', []),
                     core.exec_hydration_prompt('x-exec', 'x', '/tmp/x', [])):
            self.assertIn('herdr-message reply', text)
            self.assertIn('reply_recorded', text)
            self.assertIn('worker_unavailable', text)
            self.assertIn('nada foi enfileirado', text)
            self.assertIn('--key reply:<ID>', text)
            self.assertIn('não faça fallback manual', text)


if __name__ == "__main__":
    unittest.main()
