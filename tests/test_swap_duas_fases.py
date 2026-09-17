"""Swap em duas fases e auto-swap.

O swap normal somente fecha o antigo depois que o novo respondeu e confirmou o
handoff. Quando o agent trocado E' aquele com quem o usuario esta falando, o
ponto de nao retorno cai no meio de uma conversa viva e dai as duas fases
mantem os dois panes disponiveis para comparacao.

O auto-swap (o agent trocando a si mesmo) e' o caso limite: dois guards mudam de
significado e pedir handoff vira impossivel.
"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load(name):
    loader = SourceFileLoader(name.replace("-", "_").replace(".py", ""),
                              os.path.join(BIN_DIR, name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


swap = _load("herdr-swap")


class EstadoPendenteTests(unittest.TestCase):
    """A fase 1 grava o que a fase 2 precisa. Se isso se perder, o mesh fica
    com dois agents do mesmo papel vivos e ninguem sabe qual promover."""

    def test_grava_e_le(self):
        with tempfile.TemporaryDirectory() as d:
            p = swap.swap_state_path(d, "herdr-exec")
            dados = {"name": "herdr-exec", "temp_name": "tmpabc",
                     "old_pane_id": "w7:p0", "new_pane_id": "w7:p9",
                     "tab_original": "w7:t1", "tab_temp": "w7:t9"}
            swap.grava_swap_state(p, dados)
            self.assertEqual(swap.le_swap_state(p), dados)

    def test_ausente_devolve_none(self):
        """Sem estado, --finalizar tem que recusar em vez de adivinhar."""
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(swap.le_swap_state(swap.swap_state_path(d, "x")))

    def test_corrompido_devolve_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = swap.swap_state_path(d, "x")
            open(p, "w").write("{ nao e json")
            self.assertIsNone(swap.le_swap_state(p))

    def test_nome_do_arquivo_isola_por_agent(self):
        """Dois swaps pendentes em spaces diferentes nao podem se sobrescrever."""
        with tempfile.TemporaryDirectory() as d:
            self.assertNotEqual(swap.swap_state_path(d, "a-exec"),
                                swap.swap_state_path(d, "b-exec"))

    def test_estado_e_oculto(self):
        """Fica no mesmo dir dos handoffs; nao pode virar ruido na listagem."""
        p = swap.swap_state_path("/tmp/x", "herdr-exec")
        self.assertTrue(os.path.basename(p).startswith("."))


class FinalizarRetornoAoTabTests(unittest.TestCase):
    """A API exige --split e pode fechar o tab de origem durante o move."""

    def finaliza(self, move_fecha_tab=False, erro_move=None, erro_fecha_tab=None):
        estado = {"temp_name": "tmpabc", "old_pane_id": "w7:p0",
                  "new_pane_id": "w7:p9", "tab_original": "w7:t1",
                  "tab_temp": "w7:t9"}
        atual = {"tab": "w7:t9", "temp_fechado": False}

        def api(*args):
            if args[:2] == ("pane", "move"):
                if "--split" not in args:
                    raise RuntimeError("usage: pane move --tab TAB --split right|down")
                split = args[args.index("--split") + 1]
                self.assertIn(split, ("right", "down"))
                if erro_move:
                    raise RuntimeError(erro_move)
                atual["tab"] = args[args.index("--tab") + 1]
                atual["temp_fechado"] = move_fecha_tab
                return {"move_result": {
                    "closed_tab_id": "w7:t9" if move_fecha_tab else None}}
            if args[:2] == ("tab", "close"):
                if atual["temp_fechado"]:
                    raise RuntimeError("tab not found")
                if erro_fecha_tab:
                    raise RuntimeError(erro_fecha_tab)
                atual["temp_fechado"] = True
            return {}

        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            path = swap.swap_state_path(d, "herdr-exec")
            swap.grava_swap_state(path, estado)
            with mock.patch.object(swap, "api", side_effect=api) as chamadas, \
                 mock.patch.object(swap, "get_agent", return_value={"agent_status": "idle"}), \
                 mock.patch.object(swap.core, "pane_looks_busy_with_human_input",
                                   return_value=(False, None)), \
                 mock.patch.object(swap.time, "sleep"), \
                 redirect_stdout(out), redirect_stderr(err):
                swap.finalizar(SimpleNamespace(forcar_fechamento=False), "herdr-exec", d)
            self.assertFalse(os.path.exists(path))
        return atual, chamadas.call_args_list, err.getvalue()

    def test_devolve_pane_ao_tab_original_com_split_suportado(self):
        atual, chamadas, erros = self.finaliza()
        self.assertEqual(atual["tab"], "w7:t1")
        self.assertTrue(atual["temp_fechado"])
        self.assertIn(mock.call("tab", "close", "w7:t9"), chamadas)
        self.assertEqual(erros, "")

    def test_nao_fecha_de_novo_tab_ja_fechado_pelo_move(self):
        atual, chamadas, erros = self.finaliza(move_fecha_tab=True)
        self.assertEqual(atual["tab"], "w7:t1")
        self.assertTrue(atual["temp_fechado"])
        self.assertNotIn(mock.call("tab", "close", "w7:t9"), chamadas)
        self.assertEqual(erros, "")

    def test_falha_no_move_preserva_tab_e_orienta_retorno_antes_do_fix_layout(self):
        atual, chamadas, erros = self.finaliza(erro_move="destino indisponivel")
        self.assertEqual(atual["tab"], "w7:t9")
        self.assertFalse(atual["temp_fechado"])
        self.assertNotIn(mock.call("tab", "close", "w7:t9"), chamadas)
        self.assertIn("herdr pane move w7:p9 --tab w7:t1 --split right", erros)
        self.assertIn("depois rode bin/herdr-fix-layout", erros)

    def test_falha_ao_fechar_tab_nao_reporta_falha_no_retorno(self):
        atual, _, erros = self.finaliza(erro_fecha_tab="tab ocupado")
        self.assertEqual(atual["tab"], "w7:t1")
        self.assertIn("pane w7:p9 ja esta no tab w7:t1", erros)
        self.assertNotIn("nao consegui mover", erros)


class NomeProvisorioTests(unittest.TestCase):
    def test_cabe_no_limite_do_agent_start(self):
        """`agent start` corta em 32 chars. `f"{name}-new"` ja estourou com
        slug real (content-insights-collector-exec-new = 35)."""
        import hashlib
        import time
        for nome in ("herdr-exec", "content-insights-collector-exec", "x"):
            t = "tmp" + hashlib.sha1(f"{nome}{time.time()}".encode()).hexdigest()[:10]
            with self.subTest(nome=nome):
                self.assertLessEqual(len(t), 32)


class FlagsTests(unittest.TestCase):
    """As flags novas precisam existir com os nomes que a mensagem de erro do
    auto-swap manda o usuario digitar."""

    def test_flags_existem(self):
        import argparse
        fonte = open(os.path.join(BIN_DIR, "herdr-swap"), encoding="utf-8").read()
        for f in ("--em-tab-novo", "--finalizar", "--handoff-pronto"):
            self.assertIn(f'"{f}"', fonte)

    def test_auto_swap_exige_as_duas_condicoes(self):
        """Sem --handoff-pronto o prompt travaria; sem --em-tab-novo o swap
        fecharia o pane que esta executando o proprio swap."""
        fonte = open(os.path.join(BIN_DIR, "herdr-swap"), encoding="utf-8").read()
        self.assertIn("auto-swap exige --handoff-pronto", fonte)
        self.assertIn("auto-swap exige --em-tab-novo", fonte)

    def test_blocked_continua_barrando_no_auto_swap(self):
        """'working' e' artefato da propria chamada; 'blocked' e' dialogo real."""
        fonte = open(os.path.join(BIN_DIR, "herdr-swap"), encoding="utf-8").read()
        self.assertIn('("idle", "done", "working") if auto else ("idle", "done")', fonte)


class GuardDeComposicaoTests(unittest.TestCase):
    """`pane_looks_busy_with_human_input` devolve (suspeito, motivo).

    Bug real (2026-09-13): `if core.pane_looks_busy_with_human_input(p):` no
    --finalizar. Toda tupla de dois elementos e' verdadeira, inclusive
    (False, None), entao o guard recusava incondicionalmente e o --finalizar
    nunca conseguiu rodar. Pior: a mensagem culpava o pane por uma condicao
    que nunca chegou a ser avaliada.
    """

    def test_a_tupla_negativa_e_truthy(self):
        """O fato que torna o bug possivel, fixado aqui pra ninguem o repetir."""
        self.assertTrue(bool((False, None)))
        self.assertFalse((False, None)[0])

    def test_finalizar_desempacota_em_vez_de_testar_a_tupla(self):
        fonte = open(os.path.join(BIN_DIR, "herdr-swap"), encoding="utf-8").read()
        self.assertIn("busy, why = core.pane_looks_busy_with_human_input(old_pane)", fonte)
        self.assertNotIn("if core.pane_looks_busy_with_human_input(old_pane):", fonte)

    def test_todo_uso_do_guard_desempacota(self):
        """Vale pros dois chamadores, nao so pro que quebrou."""
        import re
        fonte = open(os.path.join(BIN_DIR, "herdr-swap"), encoding="utf-8").read()
        # So CHAMADAS: o nome seguido de "(". Mencoes em prosa/docstring (entre
        # crases, por exemplo) nao sao uso e nao devem reprovar o teste.
        chamada = re.compile(r"pane_looks_busy_with_human_input\s*\(")
        for linha in fonte.splitlines():
            t = linha.strip()
            if not chamada.search(t) or t.startswith("#"):
                continue
            with self.subTest(linha=t):
                self.assertTrue(t.startswith("busy, why ="),
                                "use `busy, why = ...`, nunca a tupla direto")

    def test_existe_saida_para_falso_positivo(self):
        """A heuristica erra. Sem valvula, um falso positivo trava o swap pra
        sempre -- e `pane close` DESCARTA rascunho, nunca o submete."""
        fonte = open(os.path.join(BIN_DIR, "herdr-swap"), encoding="utf-8").read()
        self.assertIn('"--forcar-fechamento"', fonte)
        self.assertIn("args.forcar_fechamento", fonte)


class RuntimeInheritanceTests(unittest.TestCase):
    def test_codex_inherits_observed_model_and_reasoning(self):
        args = swap._runtime_start_args(
            "codex",
            {"observed": True, "model": "gpt-6-astra", "reasoning_effort": "low"},
        )
        self.assertEqual(args, ["--model", "gpt-6-astra", "-c", "model_reasoning_effort=low"])

    def test_claude_inherits_only_confirmed_pair(self):
        args = swap._runtime_start_args(
            "claude",
            {"observed": True, "model": "Opus 5", "reasoning_effort": "medium"},
        )
        self.assertEqual(args, ["--model", "opus-5", "--effort", "medium"])

    def test_profile_kind_blocks_cross_family_model_copy(self):
        self.assertEqual(
            swap._runtime_start_args(
                "codex",
                {"observed": True, "kind": "claude", "model": "Opus 5",
                 "reasoning_effort": "high"},
            ),
            [],
        )

    def test_unconfirmed_claude_banner_is_not_inherited(self):
        self.assertEqual(
            swap._runtime_start_args(
                "claude",
                {"observed": False, "source": "unconfirmed", "model": "opus 5",
                 "reasoning_effort": "low"},
            ),
            [],
        )

    def test_explicit_target_args_win_over_inheritance(self):
        self.assertTrue(swap._has_runtime_arg(["--model", "sonnet-5"]))
        self.assertTrue(swap._has_runtime_arg(["-c", "model_reasoning_effort=high"]))
        self.assertFalse(swap._has_runtime_arg(["--no-color"]))

    def test_resume_prompt_requires_exact_ack(self):
        prompt = swap._handoff_prompt("/tmp/hand-off.md")
        self.assertIn("HERDR_HANDOFF_READ_OK", prompt)

    def test_claude_profile_probe_uses_status_after_stale_banner(self):
        before = {"agent": "claude", "pane_id": "w:p1", "agent_status": "idle"}
        after = {**before, "state_change_seq": 2}
        status_profile = {
            "observed": True, "source": "status", "model": "opus 5",
            "reasoning_effort": "high",
        }
        with mock.patch.object(swap, "dispatch_and_wait",
                               return_value=("idle", after)) as dispatch, \
             mock.patch.object(swap, "get_agent", return_value=after), \
             mock.patch.object(swap.core, "_runtime_profile",
                               side_effect=[
                                   {"observed": True, "source": "pane", "model": "sonnet 5",
                                    "reasoning_effort": "low"},
                                   status_profile,
                               ]):
            profile, evidence, refreshed = swap._runtime_capture("rev-2", before, 30)
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[1], "/status")
        self.assertEqual(profile["model"], "opus 5")
        self.assertEqual(profile["source"], "status")
        self.assertTrue(evidence["status_probe"]["attempted"])
        self.assertEqual(refreshed, after)

    def test_aborted_status_probe_cannot_inherit_stale_profile(self):
        before = {"agent": "claude", "pane_id": "w:p1", "agent_status": "idle"}
        with mock.patch.object(swap, "dispatch_and_wait",
                               return_value=("preflight_aborted", {})), \
             mock.patch.object(swap.core, "_runtime_profile", return_value={
                 "observed": True, "source": "pane", "model": "sonnet 5",
                 "reasoning_effort": "low",
             }):
            profile, evidence, _ = swap._runtime_capture("rev-2", before, 30)
        self.assertFalse(profile["observed"])
        self.assertEqual(profile["source"], "probe_failed")
        self.assertEqual(evidence["status_probe"]["status"], "preflight_aborted")


if __name__ == "__main__":
    unittest.main()
