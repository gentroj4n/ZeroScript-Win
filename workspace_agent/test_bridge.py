# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

import websockets

import bridge
from bridge import MCPManager
from workspace_agent.agent_control import AgentControlStore
from workspace_agent.session_limits import InflightLimiter, load_limits
from workspace_agent.tools import Workspace


class WorkspaceBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = MCPManager.__new__(MCPManager)
        self.manager.workspace_settings_path = str(self.root / "settings.json")
        self.manager.workspace_root = str(self.root)
        self.manager.workspace_configured = False
        self.manager.workspace_source = "default"
        self.manager.workspace = Workspace(self.root)
        self.manager.workspace_error = None
        self.manager.clients = {}
        self.manager.index = {}
        import threading
        self.manager.index_lock = threading.RLock()

    def tearDown(self):
        self.temp.cleanup()

    def test_workspace_catalogue_builds_python_schema_defaults(self):
        catalogue = self.manager._workspace_tools()
        by_name = {item["name"]: item for item in catalogue}
        self.assertIn("workspace_tree", by_name)
        self.assertIn("workspace_search", by_name)
        self.assertIn("workspace_multi_edit", by_name)
        self.assertIn("workspace_replace_in_files", by_name)
        self.assertIn("workspace_create_file", by_name)
        multi_edit_properties = by_name["workspace_multi_edit"]["inputSchema"]["properties"]["edits"]["items"]["properties"]
        self.assertIs(multi_edit_properties["whole_file"]["default"], False)
        self.assertIn("workspace_recent_roots", by_name)
        self.assertIn("workspace_open_recent", by_name)
        self.assertIn("workspace_git_stage_file", by_name)
        self.assertIn("workspace_git_unstage_file", by_name)
        self.assertIn("workspace_git_commit", by_name)
        self.assertIn("workspace_git_changed_files", by_name)
        self.assertIn("workspace_run_task", by_name)
        self.assertIn("workspace_preview_image", by_name)
        self.assertIs(by_name["workspace_tree"]["inputSchema"]["properties"]["include_hidden"]["default"], False)
        self.assertIs(by_name["workspace_search"]["inputSchema"]["properties"]["regex"]["default"], False)
        self.assertIn("configured: false", by_name["workspace_info"]["description"])

    def test_bridge_dispatch_rejects_invalid_mcp_arguments_before_execution(self):
        self.manager.workspace_configured = True
        with mock.patch.object(self.manager.workspace, "read_file") as read_file:
            with self.assertRaisesRegex(RuntimeError, "Missing required property: path"):
                self.manager.call("workspace_read_file", {}, 5)
            read_file.assert_not_called()

    def test_bridge_dispatch_rejects_non_object_arguments(self):
        with self.assertRaisesRegex(RuntimeError, "must be a JSON object"):
            self.manager.call("workspace_read_file", ["main.py"], 5)

    def test_bridge_dispatch_accepts_valid_workspace_arguments(self):
        self.manager.workspace_configured = True
        self.manager.workspace.write_file("main.py", "print('ok')\n")
        result = self.manager.call("workspace_read_file", {"path": "main.py"}, 5)
        self.assertEqual(json.loads(result["text"])["content"], "print('ok')\n")

    def test_workspace_preview_image_returns_image_payload(self):
        image_path = self.root / "preview.png"
        # Valid PNG signature and minimal payload are sufficient for this
        # transport-level test; image decoding is handled by the provider.
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test-payload")
        self.manager.workspace_configured = True
        result = self.manager.call("workspace_preview_image", {"path": "preview.png"}, 5)
        self.assertIn('"mime_type": "image/png"', result["text"])
        self.assertEqual(result["images"][0]["mimeType"], "image/png")
        import base64
        self.assertEqual(base64.b64decode(result["images"][0]["data"]), image_path.read_bytes())

    def test_workspace_preview_image_accepts_relative_and_absolute_path_forms(self):
        output = self.root / "output"
        output.mkdir()
        image_path = output / "pallet_town.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test-payload")
        jpeg_path = output / "pallet_town_preview.jpg"
        jpeg_path.write_bytes(b"\xff\xd8\xff\xe0" + b"test-payload")
        self.manager.workspace_configured = True

        # Both common raster formats should validate by their file signatures.
        jpeg_result = self.manager.call("workspace_preview_image", {"path": "output/pallet_town_preview.jpg"}, 5)
        self.assertEqual(jpeg_result["images"][0]["mimeType"], "image/jpeg")

        # Workspace-relative paths should work with either separator style.
        for path in ("output/pallet_town.png", "output\\pallet_town.png"):
            with self.subTest(path=path):
                result = self.manager.call("workspace_preview_image", {"path": path}, 5)
                self.assertEqual(result["images"][0]["mimeType"], "image/png")

        # Absolute paths using forward slashes are common in generated tool calls
        # even on Windows; pathlib on Windows treats these as absolute paths.
        forward_absolute = str(image_path).replace("\\", "/")
        with self.subTest(path=forward_absolute):
            result = self.manager.call("workspace_preview_image", {"path": forward_absolute}, 5)
            self.assertEqual(result["images"][0]["mimeType"], "image/png")

        # Native absolute paths (backslashes on Windows) should also resolve.
        native_absolute = str(image_path)
        with self.subTest(path=native_absolute):
            result = self.manager.call("workspace_preview_image", {"path": native_absolute}, 5)
            self.assertEqual(result["images"][0]["mimeType"], "image/png")

        if os.name == "nt":
            backslash_absolute = str(image_path).replace("/", "\\\\")
            with self.subTest(path=backslash_absolute):
                result = self.manager.call("workspace_preview_image", {"path": backslash_absolute}, 5)
                self.assertEqual(result["images"][0]["mimeType"], "image/png")

    def test_workspace_preview_image_reports_path_and_signature_failures_separately(self):
        self.manager.workspace_configured = True
        with self.assertRaisesRegex(RuntimeError, "Image file not found"):
            self.manager.call("workspace_preview_image", {"path": "missing.png"}, 5)

        wrong_image = self.root / "wrong.png"
        wrong_image.write_bytes(b"not a png")
        with self.assertRaisesRegex(RuntimeError, "Image validation failed.*expected PNG signature"):
            self.manager.call("workspace_preview_image", {"path": "wrong.png"}, 5)

    def test_workspace_preview_image_accepts_safe_svg(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="red"/></svg>'
        (self.root / "preview.svg").write_bytes(svg)
        self.manager.workspace_configured = True
        result = self.manager.call("workspace_preview_image", {"path": "preview.svg"}, 5)
        self.assertEqual(result["images"][0]["mimeType"], "image/svg+xml")
        import base64
        self.assertEqual(base64.b64decode(result["images"][0]["data"]), svg)

    def test_workspace_preview_image_rejects_active_svg(self):
        (self.root / "unsafe.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', encoding="utf-8")
        self.manager.workspace_configured = True
        with self.assertRaisesRegex(RuntimeError, "active script"):
            self.manager.call("workspace_preview_image", {"path": "unsafe.svg"}, 5)

    def test_workspace_preview_image_rejects_text_and_oversized_files(self):
        (self.root / "notes.txt").write_text("not an image", encoding="utf-8")
        self.manager.workspace_configured = True
        with self.assertRaisesRegex(RuntimeError, "Unsupported image type"):
            self.manager.call("workspace_preview_image", {"path": "notes.txt"}, 5)
        (self.root / "huge.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (8 * 1024 * 1024))
        with self.assertRaisesRegex(RuntimeError, "8 MB"):
            self.manager.call("workspace_preview_image", {"path": "huge.png"}, 5)

    def test_unconfigured_workspace_blocks_project_access(self):
        info = self.manager._call_workspace("workspace_info", {})
        self.assertFalse(info["configured"])
        with self.assertRaisesRegex(RuntimeError, "No project workspace has been selected"):
            self.manager._call_workspace("workspace_list_files", {})
        with self.assertRaisesRegex(RuntimeError, "No project workspace has been selected"):
            self.manager._call_workspace("workspace_run_command", {"command": ["python", "--version"]})

    def test_recent_workspace_list_and_activation(self):
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        self.manager._call_workspace("workspace_set_root", {"path": str(first)})
        self.manager._call_workspace("workspace_set_root", {"path": str(second)})
        recent = self.manager._call_workspace("workspace_recent_roots", {})["recent_roots"]
        self.assertEqual([item["path"] for item in recent[:2]], [str(second), str(first)])
        self.assertTrue(recent[0]["current"])
        with open(self.manager.workspace_settings_path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["recent_roots"][:2], [str(second), str(first)])
        activated = self.manager._call_workspace("workspace_open_recent", {"path": str(first)})
        self.assertEqual(activated["root"], str(first))
        self.assertEqual(self.manager.workspace_source, "recent")
        with self.assertRaisesRegex(RuntimeError, "not in the saved recent-workspace list"):
            self.manager._call_workspace("workspace_open_recent", {"path": str(self.root)})

    def test_bridge_routes_preview_replace_to_selected_workspace(self):
        self.manager.workspace_configured = True
        preview = {"applied": False, "preview_hash": "hash", "files_changed": 1}
        with mock.patch.object(self.manager.workspace, "replace_in_files", return_value=preview) as replace:
            result = self.manager._call_workspace("workspace_replace_in_files", {
                "query": "old", "replacement": "new", "apply": False,
            })
        self.assertEqual(result["preview_hash"], "hash")
        replace.assert_called_once_with("old", "new", ".", 100, False, False, False, "")

    def test_bridge_routes_multi_edit_to_selected_workspace(self):
        self.manager.workspace_configured = True
        self.manager.workspace.write_file("one.py", "VALUE = 1\n")
        self.manager.workspace.write_file("two.py", "VALUE = 2\n")
        result = self.manager._call_workspace("workspace_multi_edit", {
            "edits": [
                {"path": "one.py", "old_string": "VALUE = 1", "new_string": "VALUE = 10"},
                {"path": "two.py", "old_string": "VALUE = 2", "new_string": "VALUE = 20"},
            ]
        })
        self.assertEqual(result["files_changed"], 2)
        self.assertEqual(self.manager.workspace.read_file("one.py")["content"], "VALUE = 10\n")
        self.assertEqual(self.manager.workspace.read_file("two.py")["content"], "VALUE = 20\n")

    def test_bridge_routes_declared_package_task(self):
        self.manager.workspace_configured = True
        with mock.patch.object(self.manager.workspace, "run_task", return_value={"exit_code": 0, "task": "test"}) as run_task:
            result = self.manager._call_workspace("workspace_run_task", {"task": "test", "timeout": 30})
        self.assertEqual(result["task"], "test")
        run_task.assert_called_once_with("test", 30)

    def test_bridge_routes_changed_file_status(self):
        self.manager.workspace_configured = True
        with mock.patch.object(self.manager.workspace, "git_changed_files", return_value={"exit_code": 0, "count": 1, "files": []}) as changed:
            result = self.manager._call_workspace("workspace_git_changed_files", {})
        self.assertEqual(result["count"], 1)
        changed.assert_called_once_with()

    def test_bridge_routes_git_stage_and_commit_tools(self):
        self.manager.workspace_configured = True
        with mock.patch.object(self.manager.workspace, "git_stage_file", return_value={"exit_code": 0}) as stage:
            self.assertEqual(self.manager._call_workspace("workspace_git_stage_file", {"path": "main.py"})["exit_code"], 0)
            stage.assert_called_once_with("main.py")
        with mock.patch.object(self.manager.workspace, "git_unstage_file", return_value={"exit_code": 0}) as unstage:
            self.assertEqual(self.manager._call_workspace("workspace_git_unstage_file", {"path": "main.py"})["exit_code"], 0)
            unstage.assert_called_once_with("main.py")
        with mock.patch.object(self.manager.workspace, "git_commit", return_value={"exit_code": 0}) as commit:
            self.assertEqual(self.manager._call_workspace("workspace_git_commit", {"message": "test"})["exit_code"], 0)
            commit.assert_called_once_with("test")

    def test_explicit_workspace_selection_persists_and_unlocks_tools(self):
        project = self.root / "project"
        project.mkdir()
        (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
        result = self.manager._call_workspace("workspace_set_root", {"path": str(project)})
        self.assertTrue(result["configured"])
        self.assertEqual(self.manager.workspace_root, str(project.resolve()))
        self.assertTrue(self.manager.workspace_configured)
        with open(self.manager.workspace_settings_path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["root"], str(project.resolve()))
        listing = self.manager._call_workspace("workspace_list_files", {})
        self.assertEqual([item["path"] for item in listing["files"]], ["main.py"])


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


class SessionScopingTests(unittest.TestCase):
    """safe_call scopes EVERY workspace-dependent tool to the calling session."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.default_root = self.root / "default"
        self.root_a = self.root / "a"
        self.root_b = self.root / "b"
        for folder in (self.default_root, self.root_a, self.root_b):
            folder.mkdir()
        self.manager = MCPManager.__new__(MCPManager)
        self.manager.workspace_settings_path = str(self.root / "settings.json")
        self.manager.workspace_root = str(self.default_root.resolve())
        self.manager.workspace_configured = True
        self.manager.workspace_source = "saved"
        self.manager.workspace = Workspace(self.default_root)
        self.manager.workspace_error = None
        self.manager.workspace_history = [self.manager.workspace_root]
        self.manager.clients = {}
        self.manager.index = {}
        self.manager.index_lock = threading.RLock()
        self.store = AgentControlStore(str(self.root / "control.json"), stale_after=60)
        patches = [
            mock.patch.object(bridge, "mgr", self.manager),
            mock.patch.object(bridge, "agent_control", self.store),
            mock.patch.dict(bridge.session_roots, {}, clear=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.temp.cleanup()

    def _info(self, session, workspace_root=None):
        result = bridge.safe_call("workspace_info", {}, 5, workspace_root, session)
        self.assertTrue(result["ok"], result)
        return json.loads(result["text"])

    def test_per_call_root_reaches_job_manager_without_touching_global(self):
        with mock.patch.object(bridge.job_manager, "job_list", return_value={"jobs": []}) as job_list:
            result = bridge.safe_call("workspace_job_list", {}, 5, str(self.root_a), "tab-1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(job_list.call_args.args[0], str(self.root_a.resolve()))
        self.assertEqual(self.manager.workspace_root, str(self.default_root.resolve()))

    def test_workspace_set_root_is_session_local(self):
        result = bridge.safe_call("workspace_set_root", {"path": str(self.root_a)}, 5, None, "tab-1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self._info("tab-1")["root"], str(self.root_a.resolve()))
        self.assertEqual(self._info("tab-2")["root"], str(self.default_root.resolve()))
        self.assertEqual(self.manager.workspace_root, str(self.default_root.resolve()))
        # Another session picks a different folder without disturbing tab-1.
        bridge.safe_call("workspace_set_root", {"path": str(self.root_b)}, 5, None, "tab-2")
        self.assertEqual(self._info("tab-1")["root"], str(self.root_a.resolve()))
        self.assertEqual(self._info("tab-2")["root"], str(self.root_b.resolve()))
        with open(self.manager.workspace_settings_path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["root"], str(self.default_root.resolve()))
        self.assertIn(str(self.root_a.resolve()), saved["recent_roots"])
        self.assertIn(str(self.root_a.resolve()), self.manager.workspace_history)

    def test_call_root_beats_session_root_and_session_workspace_is_recorded(self):
        self.store.register_session("tab-1", "browser")
        bridge.safe_call("workspace_set_root", {"path": str(self.root_a)}, 5, None, "tab-1")
        sessions = {item["session_id"]: item for item in self.store.status()["sessions"]}
        self.assertEqual(os.path.normcase(sessions["tab-1"]["workspace"]), os.path.normcase(str(self.root_a.resolve())))
        self.assertEqual(self._info("tab-1", str(self.root_b))["root"], str(self.root_b.resolve()))
        self.assertEqual(self._info("tab-1")["root"], str(self.root_a.resolve()))

    def test_file_lock_timeout_maps_to_file_locked(self):
        def boom(*_args, **_kwargs):
            raise bridge.FileLockTimeout("a.txt", "someone-else", 1.0)

        with mock.patch.object(MCPManager, "call", boom):
            result = bridge.safe_call("workspace_write_file", {}, 5, None, "tab-1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "file_locked")

    def test_tool_runs_with_session_as_file_lock_owner(self):
        from workspace_agent import file_locks
        seen = []
        original = MCPManager.call

        def spy(manager, *args, **kwargs):
            seen.append(file_locks.current_owner())
            return {"text": "{}", "images": []}

        with mock.patch.object(MCPManager, "call", spy):
            bridge.safe_call("workspace_git_status", {}, 5, None, "cli-1:sub-2")
        self.assertEqual(seen, ["cli-1:sub-2"])


class LimiterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.running = 0
        self.peak = 0

    def _slow_call(self, *_args):
        self.running += 1
        self.peak = max(self.peak, self.running)
        time.sleep(0.15)
        self.running -= 1
        return {"ok": True, "text": "done", "images": []}

    async def _run(self, limiter, sessions):
        sockets = {sid: FakeSocket() for sid in set(sessions)}
        with mock.patch.object(bridge, "tool_limiter", limiter), mock.patch.object(bridge, "safe_call", self._slow_call):
            tasks = [
                bridge.run_tool_task(sockets[sid], "workspace_info", {}, 5, i, "browser", None, sid)
                for i, sid in enumerate(sessions)
            ]
            await asyncio.gather(*tasks)
        return [msg for sock in sockets.values() for msg in sock.sent]

    async def test_per_session_limit_queues_calls(self):
        with mock.patch.object(bridge, "broadcast_status", mock.AsyncMock()):
            results = await self._run(InflightLimiter(16, 1, 5), ["tab-1", "tab-1", "tab-1"])
        self.assertEqual([r["ok"] for r in results], [True, True, True])
        self.assertEqual(self.peak, 1)

    async def test_other_sessions_run_in_parallel(self):
        with mock.patch.object(bridge, "broadcast_status", mock.AsyncMock()):
            await self._run(InflightLimiter(16, 1, 5), ["tab-1", "tab-2", "tab-3"])
        self.assertEqual(self.peak, 3)

    async def test_global_limit_applies_across_sessions(self):
        with mock.patch.object(bridge, "broadcast_status", mock.AsyncMock()):
            await self._run(InflightLimiter(2, 4, 5), ["a", "b", "c", "d"])
        self.assertEqual(self.peak, 2)

    async def test_queue_timeout_returns_busy(self):
        with mock.patch.object(bridge, "broadcast_status", mock.AsyncMock()):
            results = await self._run(InflightLimiter(16, 1, 0.03), ["tab-1", "tab-1"])
        kinds = sorted(r.get("kind", "ok") for r in results)
        self.assertEqual(kinds, ["busy", "ok"])
        busy = next(r for r in results if r.get("kind") == "busy")
        self.assertFalse(busy["ok"])
        self.assertIn("Too many tool calls in flight", busy["error"])

    async def test_zero_disables_limits(self):
        with mock.patch.object(bridge, "broadcast_status", mock.AsyncMock()):
            await self._run(InflightLimiter(0, 0, 0), ["tab-1"] * 6)
        self.assertEqual(self.peak, 6)

    def test_env_wins_over_config(self):
        cfg = {"max_inflight": 5, "max_inflight_per_session": 2, "queue_timeout": 9}
        self.assertEqual(load_limits(cfg, {}), {"max_inflight": 5, "max_inflight_per_session": 2, "queue_timeout": 9.0})
        env = {"ZS_MAX_INFLIGHT": "0", "ZS_QUEUE_TIMEOUT": "3"}
        self.assertEqual(load_limits(cfg, env), {"max_inflight": 0, "max_inflight_per_session": 2, "queue_timeout": 3.0})
        self.assertEqual(load_limits({}, {})["max_inflight"], 16)
        self.assertEqual(load_limits({}, {})["max_inflight_per_session"], 4)
        self.assertEqual(load_limits({}, {})["queue_timeout"], 120.0)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AgentControlStore(str(Path(self.temp.name) / "control.json"), stale_after=60)
        for patch in (
            mock.patch.object(bridge, "agent_control", self.store),
            mock.patch.object(bridge, "agent_control_lock", asyncio.Lock()),
            mock.patch.object(bridge, "_roblox_studio_app_running", return_value=False),
            mock.patch.object(bridge, "broadcast_status", mock.AsyncMock()),
            mock.patch.object(bridge, "tool_limiter", InflightLimiter(16, 4, 5)),
            mock.patch.dict(bridge.session_roots, {}, clear=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.server = await websockets.serve(bridge.handler, "127.0.0.1", 0)
        self.addCleanup(lambda: None)
        self.url = "ws://127.0.0.1:%d" % self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def _connect(self):
        ws = await websockets.connect(self.url)
        connected = json.loads(await ws.recv())
        self.assertEqual(connected["type"], "connected")
        self.assertEqual(connected["protocol_version"], 2)
        self.assertIn("max_inflight", connected["load"])
        return ws

    async def _rpc(self, ws, message, expect):
        await ws.send(json.dumps(message))
        while True:
            reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if reply.get("type") == expect and reply.get("id") == message.get("id"):
                return reply

    async def test_register_with_version(self):
        ws = await self._connect()
        reply = await self._rpc(ws, {"type": "register_client", "id": 1, "client": "cli", "protocol_version": 2,
                                     "session_id": "cli-xyz"}, "client_registered")
        self.assertEqual(reply, {"type": "client_registered", "id": 1, "ok": True, "client": "cli",
                                 "session_id": "cli-xyz", "protocol_version": 2})
        self.assertEqual([s["session_id"] for s in self.store.status()["sessions"]], ["cli-xyz"])
        self.assertEqual(self.store.status()["sessions"][0]["role"], "cli")
        await ws.close()

    async def test_register_assigns_session_when_missing(self):
        ws = await self._connect()
        reply = await self._rpc(ws, {"type": "register_client", "id": 1, "client": "browser", "protocol_version": 2},
                                "client_registered")
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["session_id"].startswith("browser-"))
        await ws.close()

    async def test_legacy_register_is_accepted_with_legacy_session(self):
        ws = await self._connect()
        reply = await self._rpc(ws, {"type": "register_client", "id": 1, "client": "cli"}, "client_registered")
        self.assertTrue(reply["ok"])
        self.assertRegex(reply["session_id"], r"^cli-legacy-[0-9a-f]{8}$")
        self.assertEqual(reply["protocol_version"], 2)
        await ws.close()

    async def test_version_mismatch_is_rejected_and_closed(self):
        ws = await self._connect()
        reply = await self._rpc(ws, {"type": "register_client", "id": 1, "client": "browser", "protocol_version": 1},
                                "client_registered")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "protocol_mismatch")
        self.assertEqual(reply["expected"], 2)
        self.assertIn("Update your ZeroScript", reply["message"])
        with self.assertRaises(websockets.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 5)
        self.assertEqual(self.store.status()["sessions"], [])

    async def test_browser_tool_gate_is_per_session_and_disconnect_cleans_up(self):
        ws = await self._connect()
        await self._rpc(ws, {"type": "register_client", "id": 1, "client": "browser", "protocol_version": 2,
                             "session_id": "browser-main"}, "client_registered")
        for rid, tab in ((2, "browser-tab-1"), (3, "browser-tab-2")):
            reply = await self._rpc(ws, {"type": "call_tool", "id": rid, "name": "workspace_info",
                                         "arguments": {}, "session_id": tab}, "tool_result")
            self.assertTrue(reply["ok"], reply)
        ids = {s["session_id"] for s in self.store.status()["sessions"]}
        self.assertEqual(ids, {"browser-main", "browser-tab-1", "browser-tab-2"})
        run = self.store.handoff({"goal": "port the thing"}, {}, "browser-tab-1")
        blocked = await self._rpc(ws, {"type": "call_tool", "id": 4, "name": "workspace_info",
                                       "arguments": {}, "session_id": "browser-tab-1"}, "tool_result")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["kind"], "control")
        free = await self._rpc(ws, {"type": "call_tool", "id": 5, "name": "workspace_info",
                                    "arguments": {}, "session_id": "browser-tab-2"}, "tool_result")
        self.assertTrue(free["ok"], free)
        await ws.close()
        for _ in range(100):
            if not self.store.status()["sessions"]:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.store.status()["sessions"], [])
        self.assertNotEqual(self.store.run(run["run_id"])["state"], "queued")

    # -- stage 2: concurrent multi-run agent control ------------------------

    def _root(self, name):
        path = Path(self.temp.name) / name
        path.mkdir(exist_ok=True)
        return str(path.resolve())

    async def _register(self, role, session):
        ws = await self._connect()
        reply = await self._rpc(ws, {"type": "register_client", "id": "reg", "client": role,
                                     "protocol_version": 2, "session_id": session}, "client_registered")
        self.assertTrue(reply["ok"], reply)
        self.addCleanup(lambda: asyncio.ensure_future(ws.close()))
        return ws

    async def _control(self, ws, mtype, **fields):
        return await self._rpc(ws, {"type": mtype, "id": "c-" + mtype, **fields}, "agent_control_result")

    async def _arm(self, session, root):
        ws = await self._register("cli", session)
        reply = await self._control(ws, "agent_arm", session_id=session, workspace_root=root)
        self.assertTrue(reply["ok"], reply)
        return ws

    async def _handoff(self, ws, session, root, **extra):
        reply = await self._rpc(ws, {"type": "call_tool", "id": "h-" + session, "name": "zeroscript_handoff_to_cli",
                                     "arguments": {"goal": "do it", **extra}, "session_id": session,
                                     "workspace_root": root}, "tool_result")
        return reply

    async def test_two_clis_can_be_armed_at_once(self):
        root = self._root("proj")
        await self._arm("cli-a", root)
        reply_ws = await self._register("cli", "cli-b")
        reply = await self._control(reply_ws, "agent_arm", session_id="cli-b", workspace_root=root)
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(reply["armed"])
        self.assertEqual(reply["control"]["version"], 2)
        armed = [s["session_id"] for s in self.store.armed_sessions(root)]
        self.assertEqual(armed, ["cli-a", "cli-b"])
        gone = await self._control(reply_ws, "agent_disarm", session_id="cli-b")
        self.assertTrue(gone["ok"] and gone["armed"] is False, gone)
        self.assertEqual([s["session_id"] for s in self.store.armed_sessions(root)], ["cli-a"])

    async def test_handoff_honours_target_and_auto_picks_other_armed_cli(self):
        root = self._root("proj")
        await self._arm("cli-a", root)
        await self._arm("cli-b", root)
        browser = await self._register("browser", "browser-main")
        first = await self._handoff(browser, "tab-1", root, target_session_id="cli-b")
        self.assertTrue(first["ok"], first)
        self.assertEqual(json.loads(first["text"])["target_session"], "cli-b")
        second = await self._handoff(browser, "tab-2", root)
        self.assertTrue(second["ok"], second)
        # cli-b is already earmarked, so the longest-armed free CLI is picked.
        self.assertEqual(json.loads(second["text"])["target_session"], "cli-a")
        bad = await self._handoff(browser, "tab-3", root, target_session_id="nobody")
        self.assertFalse(bad["ok"])

    async def test_handoff_refused_when_no_cli_is_armed_for_the_workspace(self):
        await self._arm("cli-a", self._root("other"))
        browser = await self._register("browser", "browser-main")
        reply = await self._handoff(browser, "tab-1", self._root("proj"))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["kind"], "control")
        self.assertEqual(self.store.status()["runs"], [])

    async def test_control_status_tool_lists_armed_cli_sessions(self):
        root = self._root("proj")
        await self._arm("cli-a", root)
        browser = await self._register("browser", "browser-main")
        reply = await self._rpc(browser, {"type": "call_tool", "id": 9, "name": "zeroscript_agent_control_status",
                                          "arguments": {}, "session_id": "tab-1"}, "tool_result")
        self.assertTrue(reply["ok"], reply)
        state = json.loads(reply["text"])
        self.assertEqual([s["session_id"] for s in state["armed_cli_sessions"]], ["cli-a"])
        self.assertEqual(state["version"], 2)

    async def test_claim_rejects_workspace_mismatch(self):
        root_a, root_b = self._root("a"), self._root("b")
        run = self.store.handoff({"goal": "g"}, {"root": root_a, "configured": True}, "tab-1")
        wrong = await self._register("cli", "cli-b")
        reply = await self._control(wrong, "agent_claim", session_id="cli-b", run_id=run["run_id"], workspace_root=root_b)
        self.assertFalse(reply["ok"], reply)
        self.assertIn("workspace", reply["error"])
        self.assertEqual(self.store.run(run["run_id"])["state"], "queued")
        right = await self._register("cli", "cli-a")
        reply = await self._control(right, "agent_claim", session_id="cli-a", run_id=run["run_id"], workspace_root=root_a)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["run"]["state"], "active")
        self.assertEqual(reply["run"]["target_session"], "cli-a")

    async def test_browser_agent_stop_only_for_its_own_run(self):
        root = self._root("proj")
        run = self.store.handoff({"goal": "g"}, {"root": root, "configured": True}, "tab-1")
        browser = await self._register("browser", "browser-main")
        other = await self._control(browser, "agent_stop", session_id="tab-2", run_id=run["run_id"])
        self.assertFalse(other["ok"], other)
        self.assertEqual(self.store.run(run["run_id"])["state"], "queued")
        arm = await self._control(browser, "agent_arm", session_id="tab-1", workspace_root=root)
        self.assertFalse(arm["ok"], arm)
        own = await self._control(browser, "agent_stop", session_id="tab-1", run_id=run["run_id"])
        self.assertTrue(own["ok"], own)
        self.assertEqual(own["run"]["state"], "stopped")

    async def test_yield_refused_while_session_has_calls_in_flight(self):
        root = self._root("proj")
        cli = await self._register("cli", "cli-a")
        started = await self._control(cli, "agent_start", session_id="cli-a", goal="solo", workspace_root=root)
        self.assertTrue(started["ok"], started)
        run_id = started["run"]["run_id"]
        slot = bridge.tool_limiter.slot("cli-a")
        await slot.__aenter__()
        try:
            refused = await self._control(cli, "agent_yield", session_id="cli-a", run_id=run_id)
            self.assertFalse(refused["ok"], refused)
            self.assertIn("in flight", refused["error"])
        finally:
            await slot.__aexit__(None, None, None)
        done = await self._control(cli, "agent_yield", session_id="cli-a", run_id=run_id, summary="back to you")
        self.assertTrue(done["ok"], done)
        self.assertEqual(done["run"]["state"], "yielded")

    async def test_heartbeat_and_complete_return_run_and_control(self):
        cli = await self._register("cli", "cli-a")
        started = await self._control(cli, "agent_start", session_id="cli-a", goal="solo", workspace_root=self._root("proj"))
        run_id = started["run"]["run_id"]
        beat = await self._control(cli, "agent_heartbeat", session_id="cli-a", run_id=run_id, progress="halfway")
        self.assertEqual(beat["run"]["progress"], "halfway")
        self.assertEqual(beat["control"]["version"], 2)
        done = await self._control(cli, "agent_complete", session_id="cli-a", run_id=run_id, status="completed", summary="ok")
        self.assertEqual(done["run"]["state"], "completed")

    async def test_agent_control_broadcast_reaches_cli_and_browser_clients(self):
        root = self._root("proj")
        cli = await self._register("cli", "cli-a")
        watcher = await self._register("cli", "cli-w")
        browser = await self._register("browser", "browser-main")
        await self._control(cli, "agent_arm", session_id="cli-a", workspace_root=root)
        for ws in (cli, watcher, browser):
            while True:
                pushed = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if pushed.get("type") == "agent_control" and any(
                        s["session_id"] == "cli-a" and s["armed"] for s in pushed["control"]["sessions"]):
                    break


if __name__ == "__main__":
    unittest.main()
