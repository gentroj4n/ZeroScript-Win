# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from workspace_agent.roblox_export import (
    _build_execute_args,
    _decode_payload,
    _safe_segment,
    _script_extension,
)


class RobloxExportTests(unittest.TestCase):
    def test_safe_segment_replaces_windows_path_characters(self):
        self.assertEqual(_safe_segment('Map:Main/Bad\\Name?'), "Map_Main_Bad_Name_")

    def test_script_extensions_preserve_runtime_role(self):
        self.assertEqual(_script_extension("Script"), ".server.lua")
        self.assertEqual(_script_extension("LocalScript"), ".client.lua")
        self.assertEqual(_script_extension("ModuleScript"), ".module.lua")

    def test_decode_payload_accepts_json_text_and_embedded_json(self):
        expected = {"total": 2, "files": []}
        self.assertEqual(_decode_payload('{"total": 2, "files": []}'), expected)
        self.assertEqual(_decode_payload('Returned value: {"total": 2, "files": []}'), expected)

    def test_build_execute_args_supports_native_studio_schema(self):
        tool = {
            "inputSchema": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "datamodel_type": {"type": "string", "enum": ["Edit", "Client", "Server"]},
                },
                "required": ["code", "datamodel_type"],
            }
        }
        args = _build_execute_args(tool, "return 42")
        self.assertEqual(args, {"code": "return 42", "datamodel_type": "Edit"})


if __name__ == "__main__":
    unittest.main()
