"""herdr-agent-restart: perfil (sonda > argv > matriz), comando de retomada e guardas."""
import datetime
import importlib.util
import os
import sys
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)
import _herdr_dispatch as core  # noqa: E402


def _load():
    loader = SourceFileLoader("herdr_agent_restart_test", os.path.join(BIN_DIR, "herdr-agent-restart"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _entry(model, effort, origem="thread-confirmado", kind="claude", min_atras=30):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=min_atras)
    return {"model": model, "effort": effort, "origem": origem, "kind": kind, "visto_em": t.isoformat(timespec="seconds")}


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def test_parse_argv_claude_e_codex(self):
        self.assertEqual(self.m.parse_argv(["claude", "--model", "claude-sonnet-5-5", "--effort", "low"]),
                         ("claude-sonnet-5-5", "low"))
        self.assertEqual(self.m.parse_argv(["codex", "resume", "x", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high"]),
                         ("gpt-6.1-sol", "high"))
        self.assertEqual(self.m.parse_argv(["claude", "--resume", "x"]), (None, None))

    def test_sonda_recente_codex_vence_o_argv(self):
        r = self.m.resolve_profile("x-rev-1", "codex", ["codex", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=xhigh"],
                                   _entry("gpt-6.1-sol", "low", "pane", "codex"), None)
        self.assertEqual(r[:2], ("gpt-6.1-sol", "low"))
        self.assertIn("sonda", r[2])

    def test_claude_modelo_da_sonda_mas_esforco_fixado_no_argv(self):
        argv = ["claude", "--model", "claude-opus-5-5", "--effort", "low", "--resume", "s"]
        r = self.m.resolve_profile("x-exec", "claude", argv, _entry("claude-sonnet-5-5", "high"), None)
        self.assertEqual(r[:2], ("claude-sonnet-5-5", "low"))
        self.assertIn("argv", r[2])

    def test_claude_sem_flags_usa_o_esforco_da_sonda(self):
        r = self.m.resolve_profile("x-exec", "claude", ["claude", "--resume", "s"], _entry("claude-sonnet-5-5", "high"), None)
        self.assertEqual(r[:2], ("claude-sonnet-5-5", "high"))

    def test_sonda_velha_demais_cai_para_argv(self):
        argv = ["claude", "--model", "claude-opus-5-5", "--effort", "xhigh"]
        r = self.m.resolve_profile("x-exec", "claude", argv, _entry("claude-sonnet-5-5", "high", min_atras=60 * 72), None)
        self.assertEqual(r[:2], ("claude-opus-5-5", "xhigh"))
        self.assertEqual(r[2], "argv do processo vivo")

    def test_sonda_de_origem_fraca_nao_decide(self):
        r = self.m.resolve_profile("x-exec", "claude", ["claude"], _entry("claude-sonnet-5-5", "high", "thread"), None)
        self.assertEqual(r[2], "matriz do bootstrap")

    def test_sem_nada_usa_a_matriz_do_papel(self):
        self.assertEqual(self.m.resolve_profile("x-exec", "claude", [], None, None)[:2], ("claude-sonnet-5-5", "low"))
        self.assertEqual(self.m.resolve_profile("x-rev-1", "codex", [], None, None)[:2], ("gpt-6.1-sol", "low"))
        self.assertEqual(self.m.resolve_profile("x-scout", "codex", [], None, None)[:2], ("gpt-6.1-sol", "xhigh"))
        self.assertEqual(self.m.resolve_profile("x-rev-2", "claude", [], None, None)[:2], ("claude-opus-5-5", "high"))

    def test_modelo_morto_opus5_nunca_e_usado(self):
        r = self.m.resolve_profile("x-exec", "claude", [], _entry("opus-5", "low"), None)
        self.assertNotEqual(r[0], "opus-5")

    def test_explicito_vence_tudo_e_e_validado(self):
        r = self.m.resolve_profile("x-exec", "claude", [], _entry("claude-sonnet-5-5", "high"), None, "claude-opus-5-5", "max")
        self.assertEqual(r[:2], ("claude-opus-5-5", "max"))
        with self.assertRaises(SystemExit):
            self.m.resolve_profile("x-exec", "claude", [], None, None, "opus-5", "low")

    def test_agente_sem_papel_nem_perfil_pede_flags(self):
        with self.assertRaises(SystemExit):
            self.m.resolve_profile("avulso", "claude", [], None, None)


class StartArgsTests(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def test_claude(self):
        self.assertEqual(self.m.start_args("claude", "S", "claude-sonnet-5-5", "low"),
                         ["--resume", "S", "--model", "claude-sonnet-5-5", "--effort", "low"])

    def test_codex_troca_modelo_e_esforco_e_mantem_o_resto(self):
        old = ["codex", "resume", "OLD", "-m", "gpt-6-sol", "-c", "model_reasoning_effort=xhigh", "-c", "agents.max_depth=0", "--no-daemon"]
        a = self.m.start_args("codex", "S", "gpt-6.1-sol", "low", old)
        self.assertEqual(a[:6], ["resume", "S", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=low"])
        self.assertIn("agents.max_depth=0", a)
        self.assertIn("--no-daemon", a)
        self.assertNotIn("gpt-6-sol", a)
        self.assertEqual(sum(1 for x in a if x.startswith("model_reasoning_effort=")), 1)

    def test_codex_sem_historico_liga_o_depth_zero(self):
        self.assertIn("agents.max_depth=0", self.m.start_args("codex", "S", "gpt-6.1-sol", "low", []))


class TargetAndSafetyTests(unittest.TestCase):
    def setUp(self):
        self.m = _load()
        self.lista = [{"name": "a-exec", "pane_id": "w1:p1", "cwd": "/tmp"}, {"name": "a-rev-1", "pane_id": "w1:p2", "cwd": "/tmp"}]

    def test_acha_por_nome_e_por_pane(self):
        self.assertEqual(self.m.achar_alvo("a-rev-1", self.lista)["pane_id"], "w1:p2")
        self.assertEqual(self.m.achar_alvo("w1:p1", self.lista)["name"], "a-exec")

    def test_alvo_inexistente_aborta(self):
        with self.assertRaises(SystemExit):
            self.m.achar_alvo("nada", self.lista)

    def test_sem_argumento_pega_o_unico_exec_do_projeto(self):
        with mock.patch.object(core, "project_root", return_value="/tmp"), mock.patch.object(os, "getcwd", return_value="/tmp"):
            self.assertEqual(self.m.achar_alvo(None, self.lista)["name"], "a-exec")

    def test_sessao_do_registro_ou_do_argv(self):
        self.assertEqual(self.m.sessao_de({"agent_session": {"value": "X"}}, []), "X")
        self.assertEqual(self.m.sessao_de({"agent_session": None}, ["claude", "--resume", "Y"]), "Y")
        self.assertEqual(self.m.sessao_de({"agent_session": None}, ["codex", "resume", "Z"]), "Z")
        self.assertIsNone(self.m.sessao_de({"agent_session": None}, ["claude"]))

    def _seg(self, status, comp):
        with mock.patch.object(core, "effective_status", return_value=status), \
             mock.patch.object(core, "classify_composition", return_value=(comp, "texto", "")):
            return self.m.checar_seguranca({"pane_id": "w1:p1"})

    def test_ocioso_com_caixa_vazia_ou_sugestao_pode(self):
        for comp in ("vazia", "placeholder", "sugestao"):
            self.assertEqual(self._seg("idle", comp), [], comp)

    def test_recusa_working_rascunho_dialogo_e_leitura_falha(self):
        self.assertTrue(self._seg("working", "vazia"))
        self.assertTrue(self._seg("idle", "humano"))
        self.assertTrue(self._seg("idle", "dialogo"))
        self.assertTrue(self._seg("idle", "indeterminado"))
        self.assertTrue(self._seg("unknown", "vazia"))


class LedgerTests(unittest.TestCase):
    """Agent que SAIU (/exit, queda): o Herdr apaga o registro; o ledger da sonda guarda o ultimo estado."""

    def setUp(self):
        import tempfile
        import _herdr_probe_cache as pc
        self.pc = pc
        self.m = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "last-agents.json")
        pc.gravar({"x-exec": {"pane": "w1:p1", "kind": "claude", "cwd": "/p", "session": "SID", "visto_em": "t"}}, self.path)
        patcher = mock.patch.object(pc, "LEDGER_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _panes(self, ids):
        import json
        out = json.dumps({"result": {"panes": [{"pane_id": i} for i in ids]}})
        return mock.patch.object(self.m.subprocess, "run", return_value=mock.Mock(stdout=out, returncode=0))

    def test_ledger_atualiza_guarda_so_nomeados_e_nao_apaga_a_sessao(self):
        led = self.pc.ledger_atualiza({}, [{"name": "a-exec", "pane_id": "w1:p1", "agent": "claude", "cwd": "/p",
                                            "agent_session": {"value": "S1"}}, {"name": None, "pane_id": "w1:p9"}])
        self.assertEqual(list(led), ["a-exec"])
        led = self.pc.ledger_atualiza(led, [{"name": "a-exec", "pane_id": "w1:p1", "agent": "claude", "cwd": "/p",
                                             "agent_session": None}])
        self.assertEqual(led["a-exec"]["session"], "S1")

    def test_orfao_com_pane_vazio_e_recuperavel(self):
        with self._panes(["w1:p1"]):
            o = self.m.agente_orfao("x-exec", [])
        self.assertEqual((o["pane_id"], o["agent"], o["agent_session"]["value"]), ("w1:p1", "claude", "SID"))
        self.assertTrue(o["_orfao"])

    def test_orfao_nao_vale_se_o_pane_tem_outro_agent_vivo(self):
        with self._panes(["w1:p1"]):
            self.assertIsNone(self.m.agente_orfao("x-exec", [{"pane_id": "w1:p1", "name": "outro"}]))

    def test_orfao_nao_vale_se_o_pane_nao_existe_mais(self):
        with self._panes(["w1:p7"]):
            self.assertIsNone(self.m.agente_orfao("x-exec", []))

    def test_nome_fora_do_ledger_ou_sem_sessao(self):
        with self._panes(["w1:p1"]):
            self.assertIsNone(self.m.agente_orfao("desconhecido", []))
        self.pc.gravar({"y-exec": {"pane": "w1:p1", "kind": "claude", "session": None}}, self.path)
        with self._panes(["w1:p1"]):
            self.assertIsNone(self.m.agente_orfao("y-exec", []))

    def test_achar_alvo_cai_no_orfao_antes_de_desistir(self):
        with self._panes(["w1:p1"]):
            self.assertTrue(self.m.achar_alvo("x-exec", [])["_orfao"])
        with self._panes([]), self.assertRaises(SystemExit):
            self.m.achar_alvo("x-exec", [])


if __name__ == "__main__":
    unittest.main()

class CodexLaunchContractTests(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def args(self, argv):
        return self.m.start_args('codex', 'NEW', 'gpt-6.1-sol', 'low', argv)

    def test_bia_sandbox_cwd_and_search_values_survive(self):
        argv=['codex','-s','workspace-write','-C','/home/brenoperucchi/Devs/paperclip/agents/bia',
              '--search','-c','agents.max_depth=0','--no-daemon']
        self.assertEqual(self.args(argv),['resume','NEW','-m','gpt-6.1-sol','-c','model_reasoning_effort=low',
                         *argv[1:]])

    def test_global_flags_before_resume_and_old_prompt_not_replayed(self):
        a=self.args(['/usr/local/bin/codex','-s','workspace-write','-C','/root',
                     'resume','OLD','--search','old prompt'])
        self.assertIn('workspace-write',a);self.assertIn('/root',a);self.assertIn('--search',a)
        self.assertNotIn('OLD',a);self.assertNotIn('old prompt',a)

    def test_config_value_resume_is_not_a_subcommand(self):
        a=self.args(['codex','-p','resume','-c','key="value with = and spaces"','initial prompt'])
        self.assertIn('resume',a[6:]);self.assertIn('key="value with = and spaces"',a)
        self.assertNotIn('initial prompt',a)

    def test_long_equal_and_attached_short_forms_preserve_values(self):
        a=self.args(['codex','--model=old','--config=model_reasoning_effort="max"',
                     '-sworkspace-write','--cd=/root','--config','other=1'])
        self.assertNotIn('--model=old',a)
        self.assertNotIn('--config=model_reasoning_effort="max"',a)
        self.assertIn('-sworkspace-write',a);self.assertIn('--cd=/root',a);self.assertIn('other=1',a)

    def test_old_model_config_is_removed_as_well(self):
        a=self.args(['codex','-c','model="old"','-c','model_reasoning_effort = "max"'])
        self.assertNotIn('model="old"',a)
        self.assertEqual(sum(x.startswith('model_reasoning_effort=') for x in a),1)

    def test_attachments_selectors_and_delimited_prompt_not_replayed(self):
        a=self.args(['codex','resume','--last','--all','--include-non-interactive',
                     '-i','a.png','b.png','old prompt','--search','--','--unknown prompt'])
        for unwanted in ('--last','--all','--include-non-interactive','a.png','b.png','old prompt','--unknown prompt'):
            self.assertNotIn(unwanted,a)
        self.assertIn('--search',a)

    def test_normalization_is_idempotent(self):
        first=self.args(['codex','-s','workspace-write','-C','/root','--search'])
        self.assertEqual(self.args(first),first)

    def test_unsupported_or_incomplete_contract_is_refused(self):
        for argv in (['node','wrapper.js'],['codex','exec','echo'],['codex','--future'],
                     ['codex','--worktree'],['codex','-C'],['codex','--config','broken']):
            with self.subTest(argv=argv),self.assertRaises(SystemExit): self.args(argv)

    def test_invalid_contract_never_stops_old_process(self):
        info={'pane_id':'w:p1','name':'acervo-bia','cwd':'/root','foreground_cwd':'/root'}
        for argv in (['codex','--future'],['codex','-C','relative'],['codex','-C','/another']):
            with self.subTest(argv=argv),mock.patch.object(self.m,'encerrar') as stop, \
                 mock.patch.object(self.m.subprocess,'run') as run:
                # Avoid invoking git inside the pure failure-path assertion.
                with mock.patch.object(core,'project_root',side_effect=lambda p:p):
                    ok,message=self.m.reiniciar(info,'codex','S','gpt-6.1-sol','low',123,argv)
                self.assertFalse(ok);self.assertIn('antes de encerrar',message)
                stop.assert_not_called();run.assert_not_called()
