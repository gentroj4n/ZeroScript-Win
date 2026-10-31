# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for workspace_agent.job_manager.

Real subprocesses (python -c / a socket server) are used so the lifecycle
is exercised end to end. Every job is stopped in tearDown.
"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

from workspace_agent import job_manager
from workspace_agent.unity_recovery import UnityRecoveryError


class JobManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.workspace = str(self.root)

    def tearDown(self):
        # Never leak a child process across tests.
        try:
            listing = job_manager.job_list(self.workspace)
        except Exception:
            listing = {"jobs": []}
        for job in listing.get("jobs", []):
            if job.get("status") in {"running", "starting", "cancelling", "untracked_running"}:
                try:
                    job_manager.stop_job(self.workspace, job["job_id"])
                except Exception:
                    pass
        job_manager._ACTIVE.clear()
        # Windows releases the log file handle asynchronously after the child
        # exits; retry the temp-dir cleanup briefly rather than failing on a
        # transient sharing violation.
        last_exc = None
        for _ in range(40):
            try:
                self._tmp.cleanup()
                return
            except PermissionError as exc:
                last_exc = exc
                time.sleep(0.05)
        if last_exc is not None:
            raise last_exc

    # ── validation ────────────────────────────────────────────────────────
    def test_rejects_non_list_command(self):
        with self.assertRaises(UnityRecoveryError):
            job_manager.start_job(self.workspace, "python -c print(1)")

    def test_rejects_empty_command(self):
        with self.assertRaises(UnityRecoveryError):
            job_manager.start_job(self.workspace, [])

    def test_rejects_non_string_argv(self):
        with self.assertRaises(UnityRecoveryError):
            job_manager.start_job(self.workspace, ["python", 42])

    def test_rejects_nul_byte(self):
        with self.assertRaises(UnityRecoveryError):
            job_manager.start_job(self.workspace, ["python", "a\x00b"])

    def test_rejects_cwd_outside_workspace(self):
        with self.assertRaises(UnityRecoveryError):
            job_manager.start_job(self.workspace, [sys.executable, "-c", "pass"], cwd="../")

    def test_rejects_missing_cwd(self):
        with self.assertRaises(UnityRecoveryError):
            job_manager.start_job(self.workspace, [sys.executable, "-c", "pass"], cwd="does-not-exist")

    def test_rejects_env_with_non_string_value(self):
        with self.assertRaises(UnityRecoveryError):
            job_manager.start_job(self.workspace, [sys.executable, "-c", "pass"], env={"X": 1})

    def test_unknown_job_id_rejected(self):
        with self.assertRaises(UnityRecoveryError):
            job_manager.job_status(self.workspace, "job-notahexid123")
        with self.assertRaises(UnityRecoveryError):
            job_manager.job_status(self.workspace, "../../etc/passwd")

    # ── lifecycle ─────────────────────────────────────────────────────────
    def test_start_runs_and_completes(self):
        result = job_manager.start_job(
            self.workspace,
            [sys.executable, "-c", "print('hello from job')"],
            label="hello",
        )
        job_id = result["job_id"]
        self.assertTrue(job_id.startswith("job-"))
        # Poll until the process finishes.
        deadline = time.time() + 10
        final = None
        while time.time() < deadline:
            final = job_manager.job_status(self.workspace, job_id)
            if final.get("status") not in {"starting", "running"}:
                break
            time.sleep(0.1)
        self.assertIsNotNone(final)
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(final.get("exit_code"), 0)
        logs = job_manager.job_logs(self.workspace, job_id)
        self.assertIn("hello from job", logs["content"])

    def test_immediate_exit_raises_with_log_path(self):
        with self.assertRaises(UnityRecoveryError) as ctx:
            job_manager.start_job(
                self.workspace,
                [sys.executable, "-c", "import sys; sys.exit(2)"],
            )
        self.assertIn("code 2", str(ctx.exception))

    def test_missing_command_reports_clearly(self):
        with self.assertRaises(UnityRecoveryError) as ctx:
            job_manager.start_job(self.workspace, ["definitely-not-a-real-binary-xyz"])
        self.assertIn("not found", str(ctx.exception).lower())

    def test_stop_running_job(self):
        # A job that sleeps forever; terminate should kill it.
        result = job_manager.start_job(
            self.workspace,
            [sys.executable, "-c", "import time; time.sleep(60)"],
            label="sleeper",
        )
        job_id = result["job_id"]
        time.sleep(0.2)
        stopped = job_manager.stop_job(self.workspace, job_id)
        self.assertTrue(stopped["cancelled"])
        self.assertEqual(stopped["status"], "cancelled")
        # Status afterwards must be terminal and not restart the process.
        after = job_manager.job_status(self.workspace, job_id)
        self.assertEqual(after["status"], "cancelled")

    def test_list_includes_jobs(self):
        a = job_manager.start_job(self.workspace, [sys.executable, "-c", "import time; time.sleep(0.5)"], label="a")
        listing = job_manager.job_list(self.workspace)
        ids = {j["job_id"] for j in listing["jobs"]}
        self.assertIn(a["job_id"], ids)
        self.assertEqual(listing["max_concurrent"], job_manager.MAX_CONCURRENT_JOBS)

    def test_untracked_job_never_killed(self):
        # Simulate a bridge restart: start, then clear _ACTIVE, then request stop.
        result = job_manager.start_job(
            self.workspace,
            [sys.executable, "-c", "import time; time.sleep(60)"],
            label="orphan",
        )
        job_id = result["job_id"]
        pid = result["pid"]
        job_manager._ACTIVE.pop(job_id, None)  # pretend a fresh bridge process
        stopped = job_manager.stop_job(self.workspace, job_id)
        self.assertFalse(stopped["cancelled"])
        self.assertIn("unverified PID", stopped["message"])
        # Cleanup: the child is still ours from this test process. On Windows
        # the log file handle stays open until the child actually exits, so
        # wait for it to die before tearDown tries to remove the temp dir.
        import os
        try:
            os.kill(pid, 15)
        except OSError:
            pass
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.05)

    def test_log_tail_drops_partial_first_line(self):
        # Write a small script rather than stuffing 500 statements into -c:
        # Windows caps a command line at ~8191 chars and CreateProcess would
        # truncate a longer -c argument into a syntax error.
        script = self.root / "emit_lines.py"
        script.write_text(
            "\n".join([f"print('line-{i}')" for i in range(500)]) + "\n",
            encoding="utf-8",
        )
        result = job_manager.start_job(
            self.workspace,
            [sys.executable, "-u", str(script)],
            label="many-lines",
        )
        job_id = result["job_id"]
        deadline = time.time() + 10
        while time.time() < deadline:
            if job_manager.job_status(self.workspace, job_id)["status"] not in {"starting", "running"}:
                break
            time.sleep(0.05)
        logs = job_manager.job_logs(self.workspace, job_id, max_bytes=2048)
        self.assertTrue(logs["truncated_start"])
        # Whatever came back must be complete lines only.
        for line in logs["content"].splitlines():
            if line:
                self.assertTrue(line.startswith("line-"), line)

    # ── wait_port ─────────────────────────────────────────────────────────
    def _free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_wait_port_returns_quickly_when_ready(self):
        port = self._free_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", port))
            server.listen(1)
            result = job_manager.wait_port(self.workspace, port, timeout_seconds=3.0)
        self.assertTrue(result["ready"])
        self.assertEqual(result["port"], port)

    def test_wait_port_times_out_cleanly(self):
        port = self._free_port()
        start = time.time()
        result = job_manager.wait_port(self.workspace, port, timeout_seconds=0.6)
        elapsed = time.time() - start
        self.assertFalse(result["ready"])
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertIn("did not accept", result["message"])

    def test_wait_port_rejects_bad_port(self):
        with self.assertRaises(UnityRecoveryError):
            job_manager.wait_port(self.workspace, 0)
        with self.assertRaises(UnityRecoveryError):
            job_manager.wait_port(self.workspace, 70000)

    # ── state file safety ─────────────────────────────────────────────────
    def test_state_file_outside_workspace_rejected(self):
        # job_id format check is the only surface; verified above.
        with self.assertRaises(UnityRecoveryError):
            job_manager.job_status(self.workspace, "job-../../escape")

    def test_state_is_persisted_between_calls(self):
        result = job_manager.start_job(
            self.workspace,
            [sys.executable, "-c", "import time; time.sleep(0.4)"],
            label="persisted",
        )
        job_id = result["job_id"]
        state_file = self.root / ".zeroscript" / "jobs" / f"{job_id}.json"
        self.assertTrue(state_file.is_file())
        data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(data["job_id"], job_id)
        self.assertEqual(data["label"], "persisted")


if __name__ == "__main__":
    unittest.main()
