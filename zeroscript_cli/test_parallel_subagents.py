# SPDX-License-Identifier: GPL-3.0-or-later
"""Concurrent zeroscript_run_subagent execution: overlap, ordering, limits, isolation."""
import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zeroscript_cli import agent as agent_module
from zeroscript_cli.agent import IndependentAgent, _tool_aliases
from zeroscript_cli.remaster_services import AgentFlightRecorder

SUBAGENT_TOOL = {
    "name": "zeroscript_run_subagent", "server": "zeroscript-runtime", "description": "Delegate",
    "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "task": {"type": "string"}}},
}
READ_TOOL = {"name": "workspace_read_file", "server": "workspace", "description": "Read a file",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}}


class FakeBridge:
    url = "ws://fake"
    session_id = "sess"

    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return [dict(READ_TOOL)]

    async def control(self, *args, **kwargs):
        # Protocol 2 snapshot: the run is active and targeted at this bridge session.
        return {"control": {"runs": [{"run_id": "run-1", "state": "active", "target_session": self.session_id}]}}

    async def call_tool(self, name, arguments, session_id=None, workspace_root=None):
        self.calls.append((name, dict(arguments), session_id))
        await asyncio.sleep(0)
        return {"ok": True, "text": "contents of " + str(arguments.get("path"))}


class LegacyBridge(FakeBridge):
    async def call_tool(self, name, arguments):  # no session_id support (before the bridge change)
        self.calls.append((name, dict(arguments), None))
        return {"ok": True, "text": "legacy"}


class FakeProvider:
    """Each subagent: one tool call, then a final answer. Delay is looked up from the task text."""
    supports_vision = False

    def __init__(self, delays=None):
        self.model = "main-model"
        self.delays = delays or {}
        self.in_flight = 0
        self.max_in_flight = 0
        self.models_seen = []
        self.model_during = []

    async def complete(self, messages, tools, on_text=None, model=None):
        task = messages[1]["content"].split("DELEGATED TASK\n", 1)[1]
        self.models_seen.append(model)
        self.model_during.append(self.model)
        first = not any(m.get("role") == "tool" for m in messages)
        if first:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(self.delays.get(task, 0.05))
            alias = next(iter(tool["function"]["name"] for tool in tools))
            return {"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c-" + task, "type": "function",
                 "function": {"name": alias, "arguments": json.dumps({"path": task + ".txt"})}}]}}]}
        self.in_flight -= 1
        return {"choices": [{"message": {"content": "answer:" + task}}]}


def make_agent(tmp, bridge=None, provider=None, agent_model=""):
    run_record = {"run_id": "run-1", "session_id": "ui-session", "workspace": "", "goal": "g"}
    with mock.patch.object(agent_module, "_session_dir", return_value=Path(tmp)):
        agent = IndependentAgent(bridge or FakeBridge(), provider or FakeProvider(), run_record, all_tools=True)
    agent.recorder = AgentFlightRecorder(Path(tmp) / "events.jsonl")
    agent.skill_catalog = SimpleNamespace(
        find_agent=lambda name: SimpleNamespace(
            name=name, allowed_tools=(), model=agent_model, max_turns=4, disable_model_invocation=False,
            render=lambda: "profile " + name),
        model_tool=lambda: None,
        agent_tool=lambda: None,
    )
    agent._build_system_prompt = lambda *a, **k: "system"
    agent.tool_defs, agent.aliases = _tool_aliases([SUBAGENT_TOOL, READ_TOOL])
    agent.messages = []
    return agent


def sub_call(call_id, name, task):
    return {"id": call_id, "type": "function", "function": {
        "name": "zeroscript_run_subagent", "arguments": json.dumps({"name": name, "task": task})}}


class ParallelSubagentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        patches = [
            mock.patch.object(agent_module, "_session_dir", return_value=Path(self.tmp)),
            mock.patch.object(agent_module, "_write_session", lambda run_id, data: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._tmp.cleanup)
        os.environ.pop("ZS_MAX_PARALLEL_SUBAGENTS", None)

    async def run_calls(self, agent, calls):
        stopped = await agent._run_tool_calls(1, calls, {})
        return stopped

    async def test_three_subagents_overlap_and_results_keep_call_order(self):
        delays = {"slow": 0.30, "mid": 0.15, "fast": 0.02}
        provider = FakeProvider(delays)
        agent = make_agent(self.tmp, provider=provider)
        calls = [sub_call("A", "explorer", "slow"), sub_call("B", "explorer", "mid"), sub_call("C", "reviewer", "fast")]
        started = time.monotonic()
        self.assertTrue(await self.run_calls(agent, calls))
        elapsed = time.monotonic() - started
        self.assertEqual(provider.max_in_flight, 3)
        self.assertLess(elapsed, 0.30 + 0.15 + 0.02)  # serial execution would take the sum
        self.assertEqual([m["tool_call_id"] for m in agent.messages], ["A", "B", "C"])
        self.assertIn("answer:slow", agent.messages[0]["content"])
        self.assertIn("answer:fast", agent.messages[2]["content"])

    async def test_limit_two_is_respected(self):
        provider = FakeProvider({"t1": 0.08, "t2": 0.08, "t3": 0.08, "t4": 0.08})
        agent = make_agent(self.tmp, provider=provider)
        agent.max_parallel_subagents = 2
        calls = [sub_call(str(i), "explorer", "t" + str(i)) for i in range(1, 5)]
        await self.run_calls(agent, calls)
        self.assertEqual(provider.max_in_flight, 2)
        self.assertEqual([m["tool_call_id"] for m in agent.messages], ["1", "2", "3", "4"])

    async def test_zero_means_unlimited_and_env_is_honoured(self):
        provider = FakeProvider({"t" + str(i): 0.08 for i in range(6)})
        agent = make_agent(self.tmp, provider=provider)
        agent.max_parallel_subagents = 0
        self.assertEqual(agent._parallel_subagent_limit(), 0)
        await self.run_calls(agent, [sub_call(str(i), "explorer", "t" + str(i)) for i in range(6)])
        self.assertEqual(provider.max_in_flight, 6)
        agent.max_parallel_subagents = None
        self.assertEqual(agent._parallel_subagent_limit(), 4)
        with mock.patch.dict(os.environ, {"ZS_MAX_PARALLEL_SUBAGENTS": "3"}):
            self.assertEqual(agent._parallel_subagent_limit(), 3)
        with mock.patch.dict(os.environ, {"ZS_MAX_PARALLEL_SUBAGENTS": "garbage"}):
            self.assertEqual(agent._parallel_subagent_limit(), 4)
        env_provider = FakeProvider({"t" + str(i): 0.08 for i in range(4)})
        env_agent = make_agent(self.tmp, provider=env_provider)
        with mock.patch.dict(os.environ, {"ZS_MAX_PARALLEL_SUBAGENTS": "1"}):
            await self.run_calls(env_agent, [sub_call(str(i), "explorer", "t" + str(i)) for i in range(4)])
        self.assertEqual(env_provider.max_in_flight, 1)

    async def test_subagent_bridge_sessions_are_distinct(self):
        bridge = FakeBridge()
        agent = make_agent(self.tmp, bridge=bridge)
        await self.run_calls(agent, [sub_call("A", "explorer", "one"), sub_call("B", "explorer", "two"),
                                     sub_call("C", "reviewer", "three")])
        sessions = [session for _name, _args, session in bridge.calls]
        self.assertEqual(sorted(sessions), ["sess:sub-1", "sess:sub-2", "sess:sub-3"])
        by_path = {args["path"]: session for _n, args, session in bridge.calls}
        self.assertEqual(by_path, {"one.txt": "sess:sub-1", "two.txt": "sess:sub-2", "three.txt": "sess:sub-3"})
        # A later single subagent keeps counting up.
        await self.run_calls(agent, [sub_call("D", "explorer", "four")])
        self.assertEqual(bridge.calls[-1][2], "sess:sub-4")

    async def test_bridge_without_session_id_support_still_works(self):
        bridge = LegacyBridge()
        agent = make_agent(self.tmp, bridge=bridge)
        await self.run_calls(agent, [sub_call("A", "explorer", "one"), sub_call("B", "explorer", "two")])
        self.assertEqual(len(bridge.calls), 2)
        self.assertIn("answer:one", agent.messages[0]["content"])

    async def test_main_agent_state_is_untouched(self):
        provider = FakeProvider()
        agent = make_agent(self.tmp, provider=provider, agent_model="sub-model")
        aliases, tool_defs = agent.aliases, agent.tool_defs
        alias_keys = set(aliases)
        await self.run_calls(agent, [sub_call("A", "explorer", "one"), sub_call("B", "explorer", "two")])
        self.assertIs(agent.aliases, aliases)
        self.assertIs(agent.tool_defs, tool_defs)
        self.assertEqual(set(agent.aliases), alias_keys)
        self.assertEqual(provider.model, "main-model")
        # The model override reached the provider per call and was never written to provider.model.
        self.assertTrue(provider.models_seen and all(model == "sub-model" for model in provider.models_seen))
        self.assertTrue(all(model == "main-model" for model in provider.model_during))
        self.assertEqual(agent._last_tool_images, [])

    async def test_mixed_turn_keeps_order_and_runs_other_calls_first(self):
        bridge = FakeBridge()
        agent = make_agent(self.tmp, bridge=bridge, provider=FakeProvider({"slow": 0.1, "fast": 0.01}))
        read = {"id": "R", "type": "function", "function": {
            "name": "workspace_read_file", "arguments": json.dumps({"path": "main.txt"})}}
        calls = [sub_call("A", "explorer", "slow"), read, sub_call("B", "explorer", "fast")]
        await self.run_calls(agent, calls)
        self.assertEqual([m["tool_call_id"] for m in agent.messages], ["A", "R", "B"])
        self.assertEqual(bridge.calls[0][1]["path"], "main.txt")  # sequential call ran before the subagent batch
        self.assertIsNone(bridge.calls[0][2])  # main agent keeps the bridge's default session

    async def test_single_subagent_runs_in_place_with_normal_output(self):
        agent = make_agent(self.tmp)
        lines = []
        with mock.patch.object(agent_module.UI, "info", side_effect=lines.append):
            await self.run_calls(agent, [sub_call("A", "explorer", "solo")])
        self.assertEqual(agent.messages[0]["tool_call_id"], "A")
        self.assertTrue(any(line.startswith("Subagent explorer started") for line in lines))

    async def test_parallel_output_is_prefixed_and_uses_one_spinner(self):
        agent = make_agent(self.tmp)
        lines = []
        spinners = []

        def fake_status(message):
            spinners.append(message)
            from contextlib import nullcontext
            return nullcontext()

        with mock.patch.object(agent_module.UI, "info", side_effect=lines.append), \
                mock.patch.object(agent_module.UI, "status", side_effect=fake_status), \
                mock.patch.object(agent_module.UI, "tool_start") as tool_start, \
                mock.patch.object(agent_module.UI, "tool_result") as tool_result:
            await self.run_calls(agent, [sub_call("A", "explorer", "one"), sub_call("B", "reviewer", "two")])
        self.assertEqual(len(spinners), 1)
        tool_start.assert_not_called()
        tool_result.assert_not_called()
        prefixed = [line for line in lines if line.startswith("[explorer#1]") or line.startswith("[reviewer#2]")]
        self.assertTrue(any("started" in line for line in prefixed))
        self.assertTrue(any("workspace_read_file" in line for line in prefixed))
        self.assertTrue(all("\n" not in line for line in prefixed))

    async def test_failed_subagent_does_not_break_the_batch(self):
        agent = make_agent(self.tmp)
        agent.skill_catalog.find_agent = lambda name: None if name == "missing" else SimpleNamespace(
            name=name, allowed_tools=(), model="", max_turns=4, disable_model_invocation=False, render=lambda: "p")
        await self.run_calls(agent, [sub_call("A", "missing", "x"), sub_call("B", "explorer", "ok")])
        self.assertEqual([m["tool_call_id"] for m in agent.messages], ["A", "B"])
        self.assertIn("Subagent failed", agent.messages[0]["content"])
        self.assertIn("answer:ok", agent.messages[1]["content"])


class RecorderThreadSafetyTests(unittest.TestCase):
    def test_concurrent_writes_stay_intact_across_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = AgentFlightRecorder(Path(tmp) / "events.jsonl", max_bytes=65536)
            threads_count, per_thread = 8, 400
            payload = "x" * 200

            def work(worker):
                for item in range(per_thread):
                    recorder.record("evt", worker=worker, item=item, payload=payload)

            threads = [threading.Thread(target=work, args=(n,)) for n in range(threads_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            rows = 0
            for path in (recorder.path, recorder.path.with_suffix(recorder.path.suffix + ".1")):
                if path.exists():
                    for line in path.read_text(encoding="utf-8").splitlines():
                        json.loads(line)  # every line is a complete JSON record
                        rows += 1
            self.assertGreater(rows, 0)
            self.assertTrue(recorder.path.with_suffix(recorder.path.suffix + ".1").exists())
            self.assertLessEqual(rows, threads_count * per_thread)


if __name__ == "__main__":
    unittest.main()
