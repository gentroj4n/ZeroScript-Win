# SPDX-License-Identifier: GPL-3.0-or-later
"""MCPClient multiplexing: concurrent read-only calls, serialized mutating calls,
per-request timeouts and fail-fast on process death - against a fake stdio MCP
server that answers requests on worker threads (so replies come out of order)."""
import json
import os
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

import bridge
from bridge import MCPClient

FAKE_SERVER = textwrap.dedent('''
    import json, os, sys, threading, time
    out_lock = threading.Lock()

    def send(obj):
        with out_lock:
            sys.stdout.write(json.dumps(obj) + "\\n")
            sys.stdout.flush()

    def handle(msg):
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "ro_sleep"}]}})
        elif method == "tools/call":
            p = msg["params"]
            args = p.get("arguments") or {}
            if p["name"] == "die":
                os._exit(3)
            start = time.time()
            time.sleep(float(args.get("delay", 0)))
            end = time.time()
            text = json.dumps({"tag": args.get("tag"), "start": start, "end": end})
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"content": [{"type": "text", "text": text}]}})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "id" in msg:
            threading.Thread(target=handle, args=(msg,), daemon=True).start()
''')


def _span(result):
    data = json.loads(result["text"])
    return data["start"], data["end"]


def _overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


class McpConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        script = os.path.join(self.temp.name, "fake_mcp.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(FAKE_SERVER)
        patcher = mock.patch.dict(bridge.READ_ONLY_MCP_TOOLS, {"fake": {"ro_sleep"}})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = MCPClient("fake", script, [])
        self.client.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.client.stop)

    def _run_parallel(self, jobs):
        """jobs: list of (name, args, timeout). Returns (threads, results)."""
        results = [None] * len(jobs)

        def worker(i, job):
            try:
                results[i] = self.client.call_tool(*job)
            except Exception as e:  # noqa: BLE001 - assertions inspect it
                results[i] = e

        threads = [threading.Thread(target=worker, args=(i, j)) for i, j in enumerate(jobs)]
        for t in threads:
            t.start()
        return threads, results

    def _join(self, threads):
        for t in threads:
            t.join(30)
            self.assertFalse(t.is_alive())

    def test_read_only_calls_overlap(self):
        t0 = time.time()
        threads, results = self._run_parallel(
            [("ro_sleep", {"delay": 1.0, "tag": i}, 15) for i in range(4)])
        self._join(threads)
        elapsed = time.time() - t0
        spans = [_span(r) for r in results]
        self.assertLess(elapsed, 3.0, f"4x1s read-only calls took {elapsed:.2f}s (serialized?)")
        for i in range(4):
            for j in range(i + 1, 4):
                self.assertTrue(_overlap(spans[i], spans[j]))

    def test_mutating_calls_serialize(self):
        threads, results = self._run_parallel(
            [("mut_sleep", {"delay": 0.6, "tag": i}, 15) for i in range(2)])
        self._join(threads)
        a, b = sorted(_span(r) for r in results)
        self.assertGreaterEqual(b[0], a[1] - 0.02, "mutating calls overlapped")

    def test_read_only_runs_during_mutating_call(self):
        threads, results = self._run_parallel([("mut_sleep", {"delay": 1.5, "tag": "m"}, 15)])
        time.sleep(0.3)
        t0 = time.time()
        ro = self.client.call_tool("ro_sleep", {"delay": 0.2, "tag": "r"}, 10)
        self.assertLess(time.time() - t0, 1.0, "read-only call waited for the mutating one")
        self._join(threads)
        self.assertTrue(_overlap(_span(ro), _span(results[0])))

    def test_mutate_lock_wait_respects_timeout(self):
        threads, _ = self._run_parallel([("mut_sleep", {"delay": 1.5, "tag": "m"}, 15)])
        time.sleep(0.3)
        t0 = time.time()
        with self.assertRaises(TimeoutError) as ctx:
            self.client.call_tool("mut_sleep", {"delay": 0, "tag": "x"}, 0.4)
        self.assertLess(time.time() - t0, 1.2)
        self.assertIn("busy with another change", str(ctx.exception))
        self.assertIn("fake", str(ctx.exception))
        self._join(threads)

    def test_timeout_only_affects_that_request(self):
        threads, results = self._run_parallel([
            ("ro_sleep", {"delay": 2.5, "tag": "slow"}, 0.5),
            ("ro_sleep", {"delay": 1.0, "tag": "ok1"}, 15),
            ("ro_sleep", {"delay": 0.2, "tag": "ok2"}, 15),
        ])
        self._join(threads)
        self.assertIsInstance(results[0], TimeoutError)
        self.assertEqual(json.loads(results[1]["text"])["tag"], "ok1")
        self.assertEqual(json.loads(results[2]["text"])["tag"], "ok2")
        self.assertEqual(self.client.pending, {})
        # Late reply for the timed-out id is dropped; the client keeps working.
        time.sleep(2.2)
        self.assertEqual(json.loads(
            self.client.call_tool("ro_sleep", {"tag": "after"}, 5)["text"])["tag"], "after")

    def test_process_death_fails_all_pending_promptly(self):
        errors = [None] * 4

        def worker(i):
            try:
                if i < 3:
                    self.client._request("tools/call", {"name": "ro_sleep",
                                                        "arguments": {"delay": 20}}, 30)
                else:
                    time.sleep(0.4)
                    self.client._request("tools/call", {"name": "die", "arguments": {}}, 30)
            except Exception as e:  # noqa: BLE001
                errors[i] = e

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        t0 = time.time()
        for t in threads:
            t.start()
        self._join(threads)
        self.assertLess(time.time() - t0, 5.0)
        for err in errors:
            self.assertIsInstance(err, RuntimeError)
            self.assertIn("exited", str(err))
        self.assertEqual(self.client.pending, {})
        # The client recovers with a fresh process on the next call.
        res = self.client.call_tool("ro_sleep", {"tag": "again"}, 10)
        self.assertEqual(json.loads(res["text"])["tag"], "again")

    def test_stop_fails_pending_and_request_after_stop_errors(self):
        errors = []

        def worker():
            try:
                self.client._request("tools/call", {"name": "ro_sleep",
                                                    "arguments": {"delay": 20}}, 30)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.4)
        self.client.stop()
        self._join([t])
        self.assertEqual(len(errors), 1)
        self.assertIn("stopped or restarted", str(errors[0]))
        with self.assertRaises(RuntimeError):
            self.client._request("tools/list", {}, 1)

    def test_request_ids_unique_under_threads(self):
        ids = []
        lock = threading.Lock()

        def grab():
            for _ in range(200):
                rid = self.client._next_id()
                with lock:
                    ids.append(rid)

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        self._join(threads)
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
