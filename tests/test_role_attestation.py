"""Regression tests for model-independent Herdr role attestation.

The same neutral question produced a generic training hierarchy from Codex and
an observed Herdr identity from Claude. The reinforcement prompt must make
the latter an explicit protocol requirement for every role.
"""
import importlib.util
import os
import unittest
from importlib.machinery import SourceFileLoader

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load(filename, module_name):
    path = os.path.join(BIN_DIR, filename)
    loader = SourceFileLoader(module_name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class RoleAttestationTests(unittest.TestCase):
    def setUp(self):
        self.core = _load("_herdr_dispatch.py", "herdr_dispatch_role_attestation")
        self.review = _load("herdr-review-dispatch", "herdr_review_role_attestation")
        self.ask = _load("herdr-ask", "herdr_ask_role_attestation")

    def test_reviewer_must_observe_identity_and_authority(self):
        prompt = self.core.role_reinforcement_prompt(
            "claude-bridge-rev",
            "claude-bridge",
            "/repo/claude-bridge",
            ["claude-bridge-exec", "claude-bridge-rev-2"],
        )

        for required in (
            "herdr agent get claude-bridge-rev",
            "herdr agent list",
            "Breno",
            "claude-bridge-exec",
            "artifact do colega cego",
            "pare e registre a inconsistência",
            "Panes headless de revisão",
            "letra isolada digitada por engano",
            "não deve\nparar o fluxo",
        ):
            self.assertIn(required, prompt)

    def test_scout_keeps_observed_identity_and_read_only_boundary(self):
        prompt = self.core.role_reinforcement_prompt(
            "claude-bridge-scout",
            "claude-bridge",
            "/repo/claude-bridge",
            ["claude-bridge-exec", "claude-bridge-rev", "claude-bridge-rev-2"],
        )

        self.assertIn("herdr agent get claude-bridge-scout", prompt)
        self.assertIn("READ-ONLY", prompt)
        self.assertIn("papel `scout`", prompt)

    def test_exec_hydration_carries_escalation_policy(self):
        prompt = self.core.exec_hydration_prompt(
            "foo-exec", "foo", "/repo/foo", ["foo-rev-1", "foo-rev-2", "foo-scout"],
        )

        for required in (
            self.core.EXEC_HYDRATION_MARKER,
            "no máximo duas rodadas de correção",
            "herdr-ask --reviewer scout",
            "sem pedir autorização prévia ao Breno",
            "devolve somente para este exec",
            "divergência ou incerteza",
            "decisão final antes de qualquer commit",
        ):
            self.assertIn(required, prompt)

    def test_each_dispatch_protocol_repeats_identity_confirmation(self):
        review_prompt = self.review.prompt_text(
            "/repo/.herdr/review/claude-bridge-7/claude-bridge-rev"
        )
        ask_prompt = self.ask.prompt_text(
            "/repo/.herdr/ask/claude-bridge-3/claude-bridge-rev-2"
        )

        self.assertIn("`claude-bridge-rev`", review_prompt)
        self.assertIn("`claude-bridge-rev-2`", ask_prompt)
        self.assertIn("attestation", self.review.PROTOCOL.lower())
        self.assertIn("attestation", self.ask.ASK_PROTOCOL.lower())

    def test_interactive_ready_caveat_is_shared_not_duplicated(self):
        """Achado 2026-09-06: o mesmo texto sobre interactive_ready ausente
        já existiu copiado a mao em 4 lugares (ROLE_REINFORCEMENT_PROMPT,
        PROTOCOL, VERIFY_PROTOCOL, ASK_PROTOCOL) antes desta correcao -
        exatamente a classe de bug (protocolo duplicado divergindo) que ja
        causou retrabalho nas rodadas herdr-4/5/6. Confere que os 4 lugares
        referenciam a MESMA constante via placeholder, em vez de cada um
        ter sua propria copia do texto."""
        for template_name, raw_template in (
            ("ROLE_REINFORCEMENT_PROMPT", self.core.ROLE_REINFORCEMENT_PROMPT),
            ("PROTOCOL", self.review.PROTOCOL),
            ("VERIFY_PROTOCOL", self.review.VERIFY_PROTOCOL),
            ("ASK_PROTOCOL", self.ask.ASK_PROTOCOL),
        ):
            self.assertIn(
                "{interactive_ready_caveat}", raw_template,
                f"{template_name} deveria referenciar core.INTERACTIVE_READY_CAVEAT via placeholder, nao duplicar o texto",
            )

        # E confere que o texto de fato chega no output formatado final dos
        # tres pontos de entrada reais (nao so que o placeholder existe no
        # template cru).
        reinforcement = self.core.role_reinforcement_prompt(
            "foo-rev", "foo", "/repo/foo", ["foo-exec", "foo-rev-2"],
        )
        self.assertIn(self.core.INTERACTIVE_READY_CAVEAT, reinforcement)

        review_request = self.review.PROTOCOL.format(
            diff_instruction="x", verdict_dir="/x", sibling_dir="/y",
            interactive_ready_caveat=self.core.INTERACTIVE_READY_CAVEAT,
        )
        self.assertIn(self.core.INTERACTIVE_READY_CAVEAT, review_request)

        ask_request = self.ask.ASK_PROTOCOL.format(
            verdict_dir="/x", isolation_note="", context_instruction="",
            interactive_ready_caveat=self.core.INTERACTIVE_READY_CAVEAT,
        )
        self.assertIn(self.core.INTERACTIVE_READY_CAVEAT, ask_request)


if __name__ == "__main__":
    unittest.main()
