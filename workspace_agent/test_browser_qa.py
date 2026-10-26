# SPDX-License-Identifier: GPL-3.0-or-later
import json
import tempfile
import unittest
from pathlib import Path

from workspace_agent import browser_qa
from workspace_agent.unity_recovery import UnityRecoveryError


class BrowserQATests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.port = self.root / "browser-port"
        self.port.mkdir()
        (self.port / "app.js").write_text("console.log('ok')", encoding="utf-8")
        (self.port / "styles.css").write_text("body { margin: 0; }", encoding="utf-8")
        (self.port / "package.json").write_text(json.dumps({"scripts": {"test": "node test.js"}}), encoding="utf-8")
        (self.port / "index.html").write_text(
            '<!doctype html><html><head><link rel="stylesheet" href="styles.css"></head>'
            '<body><script src="app.js"></script></body></html>',
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_static_audit_finds_existing_references_and_scripts(self):
        result = browser_qa.inspect_browser_port(str(self.root))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["script_and_style_reference_count"], 2)
        self.assertEqual(result["package_scripts"]["test"], "node test.js")
        self.assertEqual(result["missing_local_references"], [])

    def test_static_audit_reports_missing_reference(self):
        (self.port / "index.html").write_text('<script src="missing.js"></script>', encoding="utf-8")
        result = browser_qa.inspect_browser_port(str(self.root))
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["missing_local_references"][0]["url"], "missing.js")

    def test_reference_cannot_escape_browser_port(self):
        (self.port / "index.html").write_text('<script src="../secret.js"></script>', encoding="utf-8")
        result = browser_qa.inspect_browser_port(str(self.root))
        self.assertEqual(result["status"], "fail")
        self.assertIn("escapes", result["missing_local_references"][0]["error"])

    def test_inspection_requires_entry_point(self):
        (self.port / "index.html").unlink()
        with self.assertRaises(UnityRecoveryError):
            browser_qa.inspect_browser_port(str(self.root))

    def test_scenario_validation_accepts_safe_selector_steps(self):
        steps = browser_qa._validate_scenarios([
            {"action": "click", "selector": "#start"},
            {"action": "fill", "selector": "#name", "value": "Student"},
            {"action": "press", "selector": "body", "key": "ArrowUp"},
            {"action": "expect_text", "selector": "#status", "value": "Ready"},
            {"action": "wait_ms", "value": 300},
            {"action": "screenshot", "label": "after-start"},
        ])
        self.assertEqual(len(steps), 6)
        self.assertEqual(steps[1]["value"], "Student")
        self.assertEqual(steps[-1]["label"], "after-start")

    def test_scenario_validation_rejects_unsafe_or_unbounded_steps(self):
        with self.assertRaisesRegex(UnityRecoveryError, "allowlisted"):
            browser_qa._validate_scenarios([{"action": "press", "selector": "body", "key": "Control+W"}])
        with self.assertRaisesRegex(UnityRecoveryError, "selector"):
            browser_qa._validate_scenarios([{"action": "click"}])
        with self.assertRaisesRegex(UnityRecoveryError, "at most"):
            browser_qa._validate_scenarios([{"action": "wait_ms"}] * 41)
        with self.assertRaisesRegex(UnityRecoveryError, "screenshot label"):
            browser_qa._validate_scenarios([{"action": "screenshot", "label": "../outside"}])

    def test_interactive_scenarios_are_localhost_only(self):
        with self.assertRaisesRegex(UnityRecoveryError, "restricted to localhost"):
            browser_qa.run_browser_qa(str(self.root), "https://example.com", scenarios=[{"action": "wait_ms", "value": 10}])

    def test_run_browser_qa_rejects_unsupported_scheme(self):
        with self.assertRaisesRegex(UnityRecoveryError, "Use a local"):
            browser_qa.run_browser_qa(str(self.root), "file:///tmp/index.html")


if __name__ == "__main__":
    unittest.main()
