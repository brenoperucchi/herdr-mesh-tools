"""Regressões do monitor read-only de contexto dos revisores."""
import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock


BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load(name, filename=None):
    filename = filename or name
    path = os.path.join(BIN_DIR, filename)
    loader = SourceFileLoader(name.replace("-", "_"), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _info(status="idle", seq=10):
    return {
        "agent": "codex",
        "agent_status": status,
        "pane_id": "w:p1",
        "workspace_id": "w1",
        "tab_id": "w1:t1",
        "cwd": "/repo",
        "foreground_cwd": "/repo",
        "agent_session": {"value": "session-1"},
        "revision": 3,
        "state_change_seq": seq,
        "interactive_ready": True,
    }


class ContextWatchTests(unittest.TestCase):
    def setUp(self):
        self.watch = _load("herdr_context_watch", "herdr-context-watch")

    def test_context_parser_requires_anchored_status_line(self):
        text = "Prosa: 99% left (1K used / 2K)\n"
        text += "  Context window: 83% left (52.8K used / 258K)\n"
        parsed = self.watch.parse_context_window(text)
        self.assertEqual(parsed["left_percent"], 83.0)
        self.assertEqual(parsed["used_tokens"], 52800)
        self.assertEqual(parsed["total_tokens"], 258000)
        self.assertEqual(parsed["source"], "pane_status")

    def test_missing_context_is_unknown(self):
        self.assertIsNone(self.watch.parse_context_window("83% left (52K used / 258K)"))

    def test_context_parser_rejects_impossible_or_malformed_numbers(self):
        self.assertIsNone(
            self.watch.parse_context_window("Context window: 120% left (1K used / 2K)")
        )
        self.assertIsNone(
            self.watch.parse_context_window("Context window: 20% left (3K used / 2K)")
        )
        self.assertIsNone(
            self.watch.parse_context_window("Context window: 20% left (1..2K used / 2K)")
        )

    def test_runtime_parser_uses_status_block(self):
        parsed = self.watch.parse_runtime_status(
            "Model: gpt-6-astra (reasoning low, summaries auto)"
        )
        self.assertEqual(parsed["model"], "gpt-6-astra")
        self.assertEqual(parsed["reasoning_effort"], "low")

    def test_runtime_parser_accepts_separate_effort_label(self):
        parsed = self.watch.parse_runtime_status(
            "Current Model: Opus 5 (1M context)\nReasoning Effort: medium\n"
        )
        self.assertEqual(parsed["model"], "Opus 5")
        self.assertEqual(parsed["reasoning_effort"], "medium")

    def test_runtime_parser_rejects_model_without_effort(self):
        self.assertFalse(
            self.watch.parse_runtime_status("Model: Opus 5\n")["observed"]
        )

    def test_working_agent_is_skipped_without_reading_pane(self):
        with mock.patch.object(self.watch.core, "get_agent_info", return_value=_info("working")), \
             mock.patch.object(self.watch.core, "_read_agent_recent") as read:
            result = self.watch.observe_agent("demo-rev-1", 20)
        self.assertEqual(result["state"], "skipped")
        read.assert_not_called()

    def test_idle_agent_gets_context_measurement(self):
        info = _info()
        status = "Context window: 15% left (220K used / 258K)\n"
        api_result = {"process_info": {"foreground_processes": [{"pid": 42, "argv": ["codex"]}]}}
        with mock.patch.object(self.watch.core, "get_agent_info", side_effect=[info, info]), \
             mock.patch.object(self.watch.core, "_read_agent_recent", return_value=status), \
             mock.patch.object(self.watch.core, "api", return_value=api_result):
            result = self.watch.observe_agent("demo-rev-1", 20)
        self.assertEqual(result["state"], "near_limit")
        self.assertEqual(result["context"]["left_percent"], 15.0)
        self.assertEqual(result["process"]["pid"], 42)

    def test_status_probe_refreshes_context_before_read(self):
        info = _info(seq=10)
        after_probe = _info(seq=11)
        status = "Context window: 96% left (21.5K used / 258K)\n"
        api_result = {"process_info": {"foreground_processes": [{"pid": 42, "argv": ["codex"]}]}}
        with mock.patch.object(self.watch.core, "get_agent_info",
                               side_effect=[info, after_probe, after_probe]), \
             mock.patch.object(self.watch.core, "dispatch_and_wait_all",
                               return_value=({"demo-rev-1": "idle"}, {"demo-rev-1": {}}, {})), \
             mock.patch.object(self.watch.core, "_read_agent_recent", return_value=status), \
             mock.patch.object(self.watch.core, "api", return_value=api_result):
            result = self.watch.observe_agent("demo-rev-1", 20, probe_status=True)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["context"]["left_percent"], 96.0)
        self.assertTrue(result["status_probe"]["attempted"])

    def test_metadata_change_discards_measurement(self):
        before = _info(seq=10)
        after = _info(seq=11)
        with mock.patch.object(self.watch.core, "get_agent_info", side_effect=[before, after]), \
             mock.patch.object(self.watch.core, "_read_agent_recent", return_value="Context window: 80% left (1K used / 2K)"), \
             mock.patch.object(self.watch.core, "api", return_value={"process_info": {}}):
            result = self.watch.observe_agent("demo-rev-1", 20)
        self.assertEqual(result["state"], "race")
        self.assertIsNone(result["context"])
        self.assertIsNone(result["runtime"])
        self.assertIsNone(result["process"])
        self.assertEqual(result["reason_code"], "identity_changed")

    def test_output_is_json_and_atomic(self):
        payload = {"schema_version": 1, "agents": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "context.json")
            self.watch.write_output(path, payload)
            with open(path, encoding="utf-8") as stream:
                self.assertEqual(json.load(stream), payload)
            self.assertFalse(os.path.exists(path + ".tmp"))


class ResetPolicyTests(unittest.TestCase):
    def setUp(self):
        self.core = _load("herdr_dispatch_watch_policy", "_herdr_dispatch.py")

    def test_automatic_reset_is_disabled_and_recorded(self):
        self.assertFalse(self.core.AUTOMATIC_REVIEWER_RESET)
        info = self.core.skipped_reset_info(["foo-rev-1", "foo-rev-2", "foo-scout"])
        self.assertTrue(all(item["skipped"] for item in info.values()))
        self.assertTrue(all("herdr-swap" in item["reason"] for item in info.values()))


if __name__ == "__main__":
    unittest.main()
