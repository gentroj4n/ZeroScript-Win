# SPDX-License-Identifier: GPL-3.0-or-later
import tempfile
import unittest
from pathlib import Path

from workspace_agent import reconstruction_tracker as tracker
from workspace_agent.unity_recovery import UnityRecoveryError


class ReconstructionTrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("source.cs", "asset.json", "game.js", "game.test.js"):
            (self.root / name).write_text("// evidence\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_update_and_readiness_require_all_evidence(self):
        tracker.update_reconstruction_feature(str(self.root), "clock", "Clock and schedule", "implemented",
            source_evidence=[{"path": "source.cs", "locator": "ClockScript.AdvanceTime, lines 10-20"}],
            asset_evidence=["asset.json"], implementation_files=["game.js"], test_files=["game.test.js"])
        report = tracker.report_reconstruction_readiness(str(self.root), "clock")
        self.assertTrue(report["features"][0]["ready_for_review"])
        self.assertEqual(report["ready_for_review_count"], 1)

    def test_missing_evidence_is_not_ready(self):
        tracker.update_reconstruction_feature(str(self.root), "inventory", "Inventory", "implemented")
        report = tracker.report_reconstruction_readiness(str(self.root), "inventory")
        self.assertFalse(report["features"][0]["ready_for_review"])
        self.assertEqual(len(report["features"][0]["missing_evidence"]), 4)

    def test_rejects_path_escape_and_unknown_status(self):
        with self.assertRaises(UnityRecoveryError):
            tracker.update_reconstruction_feature(str(self.root), "bad", "Bad", source_evidence=[{"path": "../outside", "locator": "method"}])
        with self.assertRaisesRegex(UnityRecoveryError, "status"):
            tracker.update_reconstruction_feature(str(self.root), "bad", "Bad", status="verified")

    def test_preserves_prior_evidence_when_field_omitted(self):
        tracker.update_reconstruction_feature(str(self.root), "clock", "Clock", source_evidence=[{"path": "source.cs", "locator": "Tick"}])
        tracker.update_reconstruction_feature(str(self.root), "clock", "Clock", status="implemented")
        result = tracker.report_reconstruction_readiness(str(self.root), "clock")
        self.assertEqual(result["features"][0]["source_evidence"][0]["locator"], "Tick")


if __name__ == "__main__":
    unittest.main()
