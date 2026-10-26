# SPDX-License-Identifier: GPL-3.0-or-later
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_agent import assetripper_jobs as jobs
from workspace_agent.unity_recovery import UnityRecoveryError


class FakeProcess:
    def __init__(self, pid=98765):
        self.pid = pid
        self.exit_code = None

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.exit_code = -15

    def kill(self):
        self.exit_code = -9

    def wait(self, timeout=None):
        if self.exit_code is None:
            raise jobs.subprocess.TimeoutExpired("fake", timeout)
        return self.exit_code


class AssetRipperJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.input = self.root / "YandereSimulator"
        self.workspace.mkdir()
        self.input.mkdir()
        self.executable = self.root / "AssetRipper.exe"
        self.executable.write_bytes(b"placeholder executable")

    def tearDown(self):
        for job_id in list(jobs._ACTIVE):
            process = jobs._ACTIVE.pop(job_id)
            if process.poll() is None:
                process.terminate()
        self.temp.cleanup()

    def test_start_status_cancel_persists_job_id(self):
        process = FakeProcess()
        with mock.patch.object(jobs.subprocess, "Popen", return_value=process):
            started = jobs.start_assetripper_export(
                str(self.workspace), str(self.executable), str(self.input),
            )
        self.assertEqual(started["status"], "running")
        self.assertTrue(started["job_id"].startswith("assetripper-"))
        self.assertEqual(jobs.assetripper_job_status(str(self.workspace), started["job_id"])["status"], "running")
        stopped = jobs.cancel_assetripper_job(str(self.workspace), started["job_id"])
        self.assertTrue(stopped["cancelled"])
        self.assertEqual(stopped["status"], "cancelled")
        self.assertTrue((self.workspace / "recovery" / "jobs" / (started["job_id"] + ".json")).is_file())

    def test_rejects_nonempty_output(self):
        output = self.workspace / "recovery" / "export"
        output.mkdir(parents=True)
        (output / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(UnityRecoveryError, "Refusing to overwrite"):
            jobs.start_assetripper_export(str(self.workspace), str(self.executable), str(self.input), "recovery/export")

    def test_rejects_output_inside_original_installation(self):
        source = self.workspace / "YandereSimulator"
        source.mkdir()
        with self.assertRaisesRegex(UnityRecoveryError, "cannot be inside"):
            jobs.start_assetripper_export(
                str(self.workspace), str(self.executable), str(source),
                "YandereSimulator/recovery",
            )

    def test_rejects_invalid_export_mode(self):
        with self.assertRaisesRegex(UnityRecoveryError, "mode must be one of"):
            jobs.start_assetripper_export(str(self.workspace), str(self.executable), str(self.input), mode="Everything")


if __name__ == "__main__":
    unittest.main()
