# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Discord MCP server.

No network access: the REST layer is patched at urllib.request.urlopen, and
higher-level tools are exercised against a stub _request where the exact
payload matters.
"""
from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from zeroscript_discord import mcp_server as m

FAKE_TOKEN = "super-secret-bot-token-abc123"


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, payload):
    body = io.BytesIO(json.dumps(payload).encode("utf-8"))
    return urllib.error.HTTPError("https://discord.com/api/v10/x", code, "err", {}, body)


class TokenTests(unittest.TestCase):
    def test_missing_token_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(m.DiscordError):
                m._token()

    def test_unexpanded_env_var_is_treated_as_unset(self):
        # The bridge leaves an unset $VAR as a literal "$DISCORD_BOT_TOKEN".
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "$DISCORD_BOT_TOKEN"}, clear=True):
            with self.assertRaises(m.DiscordError):
                m._token()

    def test_status_without_token_is_unconfigured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = m._status({})
        self.assertFalse(result["configured"])
        self.assertFalse(result["token_valid"])

    def test_scrub_removes_token(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            self.assertNotIn(FAKE_TOKEN, m._scrub(f"leaked {FAKE_TOKEN} here"))

    def test_token_never_appears_in_tool_error_text(self):
        leaked = {"message": f"bad auth for {FAKE_TOKEN}"}
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with mock.patch("urllib.request.urlopen", side_effect=_http_error(400, leaked)):
                response = m._reply({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "discord_status", "arguments": {}},
                })
        rendered = json.dumps(response)
        self.assertNotIn(FAKE_TOKEN, rendered)


class GuildAllowlistTests(unittest.TestCase):
    def test_allowlist_blocks_other_guilds(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN,
                                         "ZS_DISCORD_GUILD_IDS": "111,222"}, clear=True):
            with self.assertRaises(m.DiscordError):
                m._check_guild("333")

    def test_allowlist_permits_listed_guilds(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN,
                                         "ZS_DISCORD_GUILD_IDS": "111,222"}, clear=True):
            self.assertEqual(m._check_guild(111), "111")

    def test_unexpanded_allowlist_var_means_no_allowlist(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN,
                                         "ZS_DISCORD_GUILD_IDS": "$ZS_DISCORD_GUILD_IDS"}, clear=True):
            self.assertIsNone(m._allowed_guilds())
            self.assertEqual(m._check_guild("999"), "999")

    def test_mixed_placeholder_and_real_ids_keeps_real_ids(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN,
                                         "ZS_DISCORD_GUILD_IDS": "111,$ZS_DISCORD_GUILD_IDS"}, clear=True):
            self.assertEqual(m._allowed_guilds(), {"111"})

    def test_no_allowlist_permits_any_guild(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            self.assertEqual(m._check_guild("999"), "999")

    def test_list_guilds_filters_by_allowlist(self):
        guilds = [{"id": "111", "name": "A"}, {"id": "333", "name": "B"}]
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN,
                                         "ZS_DISCORD_GUILD_IDS": "111"}, clear=True):
            with mock.patch.object(m, "_request", return_value=guilds):
                result = m._list_guilds({})
        self.assertEqual([g["id"] for g in result["guilds"]], ["111"])


class ValidationTests(unittest.TestCase):
    def test_require_id_rejects_non_numeric(self):
        for bad in ("abc", "", None, True, "12x"):
            with self.assertRaises(m.DiscordError):
                m._require_id(bad, "guild_id")

    def test_require_id_accepts_int_and_str(self):
        self.assertEqual(m._require_id(123, "id"), "123")
        self.assertEqual(m._require_id("456", "id"), "456")

    def test_require_text_rejects_blank(self):
        with self.assertRaises(m.DiscordError):
            m._require_text("   ", "name", 100)

    def test_require_text_enforces_limit(self):
        with self.assertRaises(m.DiscordError):
            m._require_text("x" * 101, "name", 100)


class DestructiveOpTests(unittest.TestCase):
    def test_delete_channel_requires_confirm(self):
        with self.assertRaises(m.DiscordError):
            m._delete_channel({"channel_id": "123"})

    def test_delete_channel_with_confirm_calls_api(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with mock.patch.object(m, "_request", return_value=None) as req:
                result = m._delete_channel({"channel_id": "123", "confirm": True})
        self.assertTrue(result["deleted"])
        self.assertEqual(req.call_args.args[:2], ("DELETE", "/channels/123"))

    def test_delete_role_requires_confirm(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with self.assertRaises(m.DiscordError):
                m._delete_role({"guild_id": "1", "role_id": "2"})

    def test_everyone_role_cannot_be_deleted(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with self.assertRaises(m.DiscordError) as ctx:
                m._delete_role({"guild_id": "1", "role_id": "1", "confirm": True})
        self.assertIn("@everyone", str(ctx.exception))

    def test_everyone_role_cannot_be_edited(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with self.assertRaises(m.DiscordError) as ctx:
                m._edit_role({"guild_id": "1", "role_id": "1", "name": "x"})
        self.assertIn("@everyone", str(ctx.exception))


class ChannelOverwriteTests(unittest.TestCase):
    def test_allow_and_deny_overlap_rejected(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with self.assertRaises(m.DiscordError):
                m._set_channel_overwrite({
                    "channel_id": "1", "target_id": "2", "target_type": "role",
                    "allow": 8, "deny": 8,
                })

    def test_view_channel_deny_requires_confirm(self):
        view = m.DANGEROUS_PERMISSIONS["VIEW_CHANNEL"]
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with self.assertRaises(m.DiscordError) as ctx:
                m._set_channel_overwrite({
                    "channel_id": "1", "target_id": "2", "target_type": "role",
                    "allow": 0, "deny": view,
                })
        self.assertIn("confirm=true", str(ctx.exception))

    def test_safe_overwrite_needs_no_confirm(self):
        send = m.DANGEROUS_PERMISSIONS["SEND_MESSAGES"]
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with mock.patch.object(m, "_request", return_value=None) as req:
                result = m._set_channel_overwrite({
                    "channel_id": "1", "target_id": "2", "target_type": "role",
                    "allow": send, "deny": 0,
                })
        self.assertTrue(result["updated"])
        body = req.call_args.kwargs["body"]
        self.assertEqual(body["allow"], str(send))
        self.assertEqual(body["type"], 0)


class PayloadTests(unittest.TestCase):
    def test_create_channel_maps_type_name(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with mock.patch.object(m, "_request", return_value={"id": "9", "name": "ops"}) as req:
                m._create_channel({"guild_id": "1", "name": "ops", "type": "voice"})
        body = req.call_args.kwargs["body"]
        self.assertEqual(body["type"], 2)

    def test_create_channel_rejects_unknown_type(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with self.assertRaises(m.DiscordError):
                m._create_channel({"guild_id": "1", "name": "x", "type": "dm"})

    def test_edit_channel_requires_a_change(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with self.assertRaises(m.DiscordError):
                m._edit_channel({"channel_id": "1"})

    def test_assign_role_hits_member_roles_path(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with mock.patch.object(m, "_request", return_value=None) as req:
                result = m._assign_role({"guild_id": "1", "user_id": "2", "role_id": "3"})
        self.assertTrue(result["assigned"])
        self.assertEqual(req.call_args.args[:2],
                         ("PUT", "/guilds/1/members/2/roles/3"))


class HttpLayerTests(unittest.TestCase):
    def test_429_retries_then_succeeds(self):
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _http_error(429, {"retry_after": 0.01})
            return _FakeResponse({"id": "42", "username": "bot"})

        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with mock.patch("time.sleep"):
                    result = m._request("GET", "/users/@me")
        self.assertEqual(result["id"], "42")
        self.assertEqual(calls["n"], 2)

    def test_429_gives_up_after_max_retries(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with mock.patch("urllib.request.urlopen",
                            side_effect=_http_error(429, {"retry_after": 0.01})):
                with mock.patch("time.sleep"):
                    with self.assertRaises(m.DiscordError):
                        m._request("GET", "/users/@me")

    def test_timeout_is_retried_then_succeeds(self):
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError(TimeoutError("timed out"))
            return _FakeResponse({"id": "42"})

        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with mock.patch("time.sleep"):
                    result = m._request("GET", "/users/@me")
        self.assertEqual(result["id"], "42")
        self.assertEqual(calls["n"], 2)

    def test_budget_exhaustion_raises_before_calling(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with mock.patch.object(m, "REQUEST_BUDGET", 0.0):
                with mock.patch("urllib.request.urlopen") as urlopen:
                    with self.assertRaises(m.DiscordError) as ctx:
                        m._request("GET", "/users/@me")
        urlopen.assert_not_called()
        self.assertIn("budget", str(ctx.exception))

    def test_429_wait_beyond_budget_fails_fast(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with mock.patch.object(m, "REQUEST_BUDGET", 5.0):
                with mock.patch("urllib.request.urlopen",
                                side_effect=_http_error(429, {"retry_after": 100})):
                    with mock.patch("time.sleep") as sleep:
                        with self.assertRaises(m.DiscordError) as ctx:
                            m._request("GET", "/users/@me")
        sleep.assert_not_called()
        self.assertIn("rate limited", str(ctx.exception))

    def test_socket_timeout_is_clamped_to_budget(self):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["timeout"] = timeout
            return _FakeResponse({"id": "42"})

        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with mock.patch.object(m, "REQUEST_BUDGET", 3.0):
                with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    m._request("GET", "/users/@me")
        self.assertLessEqual(seen["timeout"], 3.0)

    def test_401_message_is_actionable(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with mock.patch("urllib.request.urlopen",
                            side_effect=_http_error(401, {"message": "401: Unauthorized"})):
                with self.assertRaises(m.DiscordError) as ctx:
                    m._request("GET", "/users/@me")
        self.assertIn("DISCORD_BOT_TOKEN", str(ctx.exception))

    def test_403_mentions_role_hierarchy(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with mock.patch("urllib.request.urlopen",
                            side_effect=_http_error(403, {"message": "Missing Permissions"})):
                with self.assertRaises(m.DiscordError) as ctx:
                    m._request("GET", "/guilds/1/roles")
        self.assertIn("top role", str(ctx.exception))

    def test_empty_body_returns_none(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}, clear=True):
            with mock.patch("urllib.request.urlopen",
                            return_value=_FakeResponse(None, status=204)):
                self.assertIsNone(m._request("DELETE", "/channels/1", expect=(200, 204)))


class ProtocolTests(unittest.TestCase):
    def test_every_handler_is_exposed_as_a_tool(self):
        names = {tool["name"] for tool in m.TOOLS}
        self.assertEqual(names, set(m.HANDLERS))

    def test_initialize_advertises_tools(self):
        response = m._reply({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(response["result"]["protocolVersion"], m.PROTOCOL_VERSION)
        self.assertIn("tools", response["result"]["capabilities"])

    def test_tools_list_returns_schemas(self):
        response = m._reply({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = response["result"]["tools"]
        self.assertGreater(len(tools), 10)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_unknown_tool_errors(self):
        response = m._reply({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(response["error"]["code"], -32602)

    def test_handler_error_becomes_iserror_result(self):
        response = m._reply({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                             "params": {"name": "discord_delete_channel",
                                        "arguments": {"channel_id": "1"}}})
        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertIn("confirm", payload["error"])

    def test_notifications_produce_no_response(self):
        self.assertIsNone(m._reply({"jsonrpc": "2.0", "method": "notifications/initialized"}))


if __name__ == "__main__":
    unittest.main()
