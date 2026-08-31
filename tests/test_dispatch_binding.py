#!/usr/bin/env python3
"""Regression tests for fail-closed project binding in the dispatchers.

The live claude-bridge workspace once had agents registered under the runtime
directory instead of the Git repository. A dispatcher must refuse before it
creates a round directory or sends a prompt when those roots differ.
"""
import importlib.util
import os
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load(name):
    path = os.path.join(BIN_DIR, name)
    loader = SourceFileLoader(name.replace("-", "_"), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class DispatchProjectBindingTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load("herdr-review-dispatch")
        self.core = self.mod.core

    def test_different_project_roots_are_rejected_before_dispatch(self):
        with mock.patch.object(
            self.core,
            "project_root",
            side_effect=["/runtime/.claude-bridge", "/repo/claude-bridge"],
        ):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                self.core.validate_dispatch_cwd("/runtime/.claude-bridge")

    def test_same_project_root_is_accepted(self):
        with mock.patch.object(
            self.core,
            "project_root",
            side_effect=["/repo/claude-bridge", "/repo/claude-bridge"],
        ):
            self.assertEqual(
                self.core.validate_dispatch_cwd("/repo/claude-bridge"),
                "/repo/claude-bridge",
            )

    def test_reviewer_foreground_root_is_checked_too(self):
        with mock.patch.object(
            self.core,
            "get_agent_info",
            return_value={
                "cwd": "/repo/claude-bridge",
                "foreground_cwd": "/runtime/.claude-bridge",
            },
        ), mock.patch.object(
            self.core,
            "project_root",
            side_effect=[
                "/repo/claude-bridge",
                "/runtime/.claude-bridge",
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "foreground_cwd"):
                self.core.validate_agent_project("claude-bridge-rev", "/repo/claude-bridge")

    def test_reviewer_project_is_accepted_when_both_roots_match(self):
        with mock.patch.object(
            self.core,
            "get_agent_info",
            return_value={
                "cwd": "/repo/claude-bridge",
                "foreground_cwd": "/repo/claude-bridge/subdir",
            },
        ), mock.patch.object(
            self.core,
            "project_root",
            side_effect=[
                "/repo/claude-bridge",
                "/repo/claude-bridge",
            ],
        ):
            info = self.core.validate_agent_project("claude-bridge-rev", "/repo/claude-bridge")
            self.assertEqual(info["cwd"], "/repo/claude-bridge")


if __name__ == "__main__":
    unittest.main()
