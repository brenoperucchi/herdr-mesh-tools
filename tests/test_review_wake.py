"""Pedido do coordenador (18/11 11:50): acordar o exec dono da rodada quando o ultimo parecer chega.
8 casos hoje com o exec parado ate 5 min esperando o ciclo do vigia."""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))
import _herdr_review_wake as wake  # noqa: E402


def write_verdict(root, name, text):
    os.makedirs(os.path.join(root, name))
    open(os.path.join(root, name, "verdict.md"), "w").write(text)


import contextlib


class Core:
    def __init__(self, pane="w1:p1", refuse=None, missing=False, swap=None):
        self.calls = []
        self.pane, self.refuse, self.missing, self.swap = pane, refuse, missing, swap
        self.reads = 0
        self.written = None

    def get_agent_info(self, name):
        if self.missing:
            raise RuntimeError("agent_not_found")
        self.reads += 1
        if self.swap and self.reads > 1:
            return {**self.swap, "pane_id": self.pane}
        return {"name": name, "pane_id": self.pane, "terminal_id": "T", "agent_session": {"value": "s"}}

    @contextlib.contextmanager
    def _dispatch_submission_locks(self, agents):
        yield

    def _dispatch_differences(self, a, b, include_lifecycle=True):
        return [k for k in ("name", "pane_id", "terminal_id") if a.get(k) != b.get(k)]

    def write_json_atomic(self, path, payload):
        if self.written == "boom":
            raise OSError("disk")
        import json
        json.dump(payload, open(path, "w"))

    def api(self, *args):
        if self.refuse:
            raise self.refuse
        self.calls.append(args)


class Wake(unittest.TestCase):
    def test_message_carries_round_verdicts_and_folder(self):
        with tempfile.TemporaryDirectory() as root:
            rd = os.path.join(root, "herdr-mesh-tools-184")
            os.makedirs(rd)
            write_verdict(rd, "herdr-mesh-tools-rev-1", "REQUEST_CHANGES\n\nx")
            write_verdict(rd, "herdr-mesh-tools-rev-2", "# Veredito\n\n## Veredito: APPROVE\n")
            msg = wake.build_message(rd, "completa", ["herdr-mesh-tools-rev-1", "herdr-mesh-tools-rev-2"],
                                     ["herdr-mesh-tools-rev-1", "herdr-mesh-tools-rev-2"], "herdr-mesh-tools")
            self.assertEqual(msg, f"[herdr-review] rodada herdr-mesh-tools-184 completa: "
                                  f"rev-1=REQUEST_CHANGES, rev-2=APPROVE, pareceres em {rd}")

    def test_missing_reviewer_is_reported_not_hidden(self):
        with tempfile.TemporaryDirectory() as root:
            msg = wake.build_message(root, "parcial", ["s-rev-1", "s-rev-2"], [], "s")
            self.assertIn("rev-1=sem parecer utilizável", msg)
            self.assertIn("parcial", msg)

    def test_delivers_through_the_guarded_core_api_to_the_own_pane(self):
        core = Core()
        result = wake.wake_exec("x-exec", "TEXTO", core, own_pane="w1:p1")
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(core.calls, [("agent", "prompt", "w1:p1", "TEXTO")])

    def test_refusal_by_can_send_is_recorded_and_never_raises(self):
        core = Core(refuse=RuntimeError("herdr-can-send x-exec: código 1; envio recusado"))
        result = wake.wake_exec("x-exec", "T", core, own_pane="w1:p1")
        self.assertEqual(result["status"], "refused")
        self.assertIn("código 1", result["reason"])

    def test_does_not_wake_a_pane_that_is_not_the_dispatchers_own(self):
        core = Core(pane="w1:p9")
        self.assertEqual(wake.wake_exec("x-exec", "T", core, own_pane="w1:p1")["status"], "skipped")
        self.assertEqual(wake.wake_exec("x-exec", "T", core, own_pane=None)["status"], "skipped")
        self.assertEqual(core.calls, [])

    def test_oserror_and_valueerror_from_transport_never_escape(self):
        # achado r186/rev-1 P2-1; no envio o resultado e' "unknown", sem reenvio
        for exc in (OSError("x"), ValueError("y")):
            core = Core(refuse=exc)
            self.assertEqual(wake.wake_exec("x-exec", "T", core, own_pane="w1:p1")["status"], "unknown")
        class Boom(Core):
            def get_agent_info(self, name):
                raise OSError("obs")
        self.assertEqual(wake.wake_exec("x-exec", "T", Boom(), own_pane="w1:p1")["status"], "error")

    def test_other_agent_in_the_same_pane_is_refused_and_nothing_is_sent(self):
        # achado r186/rev-1 P2-2
        core = Core(swap={"name": "other-exec", "terminal_id": "T"})
        result = wake.wake_exec("x-exec", "T", core, own_pane="w1:p1")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(core.calls, [])
        core2 = Core(swap={"name": "x-exec", "terminal_id": "OTHER"})
        self.assertEqual(wake.wake_exec("x-exec", "T", core2, own_pane="w1:p1")["status"], "refused")
        self.assertEqual(core2.calls, [])

    def test_wake_round_uses_published_metrics_and_survives_a_failed_second_write(self):
        # achados r186/rev-1 P2-1 e P2-3 (--complementar usa este caminho)
        import json
        with tempfile.TemporaryDirectory() as root:
            rd = os.path.join(root, "x-7")
            write_verdict(rd, "x-rev-1", "APPROVE\n")
            json.dump({"review_status": "completa",
                       "reviewers": {"x-rev-1": {"verdict_sha256": "h"}, "x-rev-2": {}}},
                      open(os.path.join(rd, "metrics.json"), "w"))
            core = Core()
            result = wake.wake_round(rd, "x", core, "x-exec", own_pane="w1:p1")
            self.assertEqual(result["status"], "delivered")
            text = core.calls[0][3]
            self.assertIn("rev-1=APPROVE", text)
            self.assertIn("rev-2=sem parecer utilizável", text)
            self.assertEqual(json.load(open(os.path.join(rd, "metrics.json")))["wake"]["status"], "delivered")
            bad = Core()
            bad.written = "boom"
            self.assertEqual(wake.wake_round(rd, "x", bad, "x-exec", own_pane="w1:p1")["status"], "delivered")
            self.assertEqual(wake.wake_round(root + "/nao-existe", "x", Core(), "x-exec", own_pane="w1:p1")["status"], "error")

    def test_terminal_swap_is_refused_with_the_REAL_core_comparator(self):
        # achado r187/rev-1 P2-1: o fake comparava terminal_id; o core real nao. Aqui o real.
        import _herdr_dispatch as real
        core = Core(swap={"name": "x-exec", "terminal_id": "T2", "agent_session": {"value": "s"}})
        core._dispatch_differences = real._dispatch_differences
        result = wake.wake_exec("x-exec", "T", core, own_pane="w1:p1")
        self.assertEqual((result["status"], core.calls), ("refused", []))
        stable = Core()
        stable._dispatch_differences = real._dispatch_differences
        self.assertEqual(wake.wake_exec("x-exec", "T", stable, own_pane="w1:p1")["status"], "delivered")

    def test_caller_context_env_then_native_caller_then_none(self):
        with wake.caller_context({"HERDR_PANE_ID": "w:p1"}) as pane:
            self.assertEqual(pane, "w:p1")
        rt = lambda: SimpleNamespace(caller=lambda: {"pane_id": "w:p7"}, env={})
        with wake.caller_context({}, rt) as pane:
            self.assertEqual(pane, "w:p7")
        def unproven():
            raise RuntimeError("identity_unproven")
        with wake.caller_context({}, lambda: SimpleNamespace(caller=unproven, env={})) as pane:
            self.assertIsNone(pane)
        def noenv():
            raise RuntimeError("server_scope_unproven")
        with wake.caller_context({}, noenv) as pane:
            self.assertIsNone(pane)

    ROUTE_KEYS = ("HERDR_SOCKET_PATH", "HERDR_CONFIG_PATH", "HERDR_SESSION")

    def _clean_env(self, **extra):
        # patch.dict restaura o os.environ INTEIRO ao sair, inclusive se uma asserção falhar
        # (achado r189/rev-1: o teste antigo apagava a rota do processo sem restaurar).
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in self.ROUTE_KEYS:
            os.environ.pop(key, None)
        os.environ.update(extra)

    def test_proof_route_is_kept_for_the_whole_notice_and_then_restored(self):
        self._clean_env()
        rt = lambda: SimpleNamespace(caller=lambda: {"pane_id": "w:p7"},
                                     env={"HERDR_SOCKET_PATH": "/run/A.sock", "HERDR_SESSION": "A"})
        seen = {}
        with wake.caller_context({}, rt) as pane:
            seen["socket"] = os.environ.get("HERDR_SOCKET_PATH")
            seen["session"] = os.environ.get("HERDR_SESSION")
        self.assertEqual((pane, seen), ("w:p7", {"socket": "/run/A.sock", "session": "A"}))
        self.assertNotIn("HERDR_SOCKET_PATH", os.environ)
        self.assertNotIn("HERDR_SESSION", os.environ)

    def test_route_already_in_the_environment_is_never_overridden(self):
        self._clean_env(HERDR_SOCKET_PATH="/run/B.sock")
        rt = lambda: SimpleNamespace(caller=lambda: {"pane_id": "w:p7"},
                                     env={"HERDR_SOCKET_PATH": "/run/A.sock"})
        with wake.caller_context({}, rt):
            self.assertEqual(os.environ["HERDR_SOCKET_PATH"], "/run/B.sock")
        self.assertEqual(os.environ["HERDR_SOCKET_PATH"], "/run/B.sock")

    def test_the_tests_themselves_leave_the_process_route_untouched(self):
        # sentinela: a rota do processo antes == depois de rodar os dois testes acima
        before = {k: os.environ.get(k) for k in self.ROUTE_KEYS}
        suite = unittest.TestSuite([Wake("test_proof_route_is_kept_for_the_whole_notice_and_then_restored"),
                                    Wake("test_route_already_in_the_environment_is_never_overridden")])
        result = unittest.TestResult()
        suite.run(result)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertEqual({k: os.environ.get(k) for k in self.ROUTE_KEYS}, before)

    def test_missing_exec_is_skipped(self):
        self.assertIn(wake.wake_exec("x-exec", "T", Core(missing=True), own_pane="w1:p1")["status"], ("skipped", "refused"))


if __name__ == "__main__":
    unittest.main()


class ScriptedCore(Core):
    """herdr-can-send recusa com os codigos da fila (3 = working, 1 = rascunho humano, 2 = dialogo) antes de aceitar."""
    def __init__(self, codes, wait=None, **kw):
        super().__init__(**kw)
        import _herdr_dispatch as real
        self.codes, self.waits, self.wait_result = list(codes), [], wait
        self.Refused = real.DeliveryRefusedError

    def api(self, *args):
        if self.codes:
            code = self.codes.pop(0)
            if code:
                raise self.Refused(f"herdr-can-send x-exec: código {code}; envio recusado", code=code)
        self.calls.append(args)

    def wait_quiescent(self, name, **kw):
        self.waits.append((name, kw))
        if isinstance(self.wait_result, Exception):
            raise self.wait_result
        return {}


class WakeWhileExecIsWorking(unittest.TestCase):
    """Regressao cerebro-210 e cerebro-212: wake=refused com herdr-can-send 3 porque o exec ainda estava no turno."""

    def test_working_exec_is_awaited_and_gets_exactly_one_prompt(self):
        core = ScriptedCore([3, 0])                      # 1a tentativa: working; depois do idle: livre
        result = wake.wake_exec("x-exec", "TEXTO", core, own_pane="w1:p1")
        self.assertEqual(result["status"], "delivered")
        self.assertTrue(result["retried_after_working"])
        self.assertEqual(core.calls, [("agent", "prompt", "w1:p1", "TEXTO")])            # um unico envio
        self.assertEqual(core.waits[0][0], "x-exec")
        self.assertEqual(core.waits[0][1]["timeout_s"], wake.WAKE_WAIT_S)

    def test_exec_that_never_gets_idle_is_refused_after_the_ceiling_and_nothing_is_sent(self):
        core = ScriptedCore([3], wait=RuntimeError("não estabilizou em idle"))
        result = wake.wake_exec("x-exec", "T", core, own_pane="w1:p1", wait_s=7)
        self.assertEqual((result["status"], result["can_send_code"], result["waited"]), ("refused", 3, True))
        self.assertIn("7s", result["reason"])
        self.assertEqual(core.calls, [])
        self.assertEqual(core.waits[0][1]["timeout_s"], 7)

    def test_still_refused_after_the_wait_is_not_retried_a_second_time(self):
        core = ScriptedCore([3, 3, 0])                   # voltou a ficar working: so uma nova tentativa
        result = wake.wake_exec("x-exec", "T", core, own_pane="w1:p1")
        self.assertEqual((result["status"], result["can_send_code"]), ("refused", 3))
        self.assertTrue(result["retried_after_working"])
        self.assertEqual((core.calls, len(core.waits)), ([], 1))

    def test_draft_or_dialog_never_waits_and_never_sends(self):
        for code in (1, 2, 4):
            core = ScriptedCore([code])
            result = wake.wake_exec("x-exec", "T", core, own_pane="w1:p1")
            self.assertEqual((result["status"], result["can_send_code"]), ("refused", code))
            self.assertEqual((core.calls, core.waits), ([], []))

    def test_draft_appearing_during_the_wait_blocks_the_retry(self):
        core = ScriptedCore([3, 1])                      # o humano comecou a digitar enquanto esperava
        result = wake.wake_exec("x-exec", "T", core, own_pane="w1:p1")
        self.assertEqual((result["status"], result["can_send_code"]), ("refused", 1))
        self.assertEqual(core.calls, [])

    def test_not_the_own_pane_still_skips_without_waiting(self):
        core = ScriptedCore([3], pane="w1:p9")
        self.assertEqual(wake.wake_exec("x-exec", "T", core, own_pane="w1:p1")["status"], "skipped")
        self.assertEqual(core.waits, [])

    def test_wake_round_records_the_retry_in_the_metrics(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            rd = os.path.join(root, "x-9")
            write_verdict(rd, "x-rev-1", "APPROVE\n")
            json.dump({"review_status": "completa", "reviewers": {"x-rev-1": {"verdict_sha256": "h"}}},
                      open(os.path.join(rd, "metrics.json"), "w"))
            core = ScriptedCore([3, 0])
            result = wake.wake_round(rd, "x", core, "x-exec", own_pane="w1:p1")
            self.assertEqual(result["status"], "delivered")
            stored = json.load(open(os.path.join(rd, "metrics.json")))["wake"]
            self.assertEqual((stored["status"], stored["retried_after_working"]), ("delivered", True))

    def test_the_real_can_send_error_carries_its_exit_code(self):
        import _herdr_dispatch as real
        with mock.patch.object(real.subprocess, "run",
                               return_value=SimpleNamespace(returncode=3, stdout="agent working", stderr="")):
            with self.assertRaises(real.DeliveryRefusedError) as ctx:
                real.require_can_send("x-exec", expected_pane="w1:p1")
        self.assertEqual(ctx.exception.code, 3)


class WakeIdentityAndMetricsAcrossTheWait(unittest.TestCase):
    """Achados r204: o retry nao troca a referencia de identidade e nao restaura metricas antigas."""

    def _core_swapping_during_wait(self, **changes):
        core = ScriptedCore([3, 0])
        current = {"name": "x-exec", "pane_id": "w1:p1", "terminal_id": "T", "agent_session": {"value": "s"}}
        core.get_agent_info = lambda name: dict(current)
        original = core.wait_quiescent

        def wait(name, **kw):
            current.update(changes)
            return original(name, **kw)
        core.wait_quiescent = wait
        import _herdr_dispatch as real
        core._dispatch_differences = real._dispatch_differences
        return core

    def test_new_session_or_terminal_in_the_same_pane_during_the_wait_is_refused_and_nothing_is_sent(self):
        for change in ({"agent_session": {"value": "new-session"}}, {"terminal_id": "T2"},
                       {"terminal_id": "T2", "agent_session": {"value": "other"}}, {"name": "outro-exec"}):
            core = self._core_swapping_during_wait(**change)
            result = wake.wake_exec("x-exec", "T", core, own_pane="w1:p1")
            self.assertEqual(result["status"], "refused", change)
            self.assertIn("durante a espera", result["reason"])
            self.assertEqual(core.calls, [], change)

    def test_same_identity_after_the_wait_still_sends_exactly_once(self):
        core = self._core_swapping_during_wait()
        result = wake.wake_exec("x-exec", "T", core, own_pane="w1:p1")
        self.assertEqual((result["status"], len(core.calls)), ("delivered", 1))

    def test_metrics_changed_during_the_wait_are_preserved_and_the_text_follows_the_current_round(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            rd = os.path.join(root, "x-9")
            write_verdict(rd, "x-rev-1", "APPROVE\n")
            write_verdict(rd, "x-rev-2", "REQUEST_CHANGES\n")
            path = os.path.join(rd, "metrics.json")
            json.dump({"review_status": "parcial", "reviewers": {"x-rev-1": {"verdict_sha256": "A"}, "x-rev-2": {}}},
                      open(path, "w"))
            core = ScriptedCore([3, 0])
            original = core.wait_quiescent

            def wait(name, **kw):                       # um complemento publica enquanto o aviso espera
                json.dump({"review_status": "completa", "complement_history": ["c1"],
                           "reviewers": {"x-rev-1": {"verdict_sha256": "A"}, "x-rev-2": {"verdict_sha256": "B"}}},
                          open(path, "w"))
                return original(name, **kw)
            core.wait_quiescent = wait
            result = wake.wake_round(rd, "x", core, "x-exec", own_pane="w1:p1")
            self.assertEqual(result["status"], "delivered")
            text = core.calls[0][3]
            self.assertIn("completa", text)                                  # texto refeito a partir do estado atual
            self.assertIn("rev-2=REQUEST_CHANGES", text)
            stored = json.load(open(path))
            self.assertEqual((stored["review_status"], stored["complement_history"]), ("completa", ["c1"]))
            self.assertEqual(stored["reviewers"]["x-rev-2"]["verdict_sha256"], "B")
            self.assertEqual(stored["wake"]["status"], "delivered")


class PublisherUsesTheRoundReservation(unittest.TestCase):
    """Achados r205: publicador legado (scope=None) respeita a reserva do complemento; texto ilegivel nao vira 'unknown'."""

    def test_unreadable_metrics_while_building_the_text_is_error_and_nothing_is_sent(self):
        core = Core()

        def text():
            raise ValueError("json")
        result = wake.wake_exec("x-exec", text, core, own_pane="w1:p1")
        self.assertEqual(result["status"], "error")
        self.assertEqual(core.calls, [])

    def test_publish_waits_for_the_complement_reservation_and_keeps_its_data(self):
        import importlib.machinery, importlib.util, json, threading, time
        loader = importlib.machinery.SourceFileLoader(
            "dispatch_under_test", os.path.join(os.path.dirname(wake.__file__), "herdr-review-dispatch"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        import _herdr_review_complement as complement
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            rd = os.path.join(root, "x-1")
            os.makedirs(rd)
            path = os.path.join(rd, "metrics.json")
            json.dump({"review_status": "parcial", "complements": []}, open(path, "w"))
            mod._PENDING_WAKE.update(round_dir=rd, silent=True)
            captured = {}
            real_round = mod.review_wake.wake_round

            def fake_round(round_dir, slug, core, coordinator, *, own_pane, publish=None):
                captured["publish"] = publish
                return {"status": "delivered"}
            with mock.patch.object(mod.review_wake, "wake_round", fake_round), \
                    mock.patch.object(mod.review_groups, "coordinator_name", lambda s: "x-exec"):
                mod._run_pending_wake(SimpleNamespace(slug="x"), None)
            publish = captured["publish"]
            done = []
            with complement.reservation(Path(rd)):            # complemento em andamento
                t = threading.Thread(target=lambda: (publish({"status": "delivered"}), done.append(1)))
                t.start()
                time.sleep(0.8)
                self.assertEqual(done, [])                    # recusou gravar enquanto a reserva estava presa
                json.dump({"review_status": "completa", "complements": ["c1"]}, open(path, "w"))
            t.join(10)
            stored = json.load(open(path))
            self.assertEqual((stored["review_status"], stored["complements"], stored["wake"]["status"]),
                             ("completa", ["c1"], "delivered"))
