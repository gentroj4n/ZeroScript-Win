# SPDX-License-Identifier: GPL-3.0-or-later
import tempfile
import time
import unittest
from pathlib import Path

from workspace_agent.agent_control import AgentControlStore


class AgentControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AgentControlStore(str(Path(self.temp.name) / "control.json"), stale_after=0.05)

    def tearDown(self):
        self.temp.cleanup()

    def test_handoff_claim_and_completion_transfer_the_tool_lease(self):
        queued = self.store.handoff(
            {
                "goal": "Fix the project tests",
                "context": "The browser agent found a failing assertion.",
                "plan": ["Inspect the failing test", "Fix the cause", "Run the focused tests"],
                "acceptance_criteria": ["The focused tests pass"],
            },
            {"root": str(Path(self.temp.name)), "configured": True},
        )
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["owner"], "browser")
        self.assertFalse(self.store.browser_tool_allowed("workspace_read_file"))
        self.assertTrue(self.store.browser_tool_allowed("zeroscript_agent_control_status"))

        claim = self.store.claim("cli-session", queued["run_id"], browser_idle=True)
        self.assertTrue(claim["ok"])
        active = claim["control"]
        self.assertEqual(active["owner"], "cli")
        self.assertEqual(active["state"], "active")
        self.assertFalse(self.store.browser_tool_allowed("workspace_write_file"))
        self.assertTrue(self.store.browser_tool_allowed("zeroscript_agent_control_status"))

        finished = self.store.finish(queued["run_id"], "cli-session", "completed", "The focused test passed.")
        self.assertEqual(finished["state"], "completed")
        self.assertEqual(finished["owner"], "none")
        self.assertTrue(self.store.browser_tool_allowed("workspace_write_file"))

    def test_claim_waits_for_browser_tool_to_finish(self):
        queued = self.store.handoff({"goal": "Continue implementation"}, {})
        busy = self.store.claim("cli-session", queued["run_id"], browser_idle=False)
        self.assertFalse(busy["ok"])
        self.assertTrue(busy["busy"])
        self.assertEqual(self.store.status()["state"], "queued")
        self.assertFalse(self.store.browser_tool_allowed("workspace_write_file"))

        claim = self.store.claim("cli-session", queued["run_id"], browser_idle=True)
        self.assertTrue(claim["ok"])

    def test_stop_request_keeps_cli_ownership_until_safe_completion(self):
        active = self.store.start_run(
            {"goal": "Refactor module"},
            {"root": str(Path(self.temp.name)), "configured": True},
            "cli-session",
        )
        stopping = self.store.request_stop(active["run_id"])
        self.assertEqual(stopping["state"], "stop_requested")
        self.assertEqual(stopping["owner"], "cli")
        self.assertFalse(self.store.browser_tool_allowed("workspace_write_file"))

        done = self.store.finish(active["run_id"], "cli-session", "cancelled", "Stopped at a tool boundary.")
        self.assertEqual(done["state"], "cancelled")
        self.assertTrue(self.store.browser_tool_allowed("workspace_write_file"))

    def test_stale_cli_lease_is_marked_interrupted_and_can_be_reclaimed(self):
        active = self.store.start_run({"goal": "Inspect source"}, {}, "old-session")
        time.sleep(0.08)
        status = self.store.status()
        self.assertEqual(status["state"], "interrupted")
        self.assertEqual(status["owner"], "none")
        self.assertTrue(self.store.browser_tool_allowed("workspace_write_file"))

        claim = self.store.claim("new-session", active["run_id"])
        self.assertTrue(claim["ok"])
        self.assertEqual(claim["control"]["session_id"], "new-session")

    def test_interrupted_run_is_not_overwritten_by_new_work(self):
        active = self.store.start_run({"goal": "Recover this task"}, {}, "old-session")
        time.sleep(0.08)
        self.assertEqual(self.store.status()["state"], "interrupted")
        with self.assertRaisesRegex(RuntimeError, "zeroscript resume"):
            self.store.start_run({"goal": "Do something else"}, {}, "new-session")
        with self.assertRaisesRegex(RuntimeError, "zeroscript resume"):
            self.store.handoff({"goal": "Another task"}, {})
        self.assertEqual(self.store.status()["run_id"], active["run_id"])

    def test_invalid_handoff_goal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "goal is required"):
            self.store.handoff({"goal": "   "}, {})

    def test_handoff_only_persists_an_explicitly_configured_workspace(self):
        queued = self.store.handoff({"goal": "Run a test"}, {"root": "C:/fallback", "configured": False})
        self.assertEqual(queued["workspace"], "")


if __name__ == "__main__":
    unittest.main()
