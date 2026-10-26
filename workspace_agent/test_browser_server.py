# SPDX-License-Identifier: GPL-3.0-or-later
import json
import socket
import tempfile
import unittest
from pathlib import Path

from workspace_agent import browser_server
from workspace_agent.unity_recovery import UnityRecoveryError


class BrowserServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.port_dir = self.root / "browser-port"
        self.port_dir.mkdir()
        (self.port_dir / "index.html").write_text("<!doctype html><title>QA</title>", encoding="utf-8")

    def tearDown(self):
        try:
            browser_server.stop_browser_server(str(self.root))
        except Exception:
            pass
        self.temp.cleanup()

    def _free_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_start_status_and_stop_static_server(self):
        result = browser_server.start_browser_server(str(self.root), port=self._free_port())
        self.assertEqual(result["status"], "started")
        self.assertTrue(result["running"])
        self.assertTrue(result["url"].startswith("http://127.0.0.1:"))
        status = browser_server.browser_server_status(str(self.root))
        self.assertTrue(status["running"])
        self.assertTrue(status["tracked_by_current_bridge"])
        stopped = browser_server.stop_browser_server(str(self.root))
        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(browser_server.browser_server_status(str(self.root))["running"])

    def test_server_refuses_missing_entry_point(self):
        (self.port_dir / "index.html").unlink()
        with self.assertRaises(UnityRecoveryError):
            browser_server.start_browser_server(str(self.root), port=self._free_port())

    def test_server_refuses_out_of_range_port(self):
        with self.assertRaisesRegex(UnityRecoveryError, "port must be between"):
            browser_server.start_browser_server(str(self.root), port=80)

    def test_status_without_state(self):
        self.assertFalse(browser_server.browser_server_status(str(self.root))["running"])


if __name__ == "__main__":
    unittest.main()
