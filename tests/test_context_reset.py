#!/usr/bin/env python3
"""Regression tests for the legacy native reset helper.

The native reset command differs by CLI (`/new` for Codex and `/clear` for
Claude), and Herdr can report a stale session id for Codex until the next
turn. The shared reset primitive therefore verifies a sentinel probe instead
of trusting either the command result or the metadata alone. Runtime
model/reasoning readings are evidence only and never a permission gate.
"""
import importlib.util
import os
import re
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock


BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load():
    loader = SourceFileLoader("herdr_dispatch_context_reset", os.path.join(BIN_DIR, "_herdr_dispatch.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class PaneCompositionTests(unittest.TestCase):
    def setUp(self):
        self.core = _load()

    def _read(self, text, **kwargs):
        result = mock.Mock(returncode=0, stdout=text, stderr="")
        with mock.patch.object(self.core.subprocess, "run", return_value=result):
            return self.core.pane_looks_busy_with_human_input("w:p1", **kwargs)

    def test_sent_history_plus_codex_placeholder_is_not_a_draft(self):
        result = self._read("› prompt já enviado\n• resposta\n› Ask Codex to do anything\n")
        self.assertEqual(result, (False, None))

    def test_latest_compose_line_is_a_draft(self):
        busy, why = self._read("› prompt já enviado\n• resposta\n› texto humano ainda não enviado\n")
        self.assertTrue(busy)
        self.assertIn("texto humano", why)

    def test_latest_compose_line_can_be_ignored_for_headless_reviewer(self):
        self.assertEqual(
            self._read(
                "› prompt já enviado\n• resposta\n› o\n",
                check_composition=False,
            ),
            (False, None),
        )

    def test_old_pending_marker_outside_tail_is_ignored(self):
        text = "interrupted\n" + "linha neutra\n" * 8 + "❯\n"
        self.assertEqual(self._read(text), (False, None))


class ReviewerContextResetTests(unittest.TestCase):
    def setUp(self):
        self.core = _load()
        self.before = {
            "agent": "codex",
            "agent_status": "idle",
            "pane_id": "w:p1",
            "agent_session": {"value": "old-session"},
        }
        self.after_reset = dict(self.before)
        self.after = {
            **self.before,
            "agent_session": {"value": "new-session"},
        }

    def _run(self, kind="codex", answer="HERDR_RESET_MARKER_ABSENT", get_infos=None):
        before = {**self.before, "agent": kind}
        infos = get_infos or [before, {**before}, {**before, "agent_session": {"value": "new-session"}}]
        profile = {
            "observed": True,
            "kind": kind,
            "model": "gpt-6-astra" if kind == "codex" else "opus",
            "reasoning_effort": "medium" if kind == "codex" else "low",
            "source": "argv",
            "pid": 123,
            "argv": [kind, "--model", "gpt-6-astra", "--effort", "medium"],
        }
        with mock.patch.object(self.core, "get_agent_info", side_effect=infos), \
             mock.patch.object(self.core, "pane_looks_busy_with_human_input", return_value=(False, None)), \
             mock.patch.object(self.core, "dispatch_and_wait_all", side_effect=[
                 ({"rev": "idle"}, {}, {}),
                 ({"rev": "idle"}, {}, {}),
             ]), \
             mock.patch.object(self.core, "api", return_value={}) as api_call, \
             mock.patch.object(self.core, "_runtime_profile", side_effect=[profile, profile]), \
             mock.patch.object(self.core, "_read_agent_recent", return_value="probe"), \
             mock.patch.object(self.core, "_probe_answer", return_value=answer), \
             mock.patch.object(self.core.time, "sleep"):
            result = self.core.reset_reviewer_context("rev")
        return result, api_call

    def test_codex_uses_new_and_accepts_stale_id_when_probe_is_absent(self):
        result, api_call = self._run()
        api_call.assert_called_once_with("agent", "prompt", "rev", "/new")
        self.assertTrue(result["verified"])
        self.assertTrue(result["session_changed"])
        self.assertTrue(result["runtime_preserved"])
        self.assertEqual(result["runtime_before"]["model"], "gpt-6-astra")

    def test_claude_uses_clear(self):
        result, api_call = self._run(kind="claude")
        api_call.assert_called_once_with("agent", "prompt", "rev", "/clear")
        self.assertEqual(result["reset_command"], "/clear")

    def test_marker_present_fails_closed(self):
        with self.assertRaisesRegex(self.core.ContextResetError, "marcador ainda está acessível"):
            self._run(answer="HERDR_RESET_MARKER_PRESENT")

    def test_unknown_reasoning_is_evidence_and_does_not_block_seed(self):
        profile = {
            "observed": False,
            "kind": "codex",
            "model": "gpt-6-astra",
            "reasoning_effort": "unknown",
            "source": "pane",
        }
        with mock.patch.object(self.core, "get_agent_info", side_effect=[
                 self.before, self.before, self.before,
             ]), \
             mock.patch.object(self.core, "pane_looks_busy_with_human_input", return_value=(False, None)), \
             mock.patch.object(self.core, "_runtime_profile", return_value=profile), \
             mock.patch.object(self.core, "dispatch_and_wait_all", side_effect=[
                 ({"rev": "idle"}, {}, {}), ({"rev": "idle"}, {}, {}),
             ]) as dispatch, \
             mock.patch.object(self.core, "api", return_value={}), \
             mock.patch.object(self.core, "_read_agent_recent", return_value="probe"), \
             mock.patch.object(self.core, "_probe_answer", return_value="HERDR_RESET_MARKER_ABSENT"), \
             mock.patch.object(self.core.time, "sleep"):
            result = self.core.reset_reviewer_context("rev")
        self.assertTrue(result["verified"])
        self.assertFalse(result["runtime_preserved"])
        self.assertEqual(result["runtime_evidence"]["unknown"], ["before", "after"])
        self.assertEqual(dispatch.call_count, 2)

    def test_probe_does_not_repeat_seed_token(self):
        calls = []

        def capture_dispatch(prompts, timeout_s, **kwargs):
            calls.append(prompts)
            return ({"rev": "idle"}, {}, {})

        with mock.patch.object(self.core, "get_agent_info", side_effect=[
            self.before,
            {**self.before},
            {**self.before},
            {**self.before, "agent_session": {"value": "new-session"}},
        ]), mock.patch.object(self.core, "pane_looks_busy_with_human_input", return_value=(False, None)), \
             mock.patch.object(self.core, "dispatch_and_wait_all", side_effect=capture_dispatch), \
             mock.patch.object(self.core, "api", return_value={}), \
             mock.patch.object(self.core, "_runtime_profile", return_value={
                 "observed": True, "model": "gpt-6-astra", "reasoning_effort": "medium",
             }), \
             mock.patch.object(self.core, "_read_agent_recent", return_value="probe"), \
             mock.patch.object(self.core, "_probe_answer", return_value="HERDR_RESET_MARKER_ABSENT"), \
             mock.patch.object(self.core.time, "sleep"):
            self.core.reset_reviewer_context("rev")

        self.assertEqual(len(calls), 2)
        seed = calls[0]["rev"]
        probe = calls[1]["rev"]
        seed_token = re.search(r"HERDR_RESET_SENTINEL_[0-9a-f]+", seed).group(0)
        self.assertNotIn(seed_token, probe)
        self.assertIn("token arbitrário que recebeu no turno imediatamente anterior", probe)

    def test_probe_waits_for_rendered_answer_after_state_settles(self):
        reads = iter((
            "HERDR_RESET_PROBE_abc prompt sem resposta",
            "HERDR_RESET_PROBE_abc prompt\n• HERDR_RESET_MARKER_ABSENT\n",
        ))
        with mock.patch.object(self.core, "_read_agent_recent", side_effect=reads), \
             mock.patch.object(self.core.time, "sleep"):
            answer = self.core._wait_for_probe_answer("rev", "HERDR_RESET_PROBE_abc", timeout_s=1)
        self.assertEqual(answer, "HERDR_RESET_MARKER_ABSENT")

    def test_reset_waits_for_working_snapshot_before_probe(self):
        working = {**self.before, "agent_status": "working", "state_change_seq": 11}
        settled = {**self.before, "agent_status": "done", "state_change_seq": 12}
        after = {**settled, "agent_session": {"value": "new-session"}}
        infos = [self.before, working, settled, after]
        dispatches = []
        profile = {
            "observed": True,
            "kind": "codex",
            "model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "source": "argv",
            "pid": 123,
            "argv": ["codex", "--model", "gpt-6-astra", "--effort", "medium"],
        }

        def capture_dispatch(prompts, timeout_s, **kwargs):
            dispatches.append(kwargs.get("expected_agents"))
            return ({"rev": "done"}, {}, {})

        with mock.patch.object(self.core, "get_agent_info", side_effect=infos), \
             mock.patch.object(self.core, "pane_looks_busy_with_human_input", return_value=(False, None)), \
             mock.patch.object(self.core, "dispatch_and_wait_all", side_effect=capture_dispatch), \
             mock.patch.object(self.core, "api", return_value={}), \
             mock.patch.object(self.core, "_runtime_profile", side_effect=[profile, profile]), \
             mock.patch.object(self.core, "_read_agent_recent", return_value="probe"), \
             mock.patch.object(self.core, "_probe_answer", return_value="HERDR_RESET_MARKER_ABSENT"), \
             mock.patch.object(self.core.time, "sleep"):
            result = self.core.reset_reviewer_context("rev")

        self.assertTrue(result["verified"])
        self.assertEqual(len(dispatches), 2)
        self.assertEqual(dispatches[1]["rev"]["agent_status"], "done")
        self.assertEqual(dispatches[1]["rev"]["state_change_seq"], 12)

    def test_unknown_kind_fails_before_sending_seed(self):
        with mock.patch.object(self.core, "get_agent_info", return_value={
            **self.before, "agent": "grok",
        }), mock.patch.object(self.core, "pane_looks_busy_with_human_input", return_value=(False, None)), \
             mock.patch.object(self.core, "dispatch_and_wait_all") as dispatch:
            with self.assertRaisesRegex(self.core.ContextResetError, "não tem comando de reset"):
                self.core.reset_reviewer_context("rev")
        dispatch.assert_not_called()

    def test_pending_composition_is_ignored_for_headless_reviewer_reset(self):
        def headless_guard(_pane_id, *args, **kwargs):
            if kwargs.get("check_composition") is False:
                return False, None
            return True, "rascunho"

        with mock.patch.object(self.core, "get_agent_info", side_effect=[
            self.before,
            self.before,
            self.before,
        ]), mock.patch.object(
            self.core, "pane_looks_busy_with_human_input", side_effect=headless_guard
        ) as guard, mock.patch.object(
            self.core, "dispatch_and_wait_all", side_effect=[
                ({"rev": "idle"}, {}, {}),
                ({"rev": "idle"}, {}, {}),
            ]
        ), mock.patch.object(self.core, "api", return_value={}), mock.patch.object(
            self.core,
            "_runtime_profile",
            return_value={
                "observed": True,
                "model": "gpt-6-astra",
                "reasoning_effort": "medium",
            },
        ), mock.patch.object(self.core, "_read_agent_recent", return_value="probe"), mock.patch.object(
            self.core, "_probe_answer", return_value="HERDR_RESET_MARKER_ABSENT"
        ), mock.patch.object(self.core.time, "sleep"):
            result = self.core.reset_reviewer_context("rev")

        self.assertTrue(result["verified"])
        self.assertGreaterEqual(guard.call_count, 3)
        self.assertTrue(
            all(call.kwargs["check_composition"] is False for call in guard.call_args_list)
        )

    def test_real_cli_dialog_still_blocks_headless_reviewer_reset(self):
        with mock.patch.object(self.core, "get_agent_info", return_value=self.before), \
             mock.patch.object(
                 self.core,
                 "pane_looks_busy_with_human_input",
                 return_value=(True, "diálogo de confiança pendente"),
             ) as guard, mock.patch.object(self.core, "dispatch_and_wait_all") as dispatch:
            with self.assertRaisesRegex(self.core.ContextResetError, "diálogo pendente"):
                self.core.reset_reviewer_context("rev")
        dispatch.assert_not_called()
        guard.assert_called_once_with("w:p1", check_composition=False)

    def test_runtime_model_or_effort_change_is_reported_without_blocking(self):
        with mock.patch.object(self.core, "get_agent_info", side_effect=[
            self.before,
            {**self.before},
            {**self.before, "agent_session": {"value": "new-session"}},
        ]), mock.patch.object(self.core, "pane_looks_busy_with_human_input", return_value=(False, None)), \
             mock.patch.object(self.core, "dispatch_and_wait_all", side_effect=[
                 ({"rev": "idle"}, {}, {}),
                 ({"rev": "idle"}, {}, {}),
             ]), mock.patch.object(self.core, "api", return_value={}), \
             mock.patch.object(self.core, "_runtime_profile", side_effect=[
                 {"observed": True, "model": "gpt-6-astra", "reasoning_effort": "medium"},
                 {"observed": True, "model": "gpt-5.6-luna", "reasoning_effort": "max"},
             ]), mock.patch.object(self.core, "_read_agent_recent", return_value="probe"), \
             mock.patch.object(self.core, "_probe_answer", return_value="HERDR_RESET_MARKER_ABSENT"), \
             mock.patch.object(self.core.time, "sleep"):
            result = self.core.reset_reviewer_context("rev")
        self.assertTrue(result["verified"])
        self.assertFalse(result["runtime_preserved"])
        self.assertEqual(
            result["runtime_evidence"]["differences"],
            [
                {"field": "model", "before": "gpt6astra", "after": "gpt5.6luna"},
                {"field": "reasoning_effort", "before": "medium", "after": "max"},
            ],
        )

    def test_runtime_profile_reads_claude_model_and_effort_from_process(self):
        info = {"agent": "claude", "pane_id": "w:p1"}
        process = {
            "process_info": {
                "foreground_processes": [{
                    "name": "claude",
                    "pid": 42,
                    "argv": ["/bin/claude", "--model", "sonnet-5", "--effort", "low"],
                }],
            },
        }
        with mock.patch.object(self.core, "api", return_value=process), \
             mock.patch.object(self.core, "_read_agent_recent", return_value=""):
            profile = self.core._runtime_profile("rev", info)
        self.assertTrue(profile["observed"])
        self.assertEqual(profile["model"], "sonnet-5")
        self.assertEqual(profile["reasoning_effort"], "low")

    def test_runtime_profile_reads_effective_claude_header_after_clear(self):
        info = {"agent": "claude", "pane_id": "w:p1"}
        with mock.patch.object(self.core, "_agent_foreground_argv",
                               return_value=(["claude"], 123)), \
             mock.patch.object(self.core, "_read_agent_recent",
                               return_value="Opus 5 (1M context) with medium effort · Claude Max"):
            profile = self.core._runtime_profile("rev", info)
        self.assertEqual(profile["model"], "opus 5")
        self.assertEqual(profile["reasoning_effort"], "medium")
        self.assertEqual(profile["source"], "pane")

    def test_claude_explicit_status_beats_stale_startup_header(self):
        """The startup banner may be old after `/model`; labelled status wins."""
        info = {
            "agent": "claude",
            "pane_id": "w:p1",
        }
        with mock.patch.object(
            self.core, "_agent_foreground_argv",
            return_value=(
                ["/bin/claude", "--model", "sonnet-5", "--effort", "low"],
                123,
            ),
        ), mock.patch.object(
            self.core, "_read_agent_recent",
            return_value=(
                "Sonnet 5 (1M context) with low effort · Claude Max\n"
                "Model: Opus 5 (1M context)\n"
                "Reasoning effort: high\n"
            ),
        ):
            profile = self.core._runtime_profile("rev-2", info)
        self.assertEqual(profile["model"], "opus 5")
        self.assertEqual(profile["reasoning_effort"], "high")
        self.assertEqual(profile["source"], "status")

    def test_claude_inline_status_effort_is_accepted_for_inheritance(self):
        info = {"agent": "claude", "pane_id": "w:p1"}
        with mock.patch.object(
            self.core, "_agent_foreground_argv", return_value=( ["/bin/claude"], 123)
        ), mock.patch.object(
            self.core, "_read_agent_recent",
            return_value="Model: Opus 5 (reasoning low, summaries auto)\n",
        ):
            profile = self.core._runtime_profile("rev-2", info)
        self.assertTrue(profile["observed"])
        self.assertEqual(profile["model"], "opus 5")
        self.assertEqual(profile["reasoning_effort"], "low")
        self.assertEqual(profile["source"], "status")

    def test_claude_alias_and_resolved_header_compare_as_same_model(self):
        before = {"kind": "claude", "model": "opus", "reasoning_effort": "low", "observed": True}
        after = {"kind": "claude", "model": "opus 5", "reasoning_effort": "low", "observed": True}
        self.core._assert_runtime_preserved("rev", before, after)

    def test_latest_runtime_header_beats_older_prose_match(self):
        info = {"agent": "claude", "pane_id": "w:p1"}
        with mock.patch.object(self.core, "_agent_foreground_argv",
                               return_value=(["claude"], 123)), \
             mock.patch.object(self.core, "_read_agent_recent",
                               return_value="sonnet-5 low (old text)\nOpus 5 (1M context) with high effort"):
            profile = self.core._runtime_profile("rev", info)
        self.assertEqual(profile["model"], "opus 5")
        self.assertEqual(profile["reasoning_effort"], "high")

    def test_full_claude_alias_matches_resolved_header(self):
        before = {"kind": "claude", "model": "claude-sonnet-5", "reasoning_effort": "high", "observed": True}
        after = {"kind": "claude", "model": "sonnet 5", "reasoning_effort": "high", "observed": True}
        self.core._assert_runtime_preserved("rev", before, after)

    def test_runtime_profile_reads_codex_footer_when_argv_is_unpinned(self):
        info = {"agent": "codex", "pane_id": "w:p1"}
        process = {
            "process_info": {
                "foreground_processes": [{
                    "name": "codex",
                    "pid": 43,
                    "argv": ["/bin/codex"],
                }],
            },
        }
        with mock.patch.object(self.core, "api", return_value=process), \
             mock.patch.object(self.core, "_read_agent_recent", return_value="gpt-5.6-sol xhigh · ~/repo"):
            profile = self.core._runtime_profile("rev", info)
        self.assertTrue(profile["observed"])
        self.assertEqual(profile["model"], "gpt-5.6-sol")
        self.assertEqual(profile["reasoning_effort"], "xhigh")
        self.assertEqual(profile["source"], "pane")

    def test_runtime_profile_reads_codex_model_and_effort_from_process(self):
        info = {"agent": "codex", "pane_id": "w:p1"}
        process = {
            "process_info": {
                "foreground_processes": [{
                    "name": "codex",
                    "pid": 44,
                    "argv": [
                        "/bin/codex", "--model", "gpt-6-astra", "-c",
                        "model_reasoning_effort=medium",
                    ],
                }],
            },
        }
        with mock.patch.object(self.core, "api", return_value=process), \
             mock.patch.object(self.core, "_read_agent_recent", return_value=""):
            profile = self.core._runtime_profile("rev", info)
        self.assertTrue(profile["observed"])
        self.assertEqual(profile["model"], "gpt-6-astra")
        self.assertEqual(profile["reasoning_effort"], "medium")
        self.assertEqual(profile["source"], "argv")

    def test_runtime_profile_marks_unobservable_reasoning_unknown(self):
        info = {"agent": "codex", "pane_id": "w:p1"}
        process = {
            "process_info": {
                "foreground_processes": [{
                    "name": "codex", "pid": 45,
                    "argv": ["/bin/codex", "--model", "gpt-6-astra"],
                }],
            },
        }
        with mock.patch.object(self.core, "api", return_value=process), \
             mock.patch.object(self.core, "_read_agent_recent", return_value=""):
            profile = self.core._runtime_profile("rev", info)
        self.assertEqual(profile["model"], "gpt-6-astra")
        self.assertEqual(profile["reasoning_effort"], "unknown")
        self.assertFalse(profile["observed"])

    def test_runtime_profile_summary_exposes_preserved_pair(self):
        summary = self.core.runtime_profile_summary({
            "runtime_before": {
                "model": "sonnet-5",
                "reasoning_effort": "low",
                "source": "argv",
            },
        })
        self.assertIn("model=sonnet-5", summary)
        self.assertIn("reasoning_effort=low", summary)
        self.assertIn("preservados", summary)


if __name__ == "__main__":
    unittest.main()
