# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from zeroscript_cli.hooks import HOOK_EVENTS, HookRunner


class HookRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.home.mkdir()
        self.project.mkdir()
        self.runner = HookRunner(self.project, self.home, timeout=2)

    def tearDown(self):
        self.temp.cleanup()

    def write_settings(self, root: Path, relative: str, config: dict):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_supported_hook_events_are_explicit(self):
        self.assertEqual(HOOK_EVENTS, {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"})

    def test_discovers_user_and_project_hooks_with_regex_matchers(self):
        self.write_settings(self.home, ".claude/settings.json", {
            "hooks": {"PreToolUse": [{"matcher": "workspace_.*", "hooks": [{"type": "command", "command": "echo user-hook"}]}]}
        })
        self.write_settings(self.project, ".claude/settings.json", {
            "hooks": {"PreToolUse": [{"matcher": "workspace_read_file", "hooks": [{"type": "command", "command": "echo project-hook"}]}]}
        })
        matched = self.runner.specs("PreToolUse", "workspace_read_file")
        self.assertEqual([item.command for item in matched], ["echo user-hook", "echo project-hook"])
        self.assertEqual(self.runner.specs("PreToolUse", "workspace_write_file"), [])

    def test_runs_matching_command_and_passes_json_payload(self):
        self.write_settings(self.project, ".claude/settings.json", {
            "hooks": {"PostToolUse": [{"matcher": "workspace_.*", "hooks": [{"type": "command", "command": "echo hook-ok"}]}]}
        })
        results = asyncio.run(self.runner.run("PostToolUse", {
            "tool_name": "workspace_read_file",
            "tool_input": {"path": "README.md"},
        }))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].returncode, 0)
        self.assertIn("hook-ok", results[0].output)

    def test_ignores_malformed_settings_and_invalid_matchers(self):
        bad = self.project / ".claude" / "settings.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.runner.specs("PreToolUse", "workspace_read_file"), [])
        bad.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "[", "hooks": [{"command": "echo should-not-run"}]}
        ]}}), encoding="utf-8")
        self.assertEqual(self.runner.specs("PreToolUse", "workspace_read_file"), [])

    def test_unknown_events_and_non_command_hooks_are_ignored(self):
        self.write_settings(self.project, ".claude/settings.json", {
            "hooks": {
                "UnknownEvent": [{"hooks": [{"command": "echo no"}]}],
                "Stop": [{"hooks": [{"type": "prompt", "prompt": "not a shell command"}]}],
            }
        })
        self.assertEqual(self.runner.specs("UnknownEvent"), [])
        self.assertEqual(self.runner.specs("Stop"), [])

    def test_symlinked_settings_file_is_ignored(self):
        target = self.root / "outside.json"
        target.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "echo unsafe"}]}]}}), encoding="utf-8")
        settings = self.project / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        try:
            settings.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks are unavailable in this environment")
        self.assertEqual(self.runner.specs("Stop"), [])


if __name__ == "__main__":
    unittest.main()
