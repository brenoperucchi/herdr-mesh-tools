"""Testes do herdr-agents (leitura de model/effort REAL do processo).

O script existe na forma atual por causa do achado de 2026-09-09: a coluna
antiga chamada MODEL mostrava o *kind* (claude/codex), nao o modelo. Isso
escondeu por dias que dois revisores rodavam Fable 5.1 -- o modelo mais caro do
catalogo, o dobro do Opus pretendido -- porque tinham subido sem `--model` e
herdaram um default que o `/model` interativo havia reescrito.

Dai a distincao que estes testes travam: PINADO (veio no argv do processo) vs
HERDADO (entre parenteses, veio do config global e muda sozinho).
"""
import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load():
    import sys
    if BIN_DIR not in sys.path:
        sys.path.insert(0, BIN_DIR)
    loader = SourceFileLoader("herdr_agents_test", os.path.join(BIN_DIR, "herdr-agents"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class FromArgvTests(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def test_claude_style_flags(self):
        argv = ["claude", "--model", "opus", "--effort", "high"]
        self.assertEqual(self.m.from_argv(argv), ("opus", "high"))

    def test_codex_style_effort_comes_from_dash_c(self):
        """Codex nao usa --effort: o esforco vem como `-c
        model_reasoning_effort=<v>`. Ler so --effort perderia todo o lado Codex."""
        argv = ["codex", "--model", "gpt-5.6-terra", "-c", "model_reasoning_effort=max",
                "-c", "agents.max_depth=0"]
        self.assertEqual(self.m.from_argv(argv), ("gpt-5.6-terra", "max"))

    def test_bare_launch_reports_nothing_pinned(self):
        """Processo relancado sem flags (o que o reboot de 2026-09-09 fez com
        todo mundo) tem que aparecer como NAO pinado, pra cair no ramo de
        'herdado' e ser exibido entre parenteses."""
        self.assertEqual(self.m.from_argv(["codex"]), (None, None))


class ClaudeDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def _settings(self, data):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".claude"))
        with open(os.path.join(d, ".claude", "settings.json"), "w") as f:
            json.dump(data, f)
        return d

    def test_exact_key_wins_over_substring(self):
        """`model` vem como "opus[1m]"; a chave canonica e' "claude-opus-5".
        Casar por substring sem prioridade deixaria a ordem do dict decidir em
        silencio qual effortLevel vence."""
        home = self._settings({
            "model": "opus[1m]",
            "modelSettings": {
                "claude-sonnet-5": {"effortLevel": "xhigh"},
                "claude-opus-5": {"effortLevel": "high"},
            },
        })
        with mock.patch.object(self.m, "HOME", home):
            model, effort = self.m.defaults_claude()
        self.assertEqual(model, "opus[1m]")
        self.assertEqual(effort, "high")

    def test_ambiguous_substring_returns_no_effort_instead_of_guessing(self):
        """Dois candidatos por substring e nenhum exato: melhor devolver None
        (aparece como '?') do que escolher um e mentir com confianca."""
        home = self._settings({
            "model": "foo",
            "modelSettings": {"a-foo-1": {"effortLevel": "low"}, "b-foo-2": {"effortLevel": "max"}},
        })
        with mock.patch.object(self.m, "HOME", home):
            _model, effort = self.m.defaults_claude()
        self.assertIsNone(effort)


class CodexOciosoPelaTelaTests(unittest.TestCase):
    """Herdr >= 0.9.2 deixa todo Codex `unknown` depois de um turno (#4563). O
    listing infere `idle?` so quando a caixa esta vazia e nao ha marcador de
    trabalho -- e na duvida NAO infere."""

    def setUp(self):
        self.m = _load()

    def test_caixa_vazia_sem_trabalho_e_ocioso(self):
        tela = "Worked for 3m 0s • 7:06 PM\n› Ask Codex to do anything\n  gpt-6.1-sol high · ~/x\n  ? for shortcuts"
        self.assertTrue(self.m.codex_ocioso_pela_tela(tela))

    def test_timer_vivo_com_dica_de_interrupcao_nao_e_ocioso(self):
        tela = "• Working (12s • esc to interrupt)\n› Ask Codex to do anything\n  gpt-6.1-sol high · ~/x"
        self.assertFalse(self.m.codex_ocioso_pela_tela(tela))

    def test_timer_vivo_sem_dica_de_interrupcao_nao_e_ocioso(self):
        tela = "• Thinking (1m 5s)\n› Ask Codex to do anything\n  gpt-6.1-sol high · ~/x"
        self.assertFalse(self.m.codex_ocioso_pela_tela(tela))

    def test_rascunho_humano_na_caixa_nao_conta_como_ocioso(self):
        tela = "Worked for 5s • 7:06 PM\n› criar as tags v0.3.0\n  gpt-6.1-sol high · ~/x"
        self.assertFalse(self.m.codex_ocioso_pela_tela(tela))

    def test_braille_animado_sobre_a_caixa_nao_e_trabalho(self):
        tela = "Worked for 5s • 7:06 PM\n⠁  ⠄  ⠂ ⢀\n› Ask Codex to do anything ⠈ ⠐\n  gpt-6.1-sol high · ~/x"
        self.assertTrue(self.m.codex_ocioso_pela_tela(tela))

    def test_vazio_nao_infere(self):
        self.assertFalse(self.m.codex_ocioso_pela_tela(""))

    def test_status_efetivo_so_mexe_em_codex_unknown_pronto(self):
        m = self.m
        with mock.patch.object(m, "le_pane", return_value="› Ask Codex to do anything"):
            codex = {"agent": "codex", "name": "x-rev-1", "agent_status": "unknown", "interactive_ready": True}
            self.assertEqual(m.status_efetivo(codex), "idle?")
            self.assertEqual(m.status_efetivo({**codex, "agent_status": "working"}), "working")
            self.assertEqual(m.status_efetivo({**codex, "interactive_ready": None}), "unknown")
            self.assertEqual(m.status_efetivo({**codex, "agent": "claude"}), "unknown")


if __name__ == "__main__":
    unittest.main()


class PrefixoDoKindTests(unittest.TestCase):
    """A coluna KIND ja diz `claude`, entao `claude-sonnet-5` em MODEL repete a
    informacao e rouba largura da linha. Pedido do usuario 2026-09-10."""

    def setUp(self):
        self.m = _load()

    def test_claude_prefix_is_dropped(self):
        self.assertEqual(self.m.sem_prefixo_do_kind("claude-sonnet-5", "claude"), "sonnet-5")
        self.assertEqual(self.m.sem_prefixo_do_kind("claude-fable-5-1", "claude"), "fable-5-1")

    def test_codex_ids_pass_through(self):
        """gpt-6-astra nao comeca com `codex-`, entao nada a tirar."""
        self.assertEqual(self.m.sem_prefixo_do_kind("gpt-6-astra", "codex"), "gpt-6-astra")

    def test_alias_from_argv_is_untouched(self):
        """`--model opus` ja vem curto; nao vira string vazia nem perde letra."""
        self.assertEqual(self.m.sem_prefixo_do_kind("opus", "claude"), "opus")

    def test_no_model_survives(self):
        self.assertEqual(self.m.sem_prefixo_do_kind("(unknown)", "claude"), "(unknown)")
        self.assertIsNone(self.m.sem_prefixo_do_kind(None, "claude"))


class OrdemDaListagemTests(unittest.TestCase):
    """A tabela deve sair na MESMA ordem que o usuario ve na sidebar do Herdr.
    Ordenar por nome do agent punha o MFC (workspace numero 1) no meio da lista,
    entre llm-* e omabackup-*, e o usuario reportou como se o space estivesse
    faltando -- 2026-09-11."""

    def setUp(self):
        self.m = _load()

    def test_exec_vem_antes_dos_revisores(self):
        """Alfabeticamente `-exec` cai DEPOIS de `-rev-1`; a ordem do layout
        (exec, rev-1, rev-2, scout) e' a que se le naturalmente."""
        import re
        fonte = open(os.path.join(BIN_DIR, "herdr-agents")).read()
        m = re.search(r'PAPEL\s*=\s*\{([^}]*)\}', fonte)
        self.assertIsNotNone(m, "o mapa de peso por papel sumiu")
        pesos = m.group(1)
        self.assertLess(pesos.index('"-exec"'), pesos.index('"-rev-1"'))
        self.assertLess(pesos.index('"-rev-1"'), pesos.index('"-rev-2"'))
        self.assertLess(pesos.index('"-rev-2"'), pesos.index('"-scout"'))

    def test_ordena_por_numero_de_workspace(self):
        """A chave de ordenacao precisa consultar a numeracao do Herdr, nao o
        nome. Sem isso a tabela volta a divergir da sidebar."""
        fonte = open(os.path.join(BIN_DIR, "herdr-agents")).read()
        self.assertIn('api("workspace", "list")', fonte)
        self.assertIn("ordem_ws.get(", fonte)
