# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import inspect
import unittest

from zeroscript_cli.ui import TerminalUI, _PROMPT_TOOLKIT
from zeroscript_cli.agent import (
    OpenAICompatibleProvider,
    _select_tools_for_task,
    _tool_aliases,
    fit_messages_to_context,
    _estimated_tokens,
    _parse_vision_option,
)


class CLIAgentUnitTests(unittest.TestCase):
    def test_terminal_prompt_uses_async_prompt_toolkit_api(self):
        class FakePromptSession:
            async def prompt_async(self, *args, **kwargs):
                return "  keep building  "

            def prompt(self, *args, **kwargs):
                raise AssertionError("Synchronous prompt must not be called inside asyncio")

        ui = TerminalUI()
        ui._session = FakePromptSession()
        self.assertEqual(asyncio.run(ui.prompt()), "keep building")

    def test_terminal_ui_prompt_session_initializes(self):
        ui = TerminalUI()
        if _PROMPT_TOOLKIT:
            self.assertIsNotNone(ui._session, "prompt-toolkit should initialize with supported key bindings")

    def test_task_tool_selection_scopes_unrelated_integrations(self):
        catalogue = [
            {"name": "workspace_info", "server": "workspace", "description": "Read workspace configuration",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "workspace_read_file", "server": "workspace", "description": "Read a project file",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "roblox/get_studio_state", "server": "roblox", "description": "Inspect Roblox Studio",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "roblox/execute_luau", "server": "roblox", "description": "Run Luau in Roblox Studio",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "blender/render_scene", "server": "blender", "description": "Render a Blender scene",
             "inputSchema": {"type": "object", "properties": {}}},
        ]
        selected = _select_tools_for_task(catalogue, "Inspect Roblox Studio and change a script")
        names = {tool["name"] for tool in selected}
        self.assertIn("workspace_info", names)
        self.assertIn("roblox/get_studio_state", names)
        self.assertIn("roblox/execute_luau", names)
        self.assertNotIn("blender/render_scene", names)

    def test_task_tool_selection_recognizes_mcp_server_aliases(self):
        catalogue = [
            {"name": "workspace_info", "server": "workspace", "description": "Read workspace configuration",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "playwright/click", "server": "playwright-mcp", "description": "Click a browser element",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "bpy/render_scene", "server": "bpy", "description": "Render a Blender scene",
             "inputSchema": {"type": "object", "properties": {}}},
        ]
        browser_tools = _select_tools_for_task(catalogue, "run a browser screenshot test")
        browser_names = {tool["name"] for tool in browser_tools}
        self.assertIn("playwright/click", browser_names)
        self.assertNotIn("bpy/render_scene", browser_names)

        blender_tools = _select_tools_for_task(catalogue, "render a Blender scene")
        blender_names = {tool["name"] for tool in blender_tools}
        self.assertIn("bpy/render_scene", blender_names)
        self.assertNotIn("playwright/click", blender_names)

    def test_all_tools_override_retains_all_non_internal_tools(self):
        catalogue = [
            {"name": "workspace_info", "server": "workspace", "inputSchema": {}},
            {"name": "blender/render_scene", "server": "blender", "inputSchema": {}},
            {"name": "zeroscript_handoff_to_cli", "server": "zeroscript-control", "inputSchema": {}},
        ]
        selected = _select_tools_for_task(catalogue, "inspect a Python file", include_all=True)
        self.assertEqual({tool["name"] for tool in selected}, {"workspace_info", "blender/render_scene"})

    def test_tool_aliases_map_namespaced_tools_to_valid_function_names(self):
        catalogue = [
            {"name": "workspace_read_file", "server": "workspace", "description": "Read a file",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "roblox/execute_luau", "server": "roblox", "description": "Execute Luau",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
            {"name": "zeroscript_handoff_to_cli", "server": "zeroscript-control", "description": "Internal handoff",
             "inputSchema": {"type": "object", "properties": {}}},
        ]
        tools, aliases = _tool_aliases(catalogue)
        self.assertEqual(len(tools), 2)
        self.assertIn("workspace_read_file", aliases)
        self.assertIn("roblox__execute_luau", aliases)
        self.assertTrue(all("/" not in item["function"]["name"] for item in tools))
        self.assertNotIn("zeroscript_handoff_to_cli", aliases)

    def test_runtime_skill_loader_is_available_to_model_tool_loop(self):
        catalogue = [
            {"name": "workspace_info", "server": "workspace", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "zeroscript_load_skill", "server": "zeroscript-runtime", "description": "Load relevant skill instructions",
             "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
            {"name": "zeroscript_handoff_to_cli", "server": "zeroscript-control", "inputSchema": {"type": "object", "properties": {}}},
        ]
        selected = _select_tools_for_task(catalogue, "fix a Python module")
        self.assertIn("zeroscript_load_skill", {item["name"] for item in selected})
        api_tools, aliases = _tool_aliases(selected)
        self.assertIn("zeroscript_load_skill", aliases)
        self.assertTrue(any(item["function"]["name"] == "zeroscript_load_skill" for item in api_tools))
        self.assertNotIn("zeroscript_handoff_to_cli", aliases)
    def test_runtime_subagent_tool_is_available_when_agent_profiles_exist(self):
        catalogue = [
            {"name": "workspace_info", "server": "workspace", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "zeroscript_run_subagent", "server": "zeroscript-runtime", "description": "Delegate to a local agent profile",
             "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "task": {"type": "string"}}, "required": ["name", "task"]}},
            {"name": "zeroscript_handoff_to_cli", "server": "zeroscript-control", "inputSchema": {"type": "object", "properties": {}}},
        ]
        selected = _select_tools_for_task(catalogue, "investigate a Python bug")
        self.assertIn("zeroscript_run_subagent", {item["name"] for item in selected})
        api_tools, aliases = _tool_aliases(selected)
        self.assertIn("zeroscript_run_subagent", aliases)
        self.assertTrue(any(item["function"]["name"] == "zeroscript_run_subagent" for item in api_tools))
        self.assertNotIn("zeroscript_handoff_to_cli", aliases)
    def test_missing_tool_schemas_normalize_to_empty_arguments(self):
        catalogue = [
            {"name": "workspace_unknown", "server": "workspace", "description": "Undeclared schema"},
        ]
        api_tools, aliases = _tool_aliases(catalogue)
        alias = api_tools[0]["function"]["name"]
        normalized = aliases[alias]
        self.assertFalse(normalized["inputSchema"]["additionalProperties"])
        self.assertEqual(normalized["inputSchema"]["type"], "object")
        self.assertEqual(normalized["inputSchema"]["properties"], {})

    def test_context_budget_keeps_system_and_recent_complete_turn(self):
        messages = [
            {"role": "system", "content": "System rules"},
            {"role": "user", "content": "old question " + ("x" * 700)},
            {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "read", "arguments": "{}"}}], "content": None},
            {"role": "tool", "tool_call_id": "call-1", "content": "old result " + ("y" * 700)},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "latest question"},
            {"role": "assistant", "content": "latest answer"},
        ]
        fitted = fit_messages_to_context(messages, [], 480)
        self.assertEqual(fitted[0]["role"], "system")
        self.assertTrue(any(m.get("content") == "latest question" for m in fitted))
        self.assertFalse(any(isinstance(m.get("content"), str) and m["content"].startswith("old question") for m in fitted))
        # Tool call and its result are removed together as part of the old turn.
        self.assertFalse(any(m.get("tool_call_id") == "call-1" for m in fitted))

    def test_context_budget_fails_clearly_when_latest_turn_cannot_fit(self):
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "x" * 6000}]
        with self.assertRaisesRegex(RuntimeError, "newest conversation turn exceeds"):
            fit_messages_to_context(messages, [], 600)

    def test_context_estimator_does_not_count_base64_as_text_tokens(self):
        payload = [{"role": "user", "content": [
            {"type": "text", "text": "Inspect this screenshot"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + ("A" * 1000000)}},
        ]}]
        self.assertLess(_estimated_tokens(payload), 2000)

    def test_vision_profile_option_accepts_documented_forms(self):
        self.assertTrue(_parse_vision_option("true"))
        self.assertTrue(_parse_vision_option("vision:true"))
        self.assertFalse(_parse_vision_option("false"))
        self.assertFalse(_parse_vision_option("vision:false"))
        with self.assertRaisesRegex(ValueError, "Vision capability"):
            _parse_vision_option("maybe")

    def test_provider_records_vision_and_context_capabilities(self):
        provider = OpenAICompatibleProvider(base_url="http://127.0.0.1:11434/v1", model="local", vision=True, context_tokens=8192)
        self.assertTrue(provider.supports_vision)
        self.assertEqual(provider.context_tokens, 8192)
        with self.assertRaisesRegex(ValueError, "at least 256"):
            OpenAICompatibleProvider(base_url="http://127.0.0.1:11434/v1", model="local", context_tokens=0)

    def test_provider_endpoint_normalization(self):
        self.assertEqual(
            OpenAICompatibleProvider(base_url="http://127.0.0.1:11434/v1", model="local")._url(),
            "http://127.0.0.1:11434/v1/chat/completions",
        )
        self.assertEqual(
            OpenAICompatibleProvider(base_url="https://example.test/v1/chat/completions", model="custom")._url(),
            "https://example.test/v1/chat/completions",
        )


if __name__ == "__main__":
    unittest.main()
