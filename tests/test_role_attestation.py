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


if __name__ == "__main__":
    unittest.main()
