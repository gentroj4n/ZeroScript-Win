# SPDX-License-Identifier: GPL-3.0-or-later
import json
import tempfile
import time
import unittest
from pathlib import Path

from workspace_agent import agent_control
from workspace_agent.agent_control import AgentControlStore


class AgentControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "control.json"
        self.store = AgentControlStore(str(self.path), stale_after=60)
        self.ws_a = str(self.root / "project-a")
        self.ws_b = str(self.root / "project-b")

    def tearDown(self):
        self.temp.cleanup()

    def info(self, root):
        return {"root": root, "configured": True}

    def test_single_handoff_claim_and_completion_transfer_the_tool_lease(self):
        self.store.register_session("tab-1", "browser", self.ws_a)
        self.store.register_session("cli-1", "cli", self.ws_a)
        self.store.arm("cli-1", self.ws_a)
        queued = self.store.handoff(
            {
                "goal": "Fix the project tests",
                "context": "The browser agent found a failing assertion.",
                "plan": ["Inspect the failing test", "Fix the cause"],
                "acceptance_criteria": ["The focused tests pass"],
            },
            self.info(self.ws_a),
            "tab-1",
        )
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["origin_session"], "tab-1")
        self.assertEqual(queued["origin_role"], "browser")
        self.assertEqual(queued["target_session"], "cli-1")
        self.assertFalse(self.store.browser_tool_allowed("workspace_read_file", "tab-1"))
        self.assertTrue(self.store.browser_tool_allowed("zeroscript_agent_control_status", "tab-1"))

        claim = self.store.claim("cli-1", queued["run_id"])
        self.assertTrue(claim["ok"])
        self.assertIsNone(claim["error"])
        self.assertEqual(claim["run"]["state"], "active")
        self.assertFalse(self.store.armed_sessions())  # claiming disarms
        self.assertFalse(self.store.browser_tool_allowed("workspace_write_file", "tab-1"))

        finished = self.store.finish(queued["run_id"], "cli-1", "completed", "Tests pass.")
        self.assertEqual(finished["state"], "completed")
        self.assertTrue(self.store.browser_tool_allowed("workspace_write_file", "tab-1"))

    def test_two_browser_sessions_hand_off_to_two_armed_clis_at_once(self):
        self.store.register_session("tab-1", "browser")
        self.store.register_session("tab-2", "browser")
        self.store.register_session("cli-1", "cli")
        self.store.register_session("cli-2", "cli")
        self.store.arm("cli-1", self.ws_a)
        time.sleep(0.01)
        self.store.arm("cli-2", self.ws_a)
        self.assertEqual([s["session_id"] for s in self.store.armed_sessions(self.ws_a)], ["cli-1", "cli-2"])

        run1 = self.store.handoff({"goal": "Task one"}, self.info(self.ws_a), "tab-1")
        run2 = self.store.handoff({"goal": "Task two"}, self.info(self.ws_a), "tab-2")
        self.assertEqual(run1["target_session"], "cli-1")  # armed longest
        self.assertEqual(run2["target_session"], "cli-2")  # cli-1 is already earmarked

        self.assertTrue(self.store.claim("cli-2")["ok"])
        self.assertTrue(self.store.claim("cli-1")["ok"])
        status = self.store.status()
        self.assertEqual({r["state"] for r in status["runs"]}, {"active"})
        self.assertFalse(self.store.armed_sessions())

        self.store.finish(run1["run_id"], "cli-1", "completed", "one")
        self.assertTrue(self.store.browser_tool_allowed("workspace_read_file", "tab-1"))
        self.assertFalse(self.store.browser_tool_allowed("workspace_read_file", "tab-2"))
        self.store.finish(run2["run_id"], "cli-2", "failed", "two")
        self.assertTrue(self.store.browser_tool_allowed("workspace_read_file", "tab-2"))

    def test_targeted_handoff_must_name_a_registered_cli_and_only_that_cli_claims(self):
        self.store.register_session("tab-1", "browser")
        self.store.register_session("tab-2", "browser")
        self.store.register_session("cli-1", "cli", self.ws_a)
        self.store.register_session("cli-2", "cli", self.ws_a)
        with self.assertRaisesRegex(ValueError, "ghost"):
            self.store.handoff({"goal": "x"}, self.info(self.ws_a), "tab-1", "ghost")
        with self.assertRaisesRegex(ValueError, "tab-2"):
            self.store.handoff({"goal": "x"}, self.info(self.ws_a), "tab-1", "tab-2")

        self.store.arm("cli-1", self.ws_a)  # armed, but the handoff names cli-2
        run = self.store.handoff({"goal": "For cli-2"}, self.info(self.ws_a), "tab-1", "cli-2")
        self.assertEqual(run["target_session"], "cli-2")
        refused = self.store.claim("cli-1", run["run_id"])
        self.assertFalse(refused["ok"])
        self.assertIn("cli-2", refused["error"])
        self.assertFalse(self.store.claim("cli-1")["ok"])  # nothing for cli-1
        self.assertTrue(self.store.claim("cli-2")["ok"])

    def test_origin_cannot_queue_a_second_run_until_the_first_is_terminal(self):
        self.store.register_session("tab-1", "browser")
        first = self.store.handoff({"goal": "First"}, {}, "tab-1")
        with self.assertRaisesRegex(ValueError, first["run_id"]):
            self.store.handoff({"goal": "Second"}, {}, "tab-1")
        self.assertEqual(self.store.request_stop(first["run_id"])["state"], "stopped")
        self.assertTrue(self.store.browser_tool_allowed("workspace_read_file", "tab-1"))
        self.store.handoff({"goal": "Second"}, {}, "tab-1")

    def test_handoff_auto_picks_armed_cli_by_workspace(self):
        self.store.register_session("tab-1", "browser")
        self.store.arm("cli-a", self.ws_a)
        self.store.arm("cli-b", self.ws_b)
        run = self.store.handoff({"goal": "In B"}, self.info(self.ws_b + "/../project-b"), "tab-1")
        self.assertEqual(run["target_session"], "cli-b")

        self.store.finish(self.store.claim("cli-b")["run"]["run_id"], "cli-b", "completed")
        other = self.store.handoff({"goal": "Nobody on C"}, self.info(str(self.root / "project-c")), "tab-1")
        self.assertIsNone(other["target_session"])
        self.assertEqual(other["state"], "queued")

    def test_unassigned_run_is_claimed_later_by_a_cli_on_the_same_workspace(self):
        self.store.register_session("tab-1", "browser")
        run = self.store.handoff({"goal": "Wait for a CLI"}, self.info(self.ws_a), "tab-1")
        self.assertIsNone(run["target_session"])
        self.assertFalse(self.store.browser_tool_allowed("workspace_read_file", "tab-1"))

        self.store.register_session("cli-wrong", "cli", self.ws_b)
        wrong = self.store.claim("cli-wrong")
        self.assertFalse(wrong["ok"])
        self.assertIsNone(wrong["run"])
        self.assertFalse(self.store.claim("cli-wrong", run["run_id"])["ok"])

        self.store.register_session("cli-right", "cli", self.ws_a)
        claim = self.store.claim("cli-right")
        self.assertTrue(claim["ok"])
        self.assertEqual(claim["run"]["run_id"], run["run_id"])
        self.assertEqual(claim["run"]["target_session"], "cli-right")
        self.assertFalse(self.store.claim("cli-right")["ok"])  # nothing left, and already busy

    def test_unregistering_a_target_makes_its_queued_run_unassigned_again(self):
        self.store.register_session("tab-1", "browser")
        self.store.arm("cli-1", self.ws_a)
        run = self.store.handoff({"goal": "Reassign me"}, self.info(self.ws_a), "tab-1")
        self.assertEqual(run["target_session"], "cli-1")
        self.store.unregister_session("cli-1")
        self.assertIsNone(self.store.run(run["run_id"])["target_session"])
        self.assertEqual(self.store.run(run["run_id"])["state"], "queued")
        self.assertEqual(self.store.armed_sessions(), [])
        self.store.arm("cli-2", self.ws_a)
        self.assertTrue(self.store.claim("cli-2")["ok"])

    def test_browser_tool_allowed_blocks_only_the_origin_session(self):
        self.store.register_session("tab-1", "browser")
        self.store.register_session("tab-2", "browser")
        self.store.arm("cli-1", self.ws_a)
        run = self.store.handoff({"goal": "Only tab-1 pauses"}, self.info(self.ws_a), "tab-1")
        for state_check in ("queued", "active", "stop_requested"):
            if state_check == "active":
                self.store.claim("cli-1")
            elif state_check == "stop_requested":
                self.store.request_stop(run["run_id"])
            self.assertEqual(self.store.run(run["run_id"])["state"], state_check)
            self.assertFalse(self.store.browser_tool_allowed("workspace_write_file", "tab-1"))
            self.assertTrue(self.store.browser_tool_allowed("zeroscript_agent_control_status", "tab-1"))
            self.assertTrue(self.store.browser_tool_allowed("workspace_write_file", "tab-2"))
            self.assertTrue(self.store.browser_tool_allowed("workspace_write_file", "cli-1"))
        self.store.finish(run["run_id"], "cli-1", "stopped", "Stopped at a checkpoint.")
        self.assertTrue(self.store.browser_tool_allowed("workspace_write_file", "tab-1"))

    def test_yield_to_browser_returns_the_tool_lease(self):
        self.store.register_session("tab-1", "browser")
        self.store.arm("cli-1", self.ws_a)
        run = self.store.handoff({"goal": "Hand it back"}, self.info(self.ws_a), "tab-1")
        self.store.claim("cli-1")
        yielded = self.store.yield_to_browser(run["run_id"], "cli-1", "Back to you")
        self.assertEqual(yielded["state"], "yielded")
        self.assertTrue(self.store.browser_tool_allowed("workspace_write_file", "tab-1"))
        with self.assertRaisesRegex(RuntimeError, run["run_id"]):
            self.store.finish(run["run_id"], "cli-1", "completed")

    def test_cli_started_runs_run_in_parallel_but_one_per_session(self):
        a = self.store.start_run({"goal": "Sub 1"}, self.info(self.ws_a), "cli-1")
        b = self.store.start_run({"goal": "Sub 2"}, self.info(self.ws_a), "cli-2")
        self.assertEqual((a["origin_session"], a["target_session"], a["origin_role"]), ("cli-1", "cli-1", "cli"))
        self.assertEqual(b["state"], "active")
        with self.assertRaisesRegex(RuntimeError, "cli-1"):
            self.store.start_run({"goal": "Again"}, self.info(self.ws_a), "cli-1")
        stopping = self.store.request_stop(a["run_id"])
        self.assertEqual(stopping["state"], "stop_requested")
        with self.assertRaisesRegex(RuntimeError, "cli-2"):
            self.store.heartbeat(a["run_id"], "cli-2")
        self.assertEqual(self.store.heartbeat(a["run_id"], "cli-1", "working")["progress"], "working")
        self.assertEqual(self.store.finish(a["run_id"], "cli-1", "cancelled")["state"], "stopped")
        self.assertEqual([r["run_id"] for r in self.store.status("cli-2")["runs"]], [b["run_id"]])
        self.assertEqual(len(self.store.status()["runs"]), 2)

    def test_stale_detection_is_per_run(self):
        store = AgentControlStore(str(self.path), stale_after=0.2)
        old = store.start_run({"goal": "Goes silent"}, {}, "cli-old")
        live = store.start_run({"goal": "Keeps beating"}, {}, "cli-live")
        queued = store.handoff({"goal": "Queued runs never go stale"}, {}, "tab-1")
        time.sleep(0.15)
        store.heartbeat(live["run_id"], "cli-live")
        time.sleep(0.12)  # old is ~0.27s silent, live ~0.12s
        self.assertEqual(store.run(old["run_id"])["state"], "stale")
        self.assertEqual(store.run(live["run_id"])["state"], "active")
        self.assertEqual(store.run(queued["run_id"])["state"], "queued")
        with self.assertRaisesRegex(RuntimeError, old["run_id"]):
            store.heartbeat(old["run_id"], "cli-old")
        self.assertFalse(store.browser_tool_allowed("workspace_read_file", "tab-1"))
        # Stale is persisted and terminal: the browser origin is released.
        self.assertEqual(json.loads(self.path.read_text())["runs"][old["run_id"]]["state"], "stale")

    def test_stale_handed_off_run_releases_its_browser_origin(self):
        store = AgentControlStore(str(self.path), stale_after=0.05)
        run = store.handoff({"goal": "CLI dies"}, {}, "tab-1")
        store.register_session("cli-1", "cli")
        store.claim("cli-1", run["run_id"])
        self.assertFalse(store.browser_tool_allowed("workspace_read_file", "tab-1"))
        time.sleep(0.08)
        self.assertTrue(store.browser_tool_allowed("workspace_read_file", "tab-1"))
        self.assertEqual(store.run(run["run_id"])["state"], "stale")

    def test_v1_file_is_discarded(self):
        self.path.write_text(json.dumps({
            "version": 1, "run_id": "zs-old", "state": "queued", "owner": "browser", "goal": "Legacy",
        }), encoding="utf-8")
        status = self.store.status()
        self.assertEqual(status, {"version": 2, "runs": [], "sessions": []})
        self.assertIsNone(self.store.run("zs-old"))
        self.store.start_run({"goal": "Fresh"}, {}, "cli-1")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["version"], 2)
        self.assertEqual(len(saved["runs"]), 1)
        self.assertIn("cli-1", saved["sessions"])

    def test_corrupt_file_starts_empty_and_state_persists_across_instances(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.status()["runs"], [])
        run = self.store.start_run({"goal": "Persisted"}, {}, "cli-1")
        again = AgentControlStore(str(self.path), stale_after=60)
        self.assertEqual(again.run(run["run_id"])["goal"], "Persisted")

    def test_terminal_runs_are_pruned_to_the_newest_fifty(self):
        ids = []
        for index in range(agent_control._MAX_TERMINAL_RUNS + 5):
            run = self.store.start_run({"goal": "Run " + str(index)}, {}, "cli-1")
            self.store.finish(run["run_id"], "cli-1", "completed", "done")
            ids.append(run["run_id"])
        live = self.store.start_run({"goal": "Still running"}, {}, "cli-1")
        remaining = {r["run_id"] for r in self.store.status()["runs"]}
        self.assertEqual(len(remaining), agent_control._MAX_TERMINAL_RUNS + 1)
        self.assertIn(live["run_id"], remaining)
        self.assertNotIn(ids[0], remaining)
        self.assertIn(ids[-1], remaining)
        self.assertIsNone(self.store.run(ids[0]))

    def test_status_filters_by_session_and_reset_clears_everything(self):
        self.store.register_session("tab-1", "browser")
        self.store.register_session("tab-2", "browser")
        run = self.store.handoff({"goal": "Mine"}, {}, "tab-1")
        self.assertEqual([r["run_id"] for r in self.store.status("tab-1")["runs"]], [run["run_id"]])
        self.assertEqual(self.store.status("tab-2")["runs"], [])
        self.assertEqual(len(self.store.status()["sessions"]), 2)
        self.store.reset_for_tests()
        self.assertEqual(self.store.status(), {"version": 2, "runs": [], "sessions": []})

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "goal is required"):
            self.store.handoff({"goal": "   "}, {}, "tab-1")
        with self.assertRaisesRegex(ValueError, "role"):
            self.store.register_session("x", "robot")
        with self.assertRaisesRegex(ValueError, "session_id is required"):
            self.store.handoff({"goal": "ok"}, {}, "")
        self.store.register_session("cli-1", "cli")
        with self.assertRaisesRegex(ValueError, "cli-1"):
            self.store.handoff({"goal": "ok"}, {}, "cli-1")
        with self.assertRaisesRegex(ValueError, "Unsupported final status"):
            self.store.finish("zs-none", "cli-1", "bogus")

    def test_handoff_only_persists_an_explicitly_configured_workspace(self):
        run = self.store.handoff({"goal": "Run a test"}, {"root": "C:/fallback", "configured": False}, "tab-1")
        self.assertEqual(run["workspace"], "")

    def test_disarm_and_set_workspace(self):
        with self.assertRaisesRegex(ValueError, "nope"):
            self.store.disarm("nope")
        with self.assertRaisesRegex(ValueError, "nope"):
            self.store.set_session_workspace("nope", self.ws_a)
        armed = self.store.arm("cli-1", self.ws_a)
        self.assertTrue(armed["armed"])
        self.assertEqual(self.store.set_session_workspace("cli-1", self.ws_b)["workspace"], self.ws_b)
        self.assertEqual(len(self.store.armed_sessions(self.ws_b)), 1)
        self.assertEqual(self.store.armed_sessions(self.ws_a), [])
        self.assertFalse(self.store.disarm("cli-1")["armed"])
        self.assertEqual(self.store.armed_sessions(), [])


if __name__ == "__main__":
    unittest.main()
