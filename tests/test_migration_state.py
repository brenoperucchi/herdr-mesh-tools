"""Tests for the rev->rev-1 migration mechanism (_herdr_migration.py) and
its touch points in _herdr_dispatch.py / herdr-bootstrap.

Context: three text-only review rounds (herdr-4, herdr-5, herdr-6, see
.herdr/review/) reprovaram versoes anteriores deste plano por falta de lock
atomico de verdade, identidade de escrita divergindo da attestation, e
`_infer_role` nao reconhecendo `-rev-1`. Estes testes cobrem exatamente
esses pontos contra codigo real, nao mais so texto.
"""
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from unittest.mock import patch

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
# _herdr_migration.py faz `import _herdr_dispatch as core` sem inserir o
# proprio sys.path (assume que quem o importa - herdr-review-dispatch,
# herdr-ask, herdr-bootstrap, herdr-swap, herdr-migrate-rev - ja fez isso
# antes). Aqui carregamos _herdr_migration.py diretamente via SourceFileLoader,
# entao precisamos garantir o mesmo antes do primeiro _load().
sys.path.insert(0, BIN_DIR)


def _load(filename, module_name):
    path = os.path.join(BIN_DIR, filename)
    loader = SourceFileLoader(module_name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class MigrationStateTests(unittest.TestCase):
    def setUp(self):
        self.migration = _load("_herdr_migration.py", "herdr_migration_state_tests")
        self.core = _load("_herdr_dispatch.py", "herdr_dispatch_migration_tests")
        self.migration.core = self.core  # garante que os dois enxergam o mesmo módulo mockável
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cwd = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    # --- estado / escrita atômica -----------------------------------

    def test_missing_state_file_is_legacy_default(self):
        state = self.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "legacy")
        self.assertEqual(state["rev2_kind"], "claude")

    def test_atomic_write_roundtrips_and_leaves_no_tmp_files(self):
        self.migration.write_migration_state_atomic(self.cwd, {"phase": "migrated", "rev2_kind": "claude"})
        state = self.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "migrated")
        self.assertEqual(state["rev2_kind"], "claude")
        self.assertIsNotNone(state["updated_at"])

        herdr_dir = os.path.join(self.cwd, ".herdr")
        leftovers = [f for f in os.listdir(herdr_dir) if ".tmp." in f]
        self.assertEqual(leftovers, [], "escrita atômica não deve deixar arquivo temporário pra trás")

    # --- lock via O_EXCL ----------------------------------------------

    def test_second_lock_acquire_fails_while_first_active(self):
        self.assertTrue(self.migration.acquire_migration_lock(self.cwd, owner={"pid": os.getpid(), "hostname": "h"}))
        self.assertFalse(self.migration.acquire_migration_lock(self.cwd, owner={"pid": os.getpid(), "hostname": "h"}))
        self.assertTrue(self.migration.is_locked(self.cwd))

    def test_lock_release_allows_new_acquire(self):
        self.migration.acquire_migration_lock(self.cwd)
        self.migration.release_migration_lock(self.cwd)
        self.assertFalse(self.migration.is_locked(self.cwd))
        self.assertTrue(self.migration.acquire_migration_lock(self.cwd))

    def test_stale_lock_from_dead_pid_is_taken_over(self):
        # PID improvável de existir; simula processo morto sem heartbeat há muito tempo.
        dead_owner = {"pid": 999999, "hostname": "h", "ts": time.time() - (self.migration.LOCK_STALE_S + 60)}
        lock_path = self.migration.migration_lock_path(self.cwd)
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        import json
        with open(lock_path, "w") as f:
            json.dump(dead_owner, f)

        self.assertTrue(self.migration.acquire_migration_lock(self.cwd))

    def test_fresh_lock_from_dead_pid_is_not_taken_over(self):
        # pid morto mas dentro da janela de stale ainda - nao deve destravar.
        dead_owner = {"pid": 999999, "hostname": "h", "ts": time.time()}
        lock_path = self.migration.migration_lock_path(self.cwd)
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        import json
        with open(lock_path, "w") as f:
            json.dump(dead_owner, f)

        self.assertFalse(self.migration.acquire_migration_lock(self.cwd))

    # --- dual-read fail-closed ------------------------------------------

    def test_resolve_reviewer_name_only_rev(self):
        with patch.object(self.core, "agent_status_safe", side_effect=lambda n: n == "foo-rev"):
            self.assertEqual(self.migration.resolve_reviewer_name(self.cwd, "foo"), "foo-rev")

    def test_resolve_reviewer_name_only_rev1(self):
        with patch.object(self.core, "agent_status_safe", side_effect=lambda n: n == "foo-rev-1"):
            self.assertEqual(self.migration.resolve_reviewer_name(self.cwd, "foo"), "foo-rev-1")

    def test_resolve_reviewer_name_both_alive_raises(self):
        with patch.object(self.core, "agent_status_safe", return_value=True):
            with self.assertRaises(RuntimeError):
                self.migration.resolve_reviewer_name(self.cwd, "foo")

    def test_resolve_reviewer_name_neither_alive_raises(self):
        with patch.object(self.core, "agent_status_safe", return_value=False):
            with self.assertRaises(RuntimeError):
                self.migration.resolve_reviewer_name(self.cwd, "foo")

    # --- _infer_role reconhece -rev-1 ------------------------------------

    def test_infer_role_recognizes_rev1_as_rev(self):
        self.assertEqual(self.core._infer_role("foo-rev-1"), "rev")
        self.assertEqual(self.core._infer_role("foo-rev"), "rev")
        self.assertEqual(self.core._infer_role("foo-rev-2"), "rev-2")

    def test_role_reinforcement_prompt_never_says_nao_determinado_for_rev1(self):
        prompt = self.core.role_reinforcement_prompt("foo-rev-1", "foo", "/repo/foo", ["foo-rev-2"])
        self.assertNotIn("não determinado", prompt)
        self.assertIn("papel `rev`", prompt)

    # --- detector de rodada em voo ----------------------------------------

    def test_round_in_flight_true_when_request_without_verdict(self):
        round_dir = os.path.join(self.cwd, ".herdr", "review", "foo-1", "foo-rev")
        os.makedirs(round_dir)
        with open(os.path.join(round_dir, "request.md"), "w") as f:
            f.write("x")
        self.assertTrue(self.migration.round_in_flight(self.cwd))

    def test_round_in_flight_false_when_verdict_present(self):
        round_dir = os.path.join(self.cwd, ".herdr", "review", "foo-1", "foo-rev")
        os.makedirs(round_dir)
        with open(os.path.join(round_dir, "request.md"), "w") as f:
            f.write("x")
        with open(os.path.join(round_dir, "verdict.md"), "w") as f:
            f.write("APPROVE")
        self.assertFalse(self.migration.round_in_flight(self.cwd))

    def test_round_in_flight_ignores_absence_of_metrics_json(self):
        # Achado recorrente herdr-4/5/6: metrics.json ausente NAO deve, por si
        # só, contar como rodada em voo (varias rodadas historicas legitimas
        # nunca tiveram metrics.json).
        round_dir = os.path.join(self.cwd, ".herdr", "review", "foo-1", "foo-rev")
        os.makedirs(round_dir)
        with open(os.path.join(round_dir, "request.md"), "w") as f:
            f.write("x")
        with open(os.path.join(round_dir, "verdict.md"), "w") as f:
            f.write("APPROVE")
        # sem metrics.json em lugar nenhum
        self.assertFalse(self.migration.round_in_flight(self.cwd))

    def test_round_in_flight_false_for_ask_namespace_answer(self):
        round_dir = os.path.join(self.cwd, ".herdr", "ask", "foo-1", "foo-rev-2")
        os.makedirs(round_dir)
        with open(os.path.join(round_dir, "request.md"), "w") as f:
            f.write("x")
        with open(os.path.join(round_dir, "answer.md"), "w") as f:
            f.write("posição: ...")
        self.assertFalse(self.migration.round_in_flight(self.cwd))

    # --- contrato de leitura histórica -----------------------------------

    def test_resolve_historical_artifact_falls_back_rev1_to_rev(self):
        round_dir = os.path.join(self.cwd, "some-round")
        os.makedirs(os.path.join(round_dir, "foo-rev"))
        with open(os.path.join(round_dir, "foo-rev", "verdict.md"), "w") as f:
            f.write("old verdict")
        path = self.migration.resolve_historical_artifact(round_dir, "foo-rev-1", "verdict.md")
        self.assertTrue(path.endswith(os.path.join("foo-rev", "verdict.md")))

    def test_resolve_historical_artifact_ambiguous_raises(self):
        round_dir = os.path.join(self.cwd, "some-round")
        os.makedirs(os.path.join(round_dir, "foo-rev"))
        os.makedirs(os.path.join(round_dir, "foo-rev-1"))
        with open(os.path.join(round_dir, "foo-rev", "verdict.md"), "w") as f:
            f.write("old")
        with open(os.path.join(round_dir, "foo-rev-1", "verdict.md"), "w") as f:
            f.write("new")
        with self.assertRaises(RuntimeError):
            self.migration.resolve_historical_artifact(round_dir, "foo-rev-1", "verdict.md")

    def test_resolve_historical_artifact_missing_raises(self):
        round_dir = os.path.join(self.cwd, "some-round")
        os.makedirs(round_dir)
        with self.assertRaises(RuntimeError):
            self.migration.resolve_historical_artifact(round_dir, "foo-rev-1", "verdict.md")

    # --- acoplamento kind -> extra_args (rev-2) --------------------------

    def test_extra_args_for_rev2_claude_gets_opus_flag(self):
        self.assertEqual(self.migration.extra_args_for_rev2("claude"), ["--model", "opus"])

    def test_extra_args_for_rev2_grok_gets_no_flags(self):
        self.assertEqual(self.migration.extra_args_for_rev2("grok"), [])


class BootstrapRevAgentsTests(unittest.TestCase):
    """herdr-bootstrap nunca deve criar `-rev-1` fora de phase=migrated, nem
    recriar `-rev` depois de migrated (achado P1 da herdr-6)."""

    def setUp(self):
        self.bootstrap = _load("herdr-bootstrap", "herdr_bootstrap_migration_tests")
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cwd = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_legacy_phase_builds_bare_rev_name(self):
        agents = self.bootstrap.build_rev_agents("foo", self.cwd)
        names = [a[0] for a in agents]
        self.assertIn("foo-rev", names)
        self.assertNotIn("foo-rev-1", names)

    def test_migrated_phase_builds_rev1_name(self):
        self.bootstrap.migration.write_migration_state_atomic(self.cwd, {"phase": "migrated", "rev2_kind": "grok"})
        agents = self.bootstrap.build_rev_agents("foo", self.cwd)
        names = [a[0] for a in agents]
        self.assertIn("foo-rev-1", names)
        self.assertNotIn("foo-rev", names)

    def test_rev2_kind_and_extra_args_stay_coupled(self):
        self.bootstrap.migration.write_migration_state_atomic(self.cwd, {"phase": "legacy", "rev2_kind": "grok"})
        agents = self.bootstrap.build_rev_agents("foo", self.cwd)
        rev2 = next(a for a in agents if a[0] == "foo-rev-2")
        self.assertEqual(rev2[1], "grok")
        self.assertEqual(rev2[2], [])  # nunca --model opus com kind grok

        self.bootstrap.migration.write_migration_state_atomic(self.cwd, {"phase": "legacy", "rev2_kind": "claude"})
        agents = self.bootstrap.build_rev_agents("foo", self.cwd)
        rev2 = next(a for a in agents if a[0] == "foo-rev-2")
        self.assertEqual(rev2[1], "claude")
        self.assertEqual(rev2[2], ["--model", "opus"])


if __name__ == "__main__":
    unittest.main()
