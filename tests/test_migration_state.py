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

    def test_atomic_write_merges_with_disk_not_with_default(self):
        # Achado P2-1 herdr-7: um patch parcial não pode apagar um campo
        # não-default já persistido só porque não foi citado no patch.
        self.migration.write_migration_state_atomic(self.cwd, {"phase": "migrated", "rev2_kind": "grok"})
        self.migration.write_migration_state_atomic(self.cwd, {"phase": "legacy", "note": "revertido"})
        state = self.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "legacy")
        self.assertEqual(state["rev2_kind"], "grok", "rev2_kind não pode voltar pro default só por causa de um patch parcial")
        self.assertEqual(state["note"], "revertido")

    # --- lock via flock (achado P1 herdr-7: lock anterior, baseado em
    # existência de arquivo + heurística de idade/pid, tinha uma corrida
    # real entre "detectar stale" e "recriar", e liberava sem checar
    # ownership. flock é preso ao file descriptor: nenhuma heurística de
    # staleness é necessária, e só quem detém pode liberar) -------------

    def test_second_lock_acquire_fails_while_first_active(self):
        handle = self.migration.acquire_migration_lock(self.cwd, owner={"pid": os.getpid(), "hostname": "h"})
        self.assertIsNotNone(handle)
        self.assertIsNone(self.migration.acquire_migration_lock(self.cwd, owner={"pid": os.getpid(), "hostname": "h"}))
        self.assertTrue(self.migration.is_locked(self.cwd))

    def test_lock_release_allows_new_acquire(self):
        handle = self.migration.acquire_migration_lock(self.cwd)
        self.migration.release_migration_lock(handle)
        self.assertFalse(self.migration.is_locked(self.cwd))
        handle2 = self.migration.acquire_migration_lock(self.cwd)
        self.assertIsNotNone(handle2)
        self.migration.release_migration_lock(handle2)

    def test_lock_auto_releases_when_holder_process_exits(self):
        # flock morre com o processo que o detém - simula isso fechando o fd
        # sem passar por release_migration_lock (equivalente a um crash: o
        # kernel fecha os fds abertos quando o processo morre).
        handle = self.migration.acquire_migration_lock(self.cwd)
        os.close(handle)
        self.assertFalse(self.migration.is_locked(self.cwd), "lock não deveria sobreviver ao fechamento do fd que o detém")

    def test_space_is_gated_false_while_only_locked_without_migrating_phase(self):
        # Achado P2-1 herdr-15 (herdr-rev-2): o lock isolado (sem
        # phase="migrating") deixou de bloquear os LEITORES deste módulo -
        # herdr-worker.ts (claude-bridge) passou a segurar o mesmo lock por
        # até 2 minutos (a duração de um agent.prompt) só pra ter exclusão
        # mútua com o migrador, e sob a semântica antiga isso fazia os
        # quatro dispatchers abortarem achando que havia uma migração real
        # em andamento, quando só havia um worker rev-1 em uso normal.
        handle = self.migration.acquire_migration_lock(self.cwd)
        self.assertFalse(self.migration.space_is_gated(self.cwd), "lock isolado (sem phase=migrating) não deve mais gatear os leitores")
        self.migration.release_migration_lock(handle)
        self.assertFalse(self.migration.space_is_gated(self.cwd))

    def test_space_is_gated_true_while_locked_and_migrating(self):
        # phase="migrating" continua bloqueando (é o que de fato significa
        # "há uma migração") - só o lock ISOLADO parou de bloquear sozinho.
        self.migration.write_migration_state_atomic(self.cwd, {"phase": "migrating"})
        handle = self.migration.acquire_migration_lock(self.cwd)
        self.assertTrue(self.migration.space_is_gated(self.cwd))
        self.migration.release_migration_lock(handle)
        self.assertTrue(self.migration.space_is_gated(self.cwd), "migrating bloqueia com ou sem o lock (marca durável)")

    def test_space_is_gated_true_for_pending_manual_even_without_lock(self):
        # Achado P1-2 herdr-7: phase=pending-manual precisa continuar
        # bloqueando MESMO depois que o lock (e o processo que o detinha)
        # já não existem mais - ao contrário do lock antigo, que expirava
        # sozinho e "esquecia" o pending-manual.
        self.migration.write_migration_state_atomic(self.cwd, {"phase": "pending-manual", "note": "algo deu errado"})
        self.assertFalse(self.migration.is_locked(self.cwd))
        self.assertTrue(self.migration.space_is_gated(self.cwd))

    def test_space_is_gated_true_for_migrating_even_without_lock(self):
        # Achado P1-1 herdr-8: uma versão intermediária desta função
        # gravava phase=migrating (marca durável, propositalmente pensada
        # pra sobreviver ao flock) mas NUNCA a lia de volta — cobria só
        # pending-manual e o flock. Cenário real: herdr-migrate-rev grava
        # migrating, morre antes de gravar o estado final (crash/SIGKILL),
        # o flock morre com o processo — sem este teste, o space voltaria a
        # parecer livre.
        self.migration.write_migration_state_atomic(self.cwd, {"phase": "migrating"})
        self.assertFalse(self.migration.is_locked(self.cwd), "o cenário do achado é justamente o lock já ter sumido")
        self.assertTrue(self.migration.space_is_gated(self.cwd))

    def test_space_gate_reason_distinguishes_the_three_states(self):
        self.assertIsNone(self.migration.space_gate_reason(self.cwd))

        self.migration.write_migration_state_atomic(self.cwd, {"phase": "migrating"})
        self.assertEqual(self.migration.space_gate_reason(self.cwd), "migrating")

        self.migration.write_migration_state_atomic(self.cwd, {"phase": "pending-manual", "note": "x"})
        self.assertEqual(self.migration.space_gate_reason(self.cwd), "pending-manual")

        # Achado P2-1 herdr-15: "legacy" com o lock isolado detido (sem
        # phase="migrating") não bloqueia mais - só a phase decide.
        self.migration.write_migration_state_atomic(self.cwd, {"phase": "legacy"})
        handle = self.migration.acquire_migration_lock(self.cwd)
        self.assertIsNone(self.migration.space_gate_reason(self.cwd), "lock isolado não é mais motivo de gate pros leitores")
        self.migration.release_migration_lock(handle)

        self.assertIsNone(self.migration.space_gate_reason(self.cwd))
        self.migration.write_migration_state_atomic(self.cwd, {"phase": "migrated"})
        self.assertIsNone(self.migration.space_gate_reason(self.cwd))

    def test_space_gate_reason_fails_closed_on_unrecognized_phase(self):
        # Achado P4 herdr-10 (herdr-rev): a versão anterior só bloqueava em
        # migrating/pending-manual e liberava QUALQUER outro valor -
        # inclusive um marcador de segurança logicamente corrompido (JSON
        # válido, phase inválida/typo/null).
        self.migration.write_migration_state_atomic(self.cwd, {"phase": "renaming"})
        self.assertEqual(self.migration.space_gate_reason(self.cwd), "unknown-phase")
        self.assertTrue(self.migration.space_is_gated(self.cwd))
        msg = self.migration.space_gate_message(self.cwd, "foo")
        self.assertIn("renaming", msg)
        self.assertIn("falhando fechado", msg)

    def test_space_gate_message_includes_note_for_pending_manual(self):
        # Achado P2-2 herdr-8: as quatro mensagens antigas diziam "lock
        # ativo — tente de novo depois" pra pending-manual também, o que é
        # enganoso (não há lock, e "de novo" nunca resolve sozinho).
        self.migration.write_migration_state_atomic(self.cwd, {"phase": "pending-manual", "note": "motivo específico"})
        msg = self.migration.space_gate_message(self.cwd, "foo")
        self.assertIn("pending-manual", msg)
        self.assertIn("motivo específico", msg)
        self.assertNotIn("tente de novo", msg)

    def test_is_locked_does_not_create_anything_on_disk(self):
        # Achado P2-1 herdr-8: a versão anterior fazia os.makedirs +
        # os.open(O_CREAT, ...) só pra CONSULTAR se estava travado,
        # materializando .herdr/migration.lock fantasma inclusive em cwds
        # que nem existem.
        nonexistent_cwd = os.path.join(self.cwd, "nao-existe-ainda")
        self.assertFalse(os.path.isdir(nonexistent_cwd))
        self.assertFalse(self.migration.is_locked(nonexistent_cwd))
        self.assertFalse(os.path.isdir(nonexistent_cwd), "consultar não deveria criar nada em disco")

    # --- dual-read fail-closed ------------------------------------------

    # Achado P1 herdr-13 (herdr-rev): resolve_reviewer_name passou a usar
    # agent_status_or_raise (que chama core.agent_status -> core.get_agent_info),
    # não mais o agent_status_safe fail-open — mockar o alias antigo deixaria
    # a chamada real escapar pro binário `herdr`.
    def _fake_get_agent_info(self, alive_names):
        def fake(name):
            if name in alive_names:
                return {"agent_status": "idle"}
            raise RuntimeError(f"agent target {name}: agent_not_found")
        return fake

    def test_resolve_reviewer_name_only_rev(self):
        with patch.object(self.core, "get_agent_info", side_effect=self._fake_get_agent_info({"foo-rev"})):
            self.assertEqual(self.migration.resolve_reviewer_name(self.cwd, "foo"), "foo-rev")

    def test_resolve_reviewer_name_only_rev1(self):
        with patch.object(self.core, "get_agent_info", side_effect=self._fake_get_agent_info({"foo-rev-1"})):
            self.assertEqual(self.migration.resolve_reviewer_name(self.cwd, "foo"), "foo-rev-1")

    def test_resolve_reviewer_name_both_alive_raises(self):
        with patch.object(self.core, "get_agent_info", side_effect=self._fake_get_agent_info({"foo-rev", "foo-rev-1"})):
            with self.assertRaises(RuntimeError):
                self.migration.resolve_reviewer_name(self.cwd, "foo")

    def test_resolve_reviewer_name_neither_alive_raises(self):
        with patch.object(self.core, "get_agent_info", side_effect=self._fake_get_agent_info(set())):
            with self.assertRaises(RuntimeError):
                self.migration.resolve_reviewer_name(self.cwd, "foo")

    def test_resolve_reviewer_name_infra_failure_propagates(self):
        # Achado P1 herdr-13: um timeout consultando UM dos dois nomes não
        # pode virar "não existe" silenciosamente — precisa propagar, não
        # deixar a função escolher o outro nome como se o estado não fosse
        # ambíguo.
        def fake(name):
            if name == "foo-rev-1":
                raise RuntimeError("sem resposta em 30s")
            return {"agent_status": "idle"}
        with patch.object(self.core, "get_agent_info", side_effect=fake):
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

    # --- detector de rodada em voo (sinal: agent_status ao vivo, não mtime
    # — achado P1-2 herdr-8: uma janela de tempo fixa mede há quanto tempo a
    # rodada foi disparada, não há quanto tempo está parada; medido contra
    # 364 rodadas reais, 9 já ultrapassavam qualquer janela fixa razoável) --

    def test_round_in_flight_true_when_agent_still_working(self):
        round_dir = os.path.join(self.cwd, ".herdr", "review", "foo-1", "foo-rev")
        os.makedirs(round_dir)
        request_path = os.path.join(round_dir, "request.md")
        with open(request_path, "w") as f:
            f.write("x")
        self._backdate_past_startup_grace(request_path)
        with patch.object(self.core, "get_agent_info", return_value={"agent_status": "working"}):
            self.assertTrue(self.migration.round_in_flight(self.cwd))

    def test_round_in_flight_true_when_agent_blocked(self):
        round_dir = os.path.join(self.cwd, ".herdr", "review", "foo-1", "foo-rev")
        os.makedirs(round_dir)
        request_path = os.path.join(round_dir, "request.md")
        with open(request_path, "w") as f:
            f.write("x")
        self._backdate_past_startup_grace(request_path)
        with patch.object(self.core, "get_agent_info", return_value={"agent_status": "blocked"}):
            self.assertTrue(self.migration.round_in_flight(self.cwd))

    def _backdate_past_startup_grace(self, path):
        # Além de _DISPATCH_STARTUP_GRACE_S, pra exercitar o ramo do
        # agent_status em vez do grace period de largada (achado P1
        # herdr-9) que, sozinho, já tornaria qualquer request.md recente
        # "em voo" independente do que o agent_status diga.
        old_ts = time.time() - (self.migration._DISPATCH_STARTUP_GRACE_S + 30)
        os.utime(path, (old_ts, old_ts))

    def test_round_in_flight_false_when_agent_idle_regardless_of_request_age(self):
        # O ponto central do achado P1-2 da herdr-8: uma rodada abandonada
        # com o revisor de volta a idle/done não conta como em voo, não
        # importa há quanto tempo.
        round_dir = os.path.join(self.cwd, ".herdr", "review", "foo-1", "foo-rev")
        os.makedirs(round_dir)
        request_path = os.path.join(round_dir, "request.md")
        with open(request_path, "w") as f:
            f.write("x")
        self._backdate_past_startup_grace(request_path)
        with patch.object(self.core, "get_agent_info", return_value={"agent_status": "idle"}):
            self.assertFalse(self.migration.round_in_flight(self.cwd))

    def test_round_in_flight_false_when_agent_truly_not_found(self):
        round_dir = os.path.join(self.cwd, ".herdr", "review", "foo-1", "foo-rev")
        os.makedirs(round_dir)
        request_path = os.path.join(round_dir, "request.md")
        with open(request_path, "w") as f:
            f.write("x")
        self._backdate_past_startup_grace(request_path)
        with patch.object(self.core, "get_agent_info", side_effect=RuntimeError("agent target foo-rev: agent_not_found")):
            self.assertFalse(self.migration.round_in_flight(self.cwd), "agent que não existe mais não é algo a esperar")

    def test_round_in_flight_true_when_agent_query_fails_for_other_reason(self):
        # Achado P2-1 herdr-9: um RuntimeError genérico (timeout, servidor
        # travado, JSON inválido) NÃO é a mesma coisa que "agent não existe
        # mais" - falha de infraestrutura tem que falhar fechado (bloquear),
        # não autorizar a migração por omissão.
        round_dir = os.path.join(self.cwd, ".herdr", "review", "foo-1", "foo-rev")
        os.makedirs(round_dir)
        request_path = os.path.join(round_dir, "request.md")
        with open(request_path, "w") as f:
            f.write("x")
        self._backdate_past_startup_grace(request_path)
        with patch.object(self.core, "get_agent_info", side_effect=RuntimeError("sem resposta em 30s (server do Herdr travado?)")):
            self.assertTrue(self.migration.round_in_flight(self.cwd))

    def test_round_in_flight_true_within_dispatch_startup_grace_regardless_of_status(self):
        # Achado P1 herdr-9: entre o dispatcher escrever request.md e o
        # agent efetivamente entrar em working, o agent ainda está
        # legitimamente idle/done - uma leitura pontual de status não
        # distingue isso de uma rodada abandonada. O grace period cobre
        # exatamente essa largada, mesmo com agent_status "idle".
        round_dir = os.path.join(self.cwd, ".herdr", "review", "foo-1", "foo-rev")
        os.makedirs(round_dir)
        with open(os.path.join(round_dir, "request.md"), "w") as f:
            f.write("x")  # mtime = agora, dentro do grace period
        with patch.object(self.core, "get_agent_info", return_value={"agent_status": "idle"}):
            self.assertTrue(self.migration.round_in_flight(self.cwd))

    def test_round_in_flight_true_regardless_of_how_slow_the_round_is(self):
        # Rodada real mais lenta medida (mfc-56/mfc-rev, 1365s) não teria
        # passado numa janela fixa de 1200s. Com o sinal por agent_status,
        # o tempo decorrido é irrelevante.
        round_dir = os.path.join(self.cwd, ".herdr", "review", "foo-1", "foo-rev")
        os.makedirs(round_dir)
        request_path = os.path.join(round_dir, "request.md")
        with open(request_path, "w") as f:
            f.write("x")
        very_old = time.time() - 1365
        os.utime(request_path, (very_old, very_old))
        with patch.object(self.core, "get_agent_info", return_value={"agent_status": "working"}):
            self.assertTrue(self.migration.round_in_flight(self.cwd))

    def test_find_in_flight_round_returns_blocking_path_for_diagnostics(self):
        # Achado P1-1 herdr-7: a versão anterior não dizia QUAL diretório
        # bloqueou, tornando o falso-positivo impossível de diagnosticar.
        round_dir = os.path.join(self.cwd, ".herdr", "review", "foo-1", "foo-rev")
        os.makedirs(round_dir)
        request_path = os.path.join(round_dir, "request.md")
        with open(request_path, "w") as f:
            f.write("x")
        self._backdate_past_startup_grace(request_path)
        with patch.object(self.core, "get_agent_info", return_value={"agent_status": "working"}):
            blocker = self.migration.find_in_flight_round(self.cwd)
        self.assertEqual(blocker, round_dir)

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


class MigrateRevGiveUpTests(unittest.TestCase):
    """Achado P2-4 herdr-8: os caminhos de "pulando esta passada" do
    herdr-migrate-rev gravavam phase="legacy" incondicionalmente, podendo
    rebaixar um space que já estava "migrated" (se `<slug>-rev` reaparecer
    por qualquer motivo) para o valor exato que faz build_rev_agents()
    recriar o nome antigo."""

    def setUp(self):
        self.migrate = _load("herdr-migrate-rev", "herdr_migrate_rev_giveup_tests")
        self.migrate.migration.core = self.migrate.core  # mesmo módulo mockável nos dois lados
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cwd = self.tmpdir.name
        # patch.dict restaura o valor original no tearDown automaticamente
        # (achado P3-3 herdr-9: setar via os.environ direto vazava pro resto
        # do processo de teste, sem contrapartida).
        self._env_patch = patch.dict(os.environ, {"HERDR_ENV": "1"})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self.tmpdir.cleanup()

    def _agent_info(self, status):
        return {
            "agent_status": status, "cwd": self.cwd, "foreground_cwd": self.cwd,
            "pane_id": "p1", "workspace_id": "w1", "tab_id": "t1",
            "agent": "codex", "interactive_ready": True,
            "agent_session": {"value": "s1"},
        }

    def test_main_writes_provenance_marker_before_rename_and_clears_on_success(self):
        # Achado P1-2 herdr-15 (herdr-rev): o marcador de proveniência que o
        # self-heal exige (agent_session do '{rev_name}' antigo) precisa ser
        # gravado pelo CAMINHO NORMAL, ANTES de qualquer tentativa de rename
        # - é o que dá ao self-heal algo real pra comparar depois de um
        # crash. Prova que ele existe no disco durante a chamada de rename
        # (não só depois) e que é limpo (None) quando a migração conclui.
        before_info = self._agent_info("idle")
        observed = {}
        state_holder = {"renamed": False}

        def fake_get_agent(name):
            if name == "foo-rev":
                if state_holder["renamed"]:
                    raise RuntimeError("agent target foo-rev: agent_not_found")
                return before_info
            if name == "foo-rev-1":
                if not state_holder["renamed"]:
                    raise RuntimeError("agent target foo-rev-1: agent_not_found")
                return before_info  # rename não muda nenhum campo, só o nome
            if name == "foo-rev-2":
                return {"agent": "grok", "agent_status": "idle"}
            raise RuntimeError(f"agent target {name}: agent_not_found")

        def fake_api(*args):
            if args[:2] == ("agent", "rename"):
                observed["pending_rename_session_during_rename"] = self.migrate.migration.read_migration_state(self.cwd).get("pending_rename_session")
                state_holder["renamed"] = True
            return {}

        # Diferente do self-heal (que chama sys.exit(0) explicitamente), o
        # caminho normal de sucesso em `_reinforce_and_finalize` só retorna
        # -- `main()` não levanta SystemExit quando tudo dá certo.
        with patch.object(self.migrate, "get_agent", side_effect=fake_get_agent), \
             patch.object(self.migrate, "api", side_effect=fake_api), \
             patch.object(self.migrate.core, "get_agent_info", side_effect=fake_get_agent), \
             patch.object(sys, "argv", ["herdr-migrate-rev", "foo"]):
            self.migrate.main()

        self.assertEqual(observed.get("pending_rename_session_during_rename"), "s1", "marcador precisa existir ANTES/DURANTE o rename, não só depois")

        state = self.migrate.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "migrated")
        self.assertIsNone(state["pending_rename_session"], "marcador precisa ser limpo após a migração concluir com sucesso")

    def test_give_up_restores_prior_phase_not_legacy(self):
        self.migrate.migration.write_migration_state_atomic(self.cwd, {"phase": "migrated", "rev2_kind": "grok"})
        # <slug>-rev "working" força o caminho de desistência dentro de _run.
        with patch.object(self.migrate, "get_agent", return_value=self._agent_info("working")):
            with self.assertRaises(SystemExit):
                with patch.object(sys, "argv", ["herdr-migrate-rev", "foo"]):
                    self.migrate.main()

        state = self.migrate.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "migrated", "desistir não pode rebaixar um space já migrado pra legacy")
        self.assertEqual(state["rev2_kind"], "grok")

    def test_self_heal_writes_migrating_before_attempting_reinforcement(self):
        # Achado P1-1 herdr-11 (os dois revisores, independentemente): o
        # self-heal adquiria o lock mas não gravava a marca durável
        # phase=migrating antes de tentar o reforço de papel (a chamada mais
        # demorada do fluxo) - um crash ali deixava o space com a phase
        # ANTIGA (ex: legacy) e nenhum gate, mesmo com o lock corretamente
        # adquirido antes. Prova isto observando a phase no disco durante a
        # própria chamada de reforço, não só o resultado final.
        #
        # phase inicial é "migrating" com pending_rename_session batendo com
        # o agent_session que _agent_info("idle") devolve pra foo-rev-1 -
        # achado P1-2 herdr-15: sem o marcador de proveniência (que só existe
        # quando a phase observada é "migrating"), o self-heal recusa
        # reconciliar sozinho antes mesmo de chegar no reforço de papel.
        self.migrate.migration.write_migration_state_atomic(self.cwd, {"phase": "migrating", "rev2_kind": "grok", "pending_rename_session": "s1"})
        observed = {}

        def fake_get_agent(name):
            # Também serve de side_effect pra core.get_agent_info (usado por
            # agent_status_safe/agent_status_or_raise) - qualquer nome não
            # listado precisa "não existir" de verdade, não estourar
            # AssertionError, já que _reinforce_and_finalize consulta
            # "{slug}-exec" pra montar a lista de siblings.
            if name == "foo-rev":
                raise RuntimeError("agent target foo-rev: agent_not_found")
            if name == "foo-rev-1":
                return self._agent_info("idle")
            if name == "foo-rev-2":
                return {"agent": "grok", "agent_status": "idle"}
            raise RuntimeError(f"agent target {name}: agent_not_found")

        def fake_api(*args):
            if args[:2] == ("agent", "prompt"):
                observed["phase_during_reinforcement"] = self.migrate.migration.read_migration_state(self.cwd)["phase"]
            return {}

        # Achado P2-2 herdr-12: agent_status_or_raise chama
        # core.agent_status -> core.get_agent_info, não agent_status_safe
        # (que já não é usado neste caminho) - mockar o alias errado deixa a
        # chamada real escapar pro binário `herdr`. Reusa fake_get_agent:
        # mesma forma (name) -> info/raise que a rechecagem sob o lock
        # precisa pra "foo-rev".
        with patch.object(self.migrate, "get_agent", side_effect=fake_get_agent), \
             patch.object(self.migrate, "api", side_effect=fake_api), \
             patch.object(self.migrate.core, "get_agent_info", side_effect=fake_get_agent):
            with self.assertRaises(SystemExit):
                with patch.object(sys, "argv", ["herdr-migrate-rev", "foo"]):
                    self.migrate.main()

        self.assertEqual(observed.get("phase_during_reinforcement"), "migrating", "a marca durável precisa existir ANTES da chamada mais demorada do fluxo")

    def test_main_self_heals_when_old_name_gone_and_phase_stuck_migrating(self):
        # Achado P1-4/P2-2 herdr-9: crash entre o rename e o write final
        # deixava phase=migrating travada, e a próxima execução falhava no
        # lookup do nome ANTIGO antes mesmo de consultar a phase - sem
        # diagnóstico nem recuperação automática do caso "rename já tinha
        # aplicado, só faltou registrar".
        #
        # pending_rename_session bate com o agent_session de _agent_info -
        # achado P1-2 herdr-15: sem esse marcador de proveniência, o
        # self-heal recusa reconciliar mesmo com tudo consistente.
        self.migrate.migration.write_migration_state_atomic(self.cwd, {"phase": "migrating", "rev2_kind": "grok", "pending_rename_session": "s1"})

        def fake_get_agent(name):
            if name == "foo-rev":
                raise RuntimeError("agent target foo-rev: agent_not_found")
            if name == "foo-rev-1":
                return self._agent_info("idle")
            if name == "foo-rev-2":
                return {"agent": "grok", "agent_status": "idle"}
            raise RuntimeError(f"agent target {name}: agent_not_found")

        with patch.object(self.migrate, "get_agent", side_effect=fake_get_agent), \
             patch.object(self.migrate, "api", return_value={}), \
             patch.object(self.migrate.core, "get_agent_info", side_effect=fake_get_agent):
            with self.assertRaises(SystemExit) as ctx:
                with patch.object(sys, "argv", ["herdr-migrate-rev", "foo"]):
                    self.migrate.main()
        self.assertEqual(ctx.exception.code, 0, "self-heal bem-sucedido deveria sair com 0, não erro")

        state = self.migrate.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "migrated")
        self.assertEqual(state["rev2_kind"], "grok")
        self.assertIsNone(state["pending_rename_session"], "marcador de proveniência precisa ser limpo após concluir")

    def test_self_heal_refuses_legacy_phase_without_provenance_marker(self):
        # Achado P1-2 herdr-15 (herdr-rev), o requisito central da rodada:
        # phase="legacy" (ou qualquer coisa que não seja "migrating") com o
        # nome antigo ausente e '{rev1_name}' presente é EXATAMENTE o cenário
        # que o achado herdr-9 original mandava reconciliar automaticamente
        # - mas sem uma migração real ter começado por este mecanismo, não
        # existe (e nunca existiu) marcador de proveniência. Mesmo com tudo
        # consistente (idle, interactive_ready), o self-heal agora recusa e
        # exige intervenção manual em vez de aceitar por semelhança.
        self.migrate.migration.write_migration_state_atomic(self.cwd, {"phase": "legacy", "rev2_kind": "grok"})

        def fake_get_agent(name):
            if name == "foo-rev":
                raise RuntimeError("agent target foo-rev: agent_not_found")
            if name == "foo-rev-1":
                return self._agent_info("idle")
            raise RuntimeError(f"agent target {name}: agent_not_found")

        with patch.object(self.migrate, "get_agent", side_effect=fake_get_agent), \
             patch.object(self.migrate.core, "get_agent_info", side_effect=fake_get_agent):
            with self.assertRaises(SystemExit) as ctx:
                with patch.object(sys, "argv", ["herdr-migrate-rev", "foo"]):
                    self.migrate.main()
        self.assertEqual(ctx.exception.code, 1)

        state = self.migrate.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "pending-manual", "sem marcador de proveniência, self-heal não pode reconciliar sozinho")
        self.assertIn("proveniência", state["note"])

    def test_self_heal_refuses_when_session_marker_mismatches(self):
        # Achado P1-2 herdr-15: mesmo com phase="migrating" e um marcador
        # presente, se o agent_session observado agora não bater com o que
        # foi persistido ANTES do rename, não é prova de que este '{rev1_name}'
        # veio daquela migração - pode ser uma reocupação com metadata
        # coincidente ou um marcador de uma tentativa completamente diferente.
        self.migrate.migration.write_migration_state_atomic(self.cwd, {"phase": "migrating", "rev2_kind": "grok", "pending_rename_session": "sessao-de-outra-tentativa"})

        def fake_get_agent(name):
            if name == "foo-rev":
                raise RuntimeError("agent target foo-rev: agent_not_found")
            if name == "foo-rev-1":
                return self._agent_info("idle")  # agent_session = "s1", não bate
            raise RuntimeError(f"agent target {name}: agent_not_found")

        with patch.object(self.migrate, "get_agent", side_effect=fake_get_agent), \
             patch.object(self.migrate.core, "get_agent_info", side_effect=fake_get_agent):
            with self.assertRaises(SystemExit) as ctx:
                with patch.object(sys, "argv", ["herdr-migrate-rev", "foo"]):
                    self.migrate.main()
        self.assertEqual(ctx.exception.code, 1)

        state = self.migrate.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "pending-manual")
        self.assertIn("não bate", state["note"])

    def test_self_heal_detects_rev1_reoccupied_between_snapshots(self):
        # Achado P1-3 herdr-14 (herdr-rev): a versão anterior não provava que
        # o '{rev1_name}' visto SOB O LOCK é o mesmo agent observado na
        # leitura inicial (antes do lock) - só checava interactive_ready e
        # agent_status em isolamento. Simula reocupação: pane_id muda entre
        # as duas consultas, ambas idle/interactive_ready=True.
        #
        # pending_rename_session bate com o agent_session que as duas
        # leituras devolvem (só o pane_id diverge) - isso prova que a
        # checagem de proveniência (achado P1-2 herdr-15) e a comparação
        # FIELDS_TO_MATCH (achado P1-3 herdr-14) são defesas INDEPENDENTES:
        # aqui a proveniência passa, mas a reocupação ainda é pega pela
        # segunda checagem.
        self.migrate.migration.write_migration_state_atomic(self.cwd, {"phase": "migrating", "rev2_kind": "grok", "pending_rename_session": "s1"})
        calls = {"n": 0}

        def fake_get_agent(name):
            if name == "foo-rev":
                raise RuntimeError("agent target foo-rev: agent_not_found")
            if name == "foo-rev-1":
                calls["n"] += 1
                info = self._agent_info("idle")
                info["pane_id"] = "p1" if calls["n"] == 1 else "p2-reocupado"
                return info
            raise RuntimeError(f"agent target {name}: agent_not_found")

        with patch.object(self.migrate, "get_agent", side_effect=fake_get_agent), \
             patch.object(self.migrate.core, "get_agent_info", side_effect=fake_get_agent):
            with self.assertRaises(SystemExit) as ctx:
                with patch.object(sys, "argv", ["herdr-migrate-rev", "foo"]):
                    self.migrate.main()
        self.assertEqual(ctx.exception.code, 1)

        state = self.migrate.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "pending-manual", "identidade divergente entre leitura inicial e sob o lock não pode ser finalizada silenciosamente")
        self.assertIn("pane_id", state["note"])

    def test_self_heal_infra_failure_keeps_phase_migrating_not_observed_phase(self):
        # Achado P1-1 herdr-13 (herdr-rev): a versão anterior restaurava
        # `observed_phase` quando a rechecagem de ausência sob o lock falhava
        # por infra — mas essa fase pode ser "legacy", que o gate do worker
        # trata como livre, reabrindo o space pra uso durante um estado ainda
        # não confirmado. Agora deve permanecer "migrating" (bloqueado).
        self.migrate.migration.write_migration_state_atomic(self.cwd, {"phase": "legacy", "rev2_kind": "grok"})

        def fake_get_agent(name):
            if name == "foo-rev-1":
                return self._agent_info("idle")
            raise RuntimeError(f"agent target {name}: agent_not_found")

        def fake_agent_status_or_raise(name):
            if name == "foo-rev":
                raise RuntimeError("sem resposta em 30s")
            raise AssertionError(f"agent_status_or_raise inesperado: {name!r}")

        with patch.object(self.migrate, "get_agent", side_effect=fake_get_agent), \
             patch.object(self.migrate.migration, "agent_status_or_raise", side_effect=fake_agent_status_or_raise):
            with self.assertRaises(SystemExit) as ctx:
                with patch.object(sys, "argv", ["herdr-migrate-rev", "foo"]):
                    self.migrate.main()
        self.assertEqual(ctx.exception.code, 2)

        state = self.migrate.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "migrating", "falha de infra na rechecagem não pode reabrir uma fase livre como legacy")

    def test_self_heal_requires_idle_or_done_not_only_interactive_ready(self):
        # Achado P1-2 herdr-13 (herdr-rev): o caminho normal já exige
        # agent_status idle/done antes de agir; o self-heal só checava
        # interactive_ready, permitindo finalizar mesmo com o rev-1 ainda
        # "working" (ou reocupado por outro pane).
        #
        # pending_rename_session precisa bater (achado P1-2 herdr-15) pra
        # este teste continuar exercitando a checagem de agent_status, e não
        # ser rejeitado antes por falta de proveniência (o que passaria pela
        # razão errada).
        self.migrate.migration.write_migration_state_atomic(self.cwd, {"phase": "migrating", "rev2_kind": "grok", "pending_rename_session": "s1"})

        def fake_get_agent(name):
            if name == "foo-rev":
                raise RuntimeError("agent target foo-rev: agent_not_found")
            if name == "foo-rev-1":
                return self._agent_info("working")
            raise RuntimeError(f"agent target {name}: agent_not_found")

        with patch.object(self.migrate, "get_agent", side_effect=fake_get_agent), \
             patch.object(self.migrate.core, "get_agent_info", side_effect=fake_get_agent):
            with self.assertRaises(SystemExit) as ctx:
                with patch.object(sys, "argv", ["herdr-migrate-rev", "foo"]):
                    self.migrate.main()
        self.assertEqual(ctx.exception.code, 1)

        state = self.migrate.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "pending-manual", "rev-1 'working' não pode ser finalizado como migrated silenciosamente")

    def test_self_heal_refuses_when_lock_held_by_an_active_process(self):
        # Achado P1-1 herdr-10 (os dois revisores, independentemente): a
        # assinatura "rev sumiu, rev-1 existe, phase=migrating" é IDÊNTICA
        # entre um crash real e uma migração legítima ainda em andamento
        # (entre o rename e o reforço de papel). Só o lock distingue os
        # dois. Simula o segundo caso segurando o lock nós mesmos, como se
        # fôssemos o processo legítimo ainda trabalhando.
        self.migrate.migration.write_migration_state_atomic(self.cwd, {"phase": "migrating", "rev2_kind": "grok"})
        held_lock = self.migrate.migration.acquire_migration_lock(self.cwd)
        self.assertIsNotNone(held_lock, "pré-condição do teste: o lock precisa estar livre pra eu conseguir segurá-lo")

        def fake_get_agent(name):
            if name == "foo-rev":
                raise RuntimeError("agent target foo-rev: agent_not_found")
            if name == "foo-rev-1":
                return self._agent_info("working")  # processo legítimo ainda mexendo nele
            raise AssertionError(f"get_agent inesperado: {name!r}")

        try:
            with patch.object(self.migrate, "get_agent", side_effect=fake_get_agent):
                with self.assertRaises(SystemExit) as ctx:
                    with patch.object(sys, "argv", ["herdr-migrate-rev", "foo"]):
                        self.migrate.main()
            self.assertNotEqual(ctx.exception.code, 0, "não deveria declarar sucesso enquanto outro processo detém o lock")
        finally:
            self.migrate.migration.release_migration_lock(held_lock)

        # Não deveria ter escrito NADA - a phase continua exatamente como
        # o processo legítimo (simulado) a deixou.
        state = self.migrate.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "migrating")
        self.assertEqual(state["rev2_kind"], "grok")

    def test_old_name_gone_and_phase_pending_manual_reports_not_nothing_to_do(self):
        # Achado P2-1 herdr-10: a versão anterior classificava QUALQUER
        # phase != "migrating" (inclusive pending-manual) como "já migrado,
        # nada a fazer" com exit 0 - contradizendo os outros 4 comandos, que
        # recusam pending-manual explicitamente com a nota gravada.
        self.migrate.migration.write_migration_state_atomic(self.cwd, {"phase": "pending-manual", "note": "motivo especifico"})

        def fake_get_agent(name):
            if name == "foo-rev":
                raise RuntimeError("agent target foo-rev: agent_not_found")
            if name == "foo-rev-1":
                return self._agent_info("idle")
            raise AssertionError(f"get_agent inesperado: {name!r}")

        with patch.object(self.migrate, "get_agent", side_effect=fake_get_agent):
            with self.assertRaises(SystemExit) as ctx:
                with patch.object(sys, "argv", ["herdr-migrate-rev", "foo"]):
                    self.migrate.main()
        self.assertNotEqual(ctx.exception.code, 0, "pending-manual não é 'nada a fazer' - precisa de intervenção humana")

        state = self.migrate.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "pending-manual", "não deveria ter mexido no estado")

    def test_old_name_gone_and_phase_migrated_is_a_true_noop(self):
        self.migrate.migration.write_migration_state_atomic(self.cwd, {"phase": "migrated", "rev2_kind": "claude"})

        def fake_get_agent(name):
            if name == "foo-rev":
                raise RuntimeError("agent target foo-rev: agent_not_found")
            if name == "foo-rev-1":
                return self._agent_info("idle")
            raise AssertionError(f"get_agent inesperado: {name!r}")

        with patch.object(self.migrate, "get_agent", side_effect=fake_get_agent):
            with self.assertRaises(SystemExit) as ctx:
                with patch.object(sys, "argv", ["herdr-migrate-rev", "foo"]):
                    self.migrate.main()
        self.assertEqual(ctx.exception.code, 0)

        state = self.migrate.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "migrated")
        self.assertEqual(state["rev2_kind"], "claude", "não deveria ter reescrito nada")

    def test_self_heal_refuses_when_rev1_not_interactive_ready(self):
        # Achado P2-2 herdr-10: o self-heal não pode exigir menos do que o
        # caminho normal exige pra declarar "migrated" - o caminho normal
        # confere interactive_ready via FIELDS_TO_MATCH.
        #
        # pending_rename_session precisa bater (achado P1-2 herdr-15) pra
        # este teste continuar exercitando a checagem de interactive_ready,
        # e não ser rejeitado antes por falta de proveniência.
        self.migrate.migration.write_migration_state_atomic(self.cwd, {"phase": "migrating", "rev2_kind": "grok", "pending_rename_session": "s1"})

        def fake_get_agent(name):
            if name == "foo-rev":
                raise RuntimeError("agent target foo-rev: agent_not_found")
            if name == "foo-rev-1":
                info = self._agent_info("idle")
                info["interactive_ready"] = False
                return info
            raise RuntimeError(f"agent target {name}: agent_not_found")

        with patch.object(self.migrate, "get_agent", side_effect=fake_get_agent), \
             patch.object(self.migrate.core, "get_agent_info", side_effect=fake_get_agent):
            with self.assertRaises(SystemExit) as ctx:
                with patch.object(sys, "argv", ["herdr-migrate-rev", "foo"]):
                    self.migrate.main()
        self.assertNotEqual(ctx.exception.code, 0)

        state = self.migrate.migration.read_migration_state(self.cwd)
        self.assertEqual(state["phase"], "pending-manual")


if __name__ == "__main__":
    unittest.main()
