# SPDX-License-Identifier: GPL-3.0-or-later
"""Protocol-2 handshake, reader robustness, and multi-run control helper tests."""
import asyncio
import json
import unittest

import websockets

from zeroscript_cli.agent import (
    BridgeClient,
    ProtocolMismatchError,
    _armed_cli_sessions,
    _claimable_runs,
    _control_runs,
    _find_run,
    _my_active_runs,
    _response_run,
    resolve_max_parallel_subagents,
)


class FakeBridge:
    """Minimal websocket bridge: sends 'connected', answers registration per `register_reply`."""

    def __init__(self, register_reply, extra_after_register=None, close_after_register=False):
        self.register_reply = register_reply
        self.extra_after_register = extra_after_register or []
        self.close_after_register = close_after_register
        self.received = []
        self.connections = 0
        self.server = None
        self.url = ""

    async def handler(self, ws):
        self.connections += 1
        await ws.send(json.dumps({"type": "connected", "workspace": {"root": "C:/p", "configured": True}}))
        async for raw in ws:
            msg = json.loads(raw)
            self.received.append(msg)
            if msg.get("type") == "register_client":
                await ws.send(json.dumps(self.register_reply(msg)))
                for extra in self.extra_after_register:
                    await ws.send(json.dumps(extra))
                if self.close_after_register:
                    await ws.close()
            elif msg.get("type") == "call_tool":
                await ws.send(json.dumps({"type": "tool_result", "id": msg["id"], "ok": True, "text": "hi"}))

    async def __aenter__(self):
        self.server = await websockets.serve(self.handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = "ws://127.0.0.1:" + str(port)
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


def ok_reply(msg):
    return {"type": "client_registered", "ok": True, "session_id": msg["session_id"], "protocol_version": 2}


def run(coro):
    return asyncio.run(coro)


class BridgeProtocolTests(unittest.TestCase):
    def test_register_message_carries_protocol_and_session_id(self):
        async def scenario():
            async with FakeBridge(ok_reply) as bridge:
                client = BridgeClient(bridge.url, session_id="my-session")
                try:
                    await client.connect()
                    register = bridge.received[0]
                    self.assertEqual(register["type"], "register_client")
                    self.assertEqual(register["client"], "cli")
                    self.assertEqual(register["protocol_version"], 2)
                    self.assertEqual(register["session_id"], "my-session")
                    self.assertEqual(client.server_protocol_version, 2)
                finally:
                    await client.close()
        run(scenario())

    def test_default_session_id_format(self):
        client = BridgeClient("ws://127.0.0.1:1")
        self.assertRegex(client.session_id, r"^cli-[0-9a-f]{8}$")

    def test_protocol_mismatch_is_fatal_and_not_retried(self):
        async def scenario():
            reply = lambda msg: {"type": "client_registered", "ok": False, "error": "protocol_mismatch",
                                 "expected": 3, "message": "Bridge wants protocol 3; update the CLI."}
            async with FakeBridge(reply) as bridge:
                client = BridgeClient(bridge.url)
                with self.assertRaises(ProtocolMismatchError) as caught:
                    await client.connect()
                self.assertIn("update the CLI", str(caught.exception))
                self.assertIsNotNone(client.fatal_error)
                with self.assertRaises(ProtocolMismatchError):
                    await client.request("ping")
                self.assertEqual(bridge.connections, 1)
                await client.close()
        run(scenario())

    def test_old_bridge_without_ok_is_a_mismatch(self):
        async def scenario():
            reply = lambda msg: {"type": "client_registered", "client": "cli"}
            async with FakeBridge(reply) as bridge:
                client = BridgeClient(bridge.url)
                with self.assertRaises(ProtocolMismatchError) as caught:
                    await client.connect()
                self.assertIn("Update", str(caught.exception))
                self.assertEqual(bridge.connections, 1)
                await client.close()
        run(scenario())

    def test_non_numeric_id_does_not_kill_reader(self):
        async def scenario():
            junk = [{"type": "note", "id": "abc"}, {"type": "note", "id": {"x": 1}}]
            async with FakeBridge(ok_reply, extra_after_register=junk) as bridge:
                client = BridgeClient(bridge.url, session_id="s1")
                try:
                    await client.connect()
                    result = await client.call_tool("workspace_info", {})
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["text"], "hi")
                finally:
                    await client.close()
        run(scenario())

    def test_pending_futures_fail_promptly_when_socket_closes(self):
        async def scenario():
            async with FakeBridge(ok_reply) as bridge:
                client = BridgeClient(bridge.url, session_id="s1")
                try:
                    await client.connect()
                    loop = asyncio.get_running_loop()
                    future = loop.create_future()
                    client.pending[999] = future
                    for ws in list(bridge.server.connections):
                        await ws.close()
                    with self.assertRaises(RuntimeError):
                        await asyncio.wait_for(future, timeout=5)
                    self.assertFalse(client.connected.is_set())
                finally:
                    await client.close()
        run(scenario())

    def test_every_request_and_call_tool_override_send_session_id(self):
        async def scenario():
            async with FakeBridge(ok_reply) as bridge:
                client = BridgeClient(bridge.url, session_id="main")
                try:
                    await client.connect()
                    await client.call_tool("a", {})
                    await client.call_tool("b", {}, session_id="main:sub-1", workspace_root="C:/other")
                    calls = [m for m in bridge.received if m["type"] == "call_tool"]
                    self.assertEqual(calls[0]["session_id"], "main")
                    self.assertNotIn("workspace_root", calls[0])
                    self.assertEqual(calls[1]["session_id"], "main:sub-1")
                    self.assertEqual(calls[1]["workspace_root"], "C:/other")
                finally:
                    await client.close()
        run(scenario())


class ControlHelperTests(unittest.TestCase):
    CONTROL = {
        "version": 2,
        "runs": [
            {"run_id": "r1", "state": "queued", "target_session": None, "workspace": "C:/proj", "origin_role": "browser"},
            {"run_id": "r2", "state": "queued", "target_session": "me", "workspace": "C:/other"},
            {"run_id": "r3", "state": "queued", "target_session": None, "workspace": "C:/elsewhere"},
            {"run_id": "r4", "state": "active", "target_session": "me"},
            {"run_id": "r5", "state": "stop_requested", "target_session": "me"},
            {"run_id": "r6", "state": "active", "target_session": "other"},
            {"run_id": "r7", "state": "completed", "target_session": "me"},
            "garbage",
        ],
        "sessions": [
            {"session_id": "me", "role": "cli", "workspace": "C:/proj", "armed": True},
            {"session_id": "b", "role": "browser", "workspace": "C:/proj", "armed": True},
        ],
    }

    def test_control_runs_and_find_run(self):
        self.assertEqual(len(_control_runs(self.CONTROL)), 7)
        self.assertEqual(_control_runs(None), [])
        self.assertEqual(_control_runs({"runs": "x"}), [])
        self.assertEqual(_find_run(self.CONTROL, "r4")["state"], "active")
        self.assertIsNone(_find_run(self.CONTROL, "nope"))
        self.assertIsNone(_find_run(self.CONTROL, None))

    def test_my_active_runs_only_includes_own_active_and_stop_requested(self):
        self.assertEqual([r["run_id"] for r in _my_active_runs(self.CONTROL, "me")], ["r4", "r5"])
        self.assertEqual(_my_active_runs(self.CONTROL, "nobody"), [])

    def test_claimable_runs_prefers_targeted_then_workspace_unassigned(self):
        ids = [r["run_id"] for r in _claimable_runs(self.CONTROL, "me", "C:/proj")]
        self.assertEqual(ids, ["r2", "r1"])
        # Without an explicit root the session's registered workspace is used.
        self.assertEqual([r["run_id"] for r in _claimable_runs(self.CONTROL, "me")], ["r2", "r1"])
        self.assertEqual([r["run_id"] for r in _claimable_runs(self.CONTROL, "stranger", "C:/elsewhere")], ["r3"])

    def test_armed_cli_sessions_excludes_browser(self):
        self.assertEqual([s["session_id"] for s in _armed_cli_sessions(self.CONTROL)], ["me"])

    def test_response_run_accepts_new_and_legacy_shapes(self):
        self.assertEqual(_response_run({"run": {"run_id": "x"}})["run_id"], "x")
        self.assertEqual(_response_run({"control": self.CONTROL}, "r2")["run_id"], "r2")
        self.assertEqual(_response_run({"control": self.CONTROL}, session_id="me")["run_id"], "r5")
        self.assertEqual(_response_run({"control": {"run_id": "old"}})["run_id"], "old")
        self.assertEqual(_response_run({"control": {}}), {})

    def test_max_parallel_subagents_resolution(self):
        import os
        previous = os.environ.pop("ZS_MAX_PARALLEL_SUBAGENTS", None)
        try:
            self.assertEqual(resolve_max_parallel_subagents(None), 4)
            self.assertEqual(resolve_max_parallel_subagents(0), 0)
            os.environ["ZS_MAX_PARALLEL_SUBAGENTS"] = "7"
            self.assertEqual(resolve_max_parallel_subagents(None), 7)
            self.assertEqual(resolve_max_parallel_subagents(2), 2)
            os.environ["ZS_MAX_PARALLEL_SUBAGENTS"] = "junk"
            self.assertEqual(resolve_max_parallel_subagents(None), 4)
        finally:
            os.environ.pop("ZS_MAX_PARALLEL_SUBAGENTS", None)
            if previous is not None:
                os.environ["ZS_MAX_PARALLEL_SUBAGENTS"] = previous


if __name__ == "__main__":
    unittest.main()
