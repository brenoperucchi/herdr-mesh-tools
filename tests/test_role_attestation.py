"""Regression tests for model-independent Herdr role attestation.

The same neutral question produced a generic training hierarchy from Codex and
an observed Herdr identity from Claude. The reinforcement prompt must make
the latter an explicit protocol requirement for every role.
"""
import importlib.util
import os
import tempfile
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

    def test_review_and_ask_require_actionable_recommendation(self):
        for required in (
            "{solution_contract_reviewer}",
        ):
            self.assertIn(required, self.review.PROTOCOL)
        for required in (
            "solução proposta",
            "validação",
            "decisão necessária",
            "não determinada",
            "ação necessária: nenhuma",
        ):
            self.assertIn(required, self.core.SOLUTION_CONTRACT_REVIEWER.lower())
        for required in (
            "{solution_contract_ask}",
        ):
            self.assertIn(required, self.ask.ASK_PROTOCOL)
        for required in (
            "recomendação executável",
            "validação e próximo passo",
            "recomendação: não determinada",
            "ação necessária: nenhuma",
        ):
            self.assertIn(required, self.core.SOLUTION_CONTRACT_ASK.lower())

    def test_exec_hydration_requires_user_facing_solution_summary(self):
        prompt = self.core.exec_hydration_prompt(
            "foo-exec", "foo", "/repo/foo", ["foo-rev-1", "foo-rev-2", "foo-scout"],
        )
        for required in (
            "problema/impacto",
            "solução proposta",
            "decisão do exec",
            "próximo passo",
            "critério de conclusão",
            "pergunta exata",
        ):
            self.assertIn(required, prompt.lower())

    def test_solution_contract_status_distinguishes_complete_and_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            review_path = os.path.join(directory, "verdict.md")
            with open(review_path, "w", encoding="utf-8") as stream:
                stream.write("P2 — solução proposta: usar lock.\nValidação: teste de regressão.\n")
            self.assertTrue(self.core.solution_contract_status(review_path, "review")["ok"])
            with open(review_path, "w", encoding="utf-8") as stream:
                stream.write("P2 — achado sem recomendação.\n")
            result = self.core.solution_contract_status(review_path, "review")
            self.assertFalse(result["ok"])
            self.assertIn("solução proposta", result["missing"])
            with open(review_path, "w", encoding="utf-8") as stream:
                stream.write(
                    "# Veredito\nAPPROVE não se aplica: há achados.\n"
                    "solução proposta: X\nvalidação: pytest\n"
                )
            self.assertTrue(self.core.solution_contract_status(review_path, "review")["ok"])
            with open(review_path, "w", encoding="utf-8") as stream:
                stream.write("APPROVE — ação necessária: nenhuma.\n")
            self.assertTrue(self.core.solution_contract_status(review_path, "review")["ok"])

            ask_path = os.path.join(directory, "answer.md")
            with open(ask_path, "w", encoding="utf-8") as stream:
                stream.write("Recomendação executável: aplicar o patch.\nValidação e próximo passo: rodar testes.\n")
            self.assertTrue(self.core.solution_contract_status(ask_path, "ask")["ok"])

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
            solution_contract_reviewer=self.core.SOLUTION_CONTRACT_REVIEWER,
        )
        self.assertIn(self.core.INTERACTIVE_READY_CAVEAT, review_request)

        verify_request = self.review.VERIFY_PROTOCOL.format(
            verdict_dir="/x", prior_verdict="x", head_sha="h", base_ref="b",
            interactive_ready_caveat=self.core.INTERACTIVE_READY_CAVEAT,
            solution_contract_reviewer=self.core.SOLUTION_CONTRACT_REVIEWER,
        )
        self.assertIn(self.core.SOLUTION_CONTRACT_REVIEWER, verify_request)

        ask_request = self.ask.ASK_PROTOCOL.format(
            verdict_dir="/x", isolation_note="", context_instruction="",
            interactive_ready_caveat=self.core.INTERACTIVE_READY_CAVEAT,
            solution_contract_ask=self.core.SOLUTION_CONTRACT_ASK,
        )
        self.assertIn(self.core.INTERACTIVE_READY_CAVEAT, ask_request)


if __name__ == "__main__":
    unittest.main()
