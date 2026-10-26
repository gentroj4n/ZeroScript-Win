# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from bridge import MCPManager
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
        self.assertEqual(result["content"], "print('ok')\n")

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
        jpeg_path.write_bytes(b"\\xff\\xd8\\xff\\xe0" + b"test-payload")
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


if __name__ == "__main__":
    unittest.main()
