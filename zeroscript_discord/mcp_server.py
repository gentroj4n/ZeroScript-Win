# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal stdio MCP server exposing Discord server management to ZeroScript-Win.

Talks to the Discord REST API v10 directly over stdlib urllib, so no extra
pip dependency is required. Credentials come from the environment only:

    DISCORD_BOT_TOKEN     required; never accepted as a tool argument
    ZS_DISCORD_GUILD_IDS  optional comma-separated allowlist of guild IDs

The bridge supervises this process separately, so a Discord outage or a bad
response cannot take down the main WebSocket bridge.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "zeroscript-discord"
SERVER_VERSION = "0.1.0"

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/zeroscript, 0.1.0)"
REQUEST_TIMEOUT = 20.0
MAX_RETRIES = 3
# Hard wall-clock budget for one logical API call, including every retry and
# backoff sleep. Must stay well under the MCP client's 120s tool timeout so a
# slow or rate-limited call returns a clean error instead of hanging the client
# until it gives up.
REQUEST_BUDGET = 60.0
MAX_RESULTS = 200
MAX_NAME_CHARS = 100
MAX_TOPIC_CHARS = 1024
MAX_REASON_CHARS = 512

# Channel types we are willing to create.
CREATABLE_CHANNEL_TYPES = {
    "text": 0,
    "voice": 2,
    "category": 4,
    "announcement": 5,
    "stage": 13,
    "forum": 15,
}

# Permission bits that can lock the bot (or everyone) out of a channel. Setting
# these without confirm=true is refused because it is hard to undo blind.
DANGEROUS_PERMISSIONS = {
    "VIEW_CHANNEL": 1 << 10,
    "SEND_MESSAGES": 1 << 11,
    "MANAGE_CHANNELS": 1 << 4,
    "MANAGE_ROLES": 1 << 28,
}


class DiscordError(RuntimeError):
    """Raised for configuration problems or refused operations."""


# ── configuration ──────────────────────────────────────────────────────────
def _token() -> str:
    token = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    # The bridge expands $VAR references in config.json with os.path.expandvars,
    # which leaves an UNSET reference as the literal string "$DISCORD_BOT_TOKEN".
    # Treat that (and any other unexpanded $VAR) as unset rather than sending a
    # nonsense token and surfacing a confusing 401.
    if not token or token.startswith("$"):
        raise DiscordError(
            "DISCORD_BOT_TOKEN is not set. Put it in the env block of the "
            "discord entry in config.json (or your shell environment) and "
            "restart the bridge. Never pass tokens as tool arguments."
        )
    return token


def _allowed_guilds() -> set[str] | None:
    raw = (os.environ.get("ZS_DISCORD_GUILD_IDS") or "").strip()
    if not raw:
        return None
    # The bridge passes an unset $VAR through literally. Drop those placeholders
    # so an unconfigured allowlist means "no allowlist", not "match nothing".
    entries = {
        part.strip() for part in raw.split(",")
        if part.strip() and not part.strip().startswith("$")
    }
    return entries or None


def _check_guild(guild_id: Any) -> str:
    gid = _require_id(guild_id, "guild_id")
    allowed = _allowed_guilds()
    if allowed is not None and gid not in allowed:
        raise DiscordError(
            f"guild {gid} is not in ZS_DISCORD_GUILD_IDS; refusing to touch it."
        )
    return gid


# ── validation helpers ─────────────────────────────────────────────────────
def _require_id(value: Any, name: str) -> str:
    if value is None or isinstance(value, bool):
        raise DiscordError(f"{name} is required and must be a Discord snowflake id")
    text = str(value).strip()
    if not text.isdigit():
        raise DiscordError(f"{name} must be a numeric Discord snowflake id, got {value!r}")
    return text


def _optional_text(value: Any, name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DiscordError(f"{name} must be a string")
    if len(value) > limit:
        raise DiscordError(f"{name} exceeds the {limit}-character limit")
    return value


def _require_text(value: Any, name: str, limit: int) -> str:
    text = _optional_text(value, name, limit)
    if text is None or not text.strip():
        raise DiscordError(f"{name} must be a non-empty string")
    return text


def _require_confirm(args: dict[str, Any], action: str) -> None:
    if args.get("confirm") is not True:
        raise DiscordError(
            f"{action} is destructive. Re-call with confirm=true to proceed."
        )


# ── HTTP layer ─────────────────────────────────────────────────────────────
def _scrub(text: str) -> str:
    """Ensure the bot token can never leak into tool output or logs."""
    token = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    if token and token in text:
        text = text.replace(token, "<redacted-bot-token>")
    return text


def _request(method: str, path: str, *, body: dict[str, Any] | None = None,
             query: dict[str, Any] | None = None, reason: str | None = None,
             expect: tuple[int, ...] = (200,)) -> Any:
    url = API_BASE + path
    if query:
        cleaned = {k: v for k, v in query.items() if v is not None}
        if cleaned:
            url += "?" + urllib.parse.urlencode(cleaned)

    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")

    headers = {
        "Authorization": f"Bot {_token()}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if reason:
        headers["X-Audit-Log-Reason"] = urllib.parse.quote(reason[:MAX_REASON_CHARS])

    deadline = time.monotonic() + REQUEST_BUDGET

    def remaining() -> float:
        return deadline - time.monotonic()

    attempt = 0
    while True:
        attempt += 1
        left = remaining()
        if left <= 0:
            raise DiscordError(
                _scrub(f"{method} {path} exceeded the {REQUEST_BUDGET:.0f}s budget "
                       "before completing (Discord may be rate limiting)")
            )
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=min(REQUEST_TIMEOUT, left)) as response:
                raw = response.read().decode("utf-8", "replace")
                if response.status not in expect:
                    raise DiscordError(
                        _scrub(f"unexpected status {response.status} from {method} {path}")
                    )
                if not raw.strip():
                    return None
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {"raw": _scrub(raw[:2000])}
        except urllib.error.HTTPError as exc:
            detail_raw = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail_raw)
            except json.JSONDecodeError:
                detail = {"message": _scrub(detail_raw[:500])}
            if exc.code == 429 and attempt <= MAX_RETRIES:
                delay = 1.0
                if isinstance(detail, dict):
                    try:
                        delay = float(detail.get("retry_after", 1.0))
                    except (TypeError, ValueError):
                        delay = 1.0
                delay = min(max(delay, 0.25), 10.0)
                left = remaining()
                if delay >= left:
                    raise DiscordError(
                        _scrub(f"{method} {path} was rate limited (429); retry_after "
                               f"{delay:.1f}s exceeds the remaining budget. Try again "
                               "shortly.")
                    ) from None
                time.sleep(delay)
                continue
            if exc.code == 401:
                raise DiscordError(
                    "Discord rejected the bot token (401). Check DISCORD_BOT_TOKEN."
                ) from None
            if exc.code == 403:
                raise DiscordError(
                    "Discord refused the request (403). The bot is missing a "
                    "permission, or the target role sits above the bot's top role."
                ) from None
            message = detail.get("message") if isinstance(detail, dict) else None
            raise DiscordError(
                _scrub(f"{method} {path} failed ({exc.code}): {message or 'unknown error'}")
            ) from None
        except urllib.error.URLError as exc:
            # Timeouts and transient connection resets are worth another attempt,
            # but only while the wall-clock budget allows it.
            if attempt <= MAX_RETRIES and remaining() > 1.0:
                time.sleep(min(1.0, max(0.0, remaining() - 0.5)))
                continue
            raise DiscordError(_scrub(f"could not reach Discord: {exc.reason}")) from None


# ── serializers (keep payloads small and stable) ───────────────────────────
def _channel_summary(channel: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": channel.get("id"),
        "name": channel.get("name"),
        "type": channel.get("type"),
        "parent_id": channel.get("parent_id"),
        "position": channel.get("position"),
        "topic": channel.get("topic"),
        "nsfw": channel.get("nsfw"),
    }


def _role_summary(role: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": role.get("id"),
        "name": role.get("name"),
        "color": role.get("color"),
        "position": role.get("position"),
        "hoist": role.get("hoist"),
        "mentionable": role.get("mentionable"),
        "managed": role.get("managed"),
        "permissions": role.get("permissions"),
    }


# ── tools ──────────────────────────────────────────────────────────────────
def _status(_args: dict[str, Any]) -> dict[str, Any]:
    configured = bool((os.environ.get("DISCORD_BOT_TOKEN") or "").strip())
    allowed = _allowed_guilds()
    result: dict[str, Any] = {
        "configured": configured,
        "server": SERVER_NAME,
        "version": SERVER_VERSION,
        "api_base": API_BASE,
        "guild_allowlist": sorted(allowed) if allowed else None,
        "allowlist_enforced": allowed is not None,
    }
    if configured:
        try:
            me = _request("GET", "/users/@me")
            result["bot"] = {
                "id": me.get("id"),
                "username": me.get("username"),
                "bot": me.get("bot", True),
            }
            result["token_valid"] = True
        except DiscordError as exc:
            result["token_valid"] = False
            result["error"] = str(exc)
    else:
        result["token_valid"] = False
    return result


def _list_guilds(_args: dict[str, Any]) -> dict[str, Any]:
    guilds = _request("GET", "/users/@me/guilds")
    allowed = _allowed_guilds()
    items = [
        {"id": g.get("id"), "name": g.get("name"), "owner": g.get("owner")}
        for g in guilds
        if allowed is None or str(g.get("id")) in allowed
    ]
    return {"guilds": items, "count": len(items)}


def _get_guild(args: dict[str, Any]) -> dict[str, Any]:
    gid = _check_guild(args.get("guild_id"))
    guild = _request("GET", f"/guilds/{gid}", query={"with_counts": "true"})
    return {
        "id": guild.get("id"),
        "name": guild.get("name"),
        "description": guild.get("description"),
        "owner_id": guild.get("owner_id"),
        "member_count": guild.get("approximate_member_count"),
        "presence_count": guild.get("approximate_presence_count"),
        "roles": [_role_summary(r) for r in sorted(
            guild.get("roles") or [], key=lambda r: r.get("position", 0), reverse=True)],
    }


def _list_members(args: dict[str, Any]) -> dict[str, Any]:
    gid = _check_guild(args.get("guild_id"))
    limit = max(1, min(int(args.get("limit", 50)), MAX_RESULTS))
    members = _request("GET", f"/guilds/{gid}/members", query={"limit": limit})
    items = [
        {
            "user_id": m.get("user", {}).get("id"),
            "username": m.get("user", {}).get("username"),
            "nick": m.get("nick"),
            "roles": m.get("roles", []),
            "joined_at": m.get("joined_at"),
        }
        for m in members
    ]
    return {"members": items, "count": len(items), "truncated": len(items) >= limit}


# ── channels ───────────────────────────────────────────────────────────────
def _list_channels(args: dict[str, Any]) -> dict[str, Any]:
    gid = _check_guild(args.get("guild_id"))
    channels = _request("GET", f"/guilds/{gid}/channels")
    wanted = args.get("type")
    items = [_channel_summary(c) for c in channels]
    if wanted is not None:
        items = [c for c in items if c.get("type") == CREATABLE_CHANNEL_TYPES.get(wanted, wanted)]
    items.sort(key=lambda c: (c.get("position") or 0, c.get("name") or ""))
    return {"channels": items, "count": len(items)}


def _get_channel(args: dict[str, Any]) -> dict[str, Any]:
    cid = _require_id(args.get("channel_id"), "channel_id")
    channel = _request("GET", f"/channels/{cid}")
    summary = _channel_summary(channel)
    summary["guild_id"] = channel.get("guild_id")
    summary["permission_overwrites"] = channel.get("permission_overwrites")
    return summary


def _create_channel(args: dict[str, Any]) -> dict[str, Any]:
    gid = _check_guild(args.get("guild_id"))
    name = _require_text(args.get("name"), "name", MAX_NAME_CHARS)
    kind = args.get("type", "text")
    if kind not in CREATABLE_CHANNEL_TYPES:
        raise DiscordError(
            f"type must be one of {sorted(CREATABLE_CHANNEL_TYPES)}, got {kind!r}"
        )
    body: dict[str, Any] = {"name": name, "type": CREATABLE_CHANNEL_TYPES[kind]}
    for key in ("topic", "nsfw", "position"):
        if key in args and args[key] is not None:
            body[key] = args[key]
    if body.get("topic") is not None:
        _optional_text(body["topic"], "topic", MAX_TOPIC_CHARS)
    if args.get("parent_id") is not None:
        body["parent_id"] = _require_id(args["parent_id"], "parent_id")
    reason = _optional_text(args.get("reason"), "reason", MAX_REASON_CHARS)
    channel = _request("POST", f"/guilds/{gid}/channels", body=body, reason=reason)
    return _channel_summary(channel)


def _edit_channel(args: dict[str, Any]) -> dict[str, Any]:
    cid = _require_id(args.get("channel_id"), "channel_id")
    body: dict[str, Any] = {}
    if args.get("name") is not None:
        body["name"] = _require_text(args["name"], "name", MAX_NAME_CHARS)
    if args.get("topic") is not None:
        body["topic"] = _optional_text(args["topic"], "topic", MAX_TOPIC_CHARS)
    if args.get("nsfw") is not None:
        body["nsfw"] = bool(args["nsfw"])
    if args.get("position") is not None:
        body["position"] = int(args["position"])
    if args.get("parent_id") is not None:
        body["parent_id"] = _require_id(args["parent_id"], "parent_id")
    if not body:
        raise DiscordError("nothing to change: supply at least one of name/topic/nsfw/position/parent_id")
    reason = _optional_text(args.get("reason"), "reason", MAX_REASON_CHARS)
    channel = _request("PATCH", f"/channels/{cid}", body=body, reason=reason)
    return _channel_summary(channel)


def _delete_channel(args: dict[str, Any]) -> dict[str, Any]:
    cid = _require_id(args.get("channel_id"), "channel_id")
    _require_confirm(args, "deleting a channel")
    reason = _optional_text(args.get("reason"), "reason", MAX_REASON_CHARS)
    _request("DELETE", f"/channels/{cid}", expect=(200, 204), reason=reason)
    return {"deleted": True, "channel_id": cid}


def _set_channel_overwrite(args: dict[str, Any]) -> dict[str, Any]:
    cid = _require_id(args.get("channel_id"), "channel_id")
    target = _require_id(args.get("target_id"), "target_id")
    target_type = args.get("target_type")
    if target_type not in ("role", "member"):
        raise DiscordError("target_type must be 'role' or 'member'")

    allow = int(args.get("allow", 0) or 0)
    deny = int(args.get("deny", 0) or 0)
    if allow & deny:
        raise DiscordError("allow and deny share bits; a bit cannot be both")

    locks_out = (deny | (int(args.get("clear", 0) or 0))) & (
        DANGEROUS_PERMISSIONS["VIEW_CHANNEL"] | DANGEROUS_PERMISSIONS["SEND_MESSAGES"]
    )
    if locks_out and args.get("confirm") is not True:
        raise DiscordError(
            "this overwrite denies VIEW_CHANNEL or SEND_MESSAGES, which can hide "
            "the channel from the bot or everyone. Re-call with confirm=true."
        )

    body = {"allow": str(allow), "deny": str(deny), "type": 0 if target_type == "role" else 1}
    reason = _optional_text(args.get("reason"), "reason", MAX_REASON_CHARS)
    _request("PUT", f"/channels/{cid}/permissions/{target}", body=body, reason=reason)
    return {"updated": True, "channel_id": cid, "target_id": target, "allow": allow, "deny": deny}


# ── roles ──────────────────────────────────────────────────────────────────
def _list_roles(args: dict[str, Any]) -> dict[str, Any]:
    gid = _check_guild(args.get("guild_id"))
    roles = _request("GET", f"/guilds/{gid}/roles")
    items = [_role_summary(r) for r in sorted(
        roles, key=lambda r: r.get("position", 0), reverse=True)]
    return {"roles": items, "count": len(items)}


def _create_role(args: dict[str, Any]) -> dict[str, Any]:
    gid = _check_guild(args.get("guild_id"))
    name = _require_text(args.get("name"), "name", MAX_NAME_CHARS)
    body: dict[str, Any] = {"name": name}
    for key in ("color", "hoist", "mentionable"):
        if args.get(key) is not None:
            body[key] = args[key]
    if args.get("permissions") is not None:
        body["permissions"] = str(int(args["permissions"]))
    reason = _optional_text(args.get("reason"), "reason", MAX_REASON_CHARS)
    role = _request("POST", f"/guilds/{gid}/roles", body=body, reason=reason)
    return _role_summary(role)


def _edit_role(args: dict[str, Any]) -> dict[str, Any]:
    gid = _check_guild(args.get("guild_id"))
    rid = _require_id(args.get("role_id"), "role_id")
    if rid == gid:
        raise DiscordError("the @everyone role cannot be edited")
    body: dict[str, Any] = {}
    if args.get("name") is not None:
        body["name"] = _require_text(args["name"], "name", MAX_NAME_CHARS)
    for key in ("color", "hoist", "mentionable"):
        if args.get(key) is not None:
            body[key] = args[key]
    if args.get("permissions") is not None:
        body["permissions"] = str(int(args["permissions"]))
    if not body:
        raise DiscordError("nothing to change: supply at least one role field")
    reason = _optional_text(args.get("reason"), "reason", MAX_REASON_CHARS)
    role = _request("PATCH", f"/guilds/{gid}/roles/{rid}", body=body, reason=reason)
    return _role_summary(role)


def _delete_role(args: dict[str, Any]) -> dict[str, Any]:
    gid = _check_guild(args.get("guild_id"))
    rid = _require_id(args.get("role_id"), "role_id")
    if rid == gid:
        raise DiscordError("the @everyone role cannot be deleted")
    _require_confirm(args, "deleting a role")
    reason = _optional_text(args.get("reason"), "reason", MAX_REASON_CHARS)
    _request("DELETE", f"/guilds/{gid}/roles/{rid}", expect=(200, 204), reason=reason)
    return {"deleted": True, "role_id": rid}


def _assign_role(args: dict[str, Any]) -> dict[str, Any]:
    gid = _check_guild(args.get("guild_id"))
    uid = _require_id(args.get("user_id"), "user_id")
    rid = _require_id(args.get("role_id"), "role_id")
    reason = _optional_text(args.get("reason"), "reason", MAX_REASON_CHARS)
    _request("PUT", f"/guilds/{gid}/members/{uid}/roles/{rid}", expect=(200, 204), reason=reason)
    return {"assigned": True, "user_id": uid, "role_id": rid}


def _remove_role(args: dict[str, Any]) -> dict[str, Any]:
    gid = _check_guild(args.get("guild_id"))
    uid = _require_id(args.get("user_id"), "user_id")
    rid = _require_id(args.get("role_id"), "role_id")
    reason = _optional_text(args.get("reason"), "reason", MAX_REASON_CHARS)
    _request("DELETE", f"/guilds/{gid}/members/{uid}/roles/{rid}", expect=(200, 204), reason=reason)
    return {"removed": True, "user_id": uid, "role_id": rid}


# ── registry ───────────────────────────────────────────────────────────────
_GUILD_SCHEMA = {"guild_id": {"type": "string", "description": "Numeric guild (server) id."}}
_REASON_SCHEMA = {"reason": {"type": "string", "maxLength": MAX_REASON_CHARS,
                             "description": "Optional audit-log reason."}}
_CONFIRM_SCHEMA = {"confirm": {"type": "boolean", "default": False,
                               "description": "Must be true to perform a destructive action."}}


def _tool(name: str, description: str, properties: dict[str, Any],
          required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties,
                              "additionalProperties": False}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "inputSchema": schema}


TOOLS = [
    _tool("discord_status",
          "Check Discord configuration, guild allowlist, and whether the bot token is valid. Does not expose the token.",
          {}),
    _tool("discord_list_guilds",
          "List guilds the bot belongs to, filtered by ZS_DISCORD_GUILD_IDS when set.",
          {}),
    _tool("discord_get_guild",
          "Read one guild's name, owner, approximate member counts, and roles.",
          dict(_GUILD_SCHEMA), ["guild_id"]),
    _tool("discord_list_members",
          "List guild members with their role ids. Requires the GUILD_MEMBERS intent.",
          {**_GUILD_SCHEMA, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS, "default": 50}},
          ["guild_id"]),
    _tool("discord_list_channels",
          "List guild channels, optionally filtered by type (text, voice, category, announcement, stage, forum).",
          {**_GUILD_SCHEMA, "type": {"type": "string", "enum": sorted(CREATABLE_CHANNEL_TYPES)}},
          ["guild_id"]),
    _tool("discord_get_channel",
          "Read one channel including its permission overwrites.",
          {"channel_id": {"type": "string"}}, ["channel_id"]),
    _tool("discord_create_channel",
          "Create a text, voice, category, announcement, stage, or forum channel.",
          {**_GUILD_SCHEMA,
           "name": {"type": "string", "minLength": 1, "maxLength": MAX_NAME_CHARS},
           "type": {"type": "string", "enum": sorted(CREATABLE_CHANNEL_TYPES), "default": "text"},
           "topic": {"type": "string", "maxLength": MAX_TOPIC_CHARS},
           "nsfw": {"type": "boolean"},
           "position": {"type": "integer"},
           "parent_id": {"type": "string", "description": "Category id to nest under."},
           **_REASON_SCHEMA},
          ["guild_id", "name"]),
    _tool("discord_edit_channel",
          "Rename a channel or change its topic, nsfw flag, position, or parent category.",
          {"channel_id": {"type": "string"},
           "name": {"type": "string", "maxLength": MAX_NAME_CHARS},
           "topic": {"type": "string", "maxLength": MAX_TOPIC_CHARS},
           "nsfw": {"type": "boolean"},
           "position": {"type": "integer"},
           "parent_id": {"type": "string"},
           **_REASON_SCHEMA},
          ["channel_id"]),
    _tool("discord_delete_channel",
          "Permanently delete a channel. Requires confirm=true.",
          {"channel_id": {"type": "string"}, **_REASON_SCHEMA, **_CONFIRM_SCHEMA},
          ["channel_id", "confirm"]),
    _tool("discord_set_channel_overwrite",
          "Set a role or member permission overwrite on a channel using raw allow/deny bitmasks. Denying VIEW_CHANNEL or SEND_MESSAGES requires confirm=true.",
          {"channel_id": {"type": "string"},
           "target_id": {"type": "string", "description": "Role id or user id."},
           "target_type": {"type": "string", "enum": ["role", "member"]},
           "allow": {"type": "integer", "description": "Permission bitmask to allow."},
           "deny": {"type": "integer", "description": "Permission bitmask to deny."},
           "clear": {"type": "integer", "description": "Bits being removed; used only for the lockout safety check."},
           **_REASON_SCHEMA, **_CONFIRM_SCHEMA},
          ["channel_id", "target_id", "target_type"]),
    _tool("discord_list_roles",
          "List guild roles ordered by hierarchy position, highest first.",
          dict(_GUILD_SCHEMA), ["guild_id"]),
    _tool("discord_create_role",
          "Create a role with optional color, hoist, mentionable, and permission bitmask.",
          {**_GUILD_SCHEMA,
           "name": {"type": "string", "minLength": 1, "maxLength": MAX_NAME_CHARS},
           "color": {"type": "integer"},
           "hoist": {"type": "boolean"},
           "mentionable": {"type": "boolean"},
           "permissions": {"type": "integer", "description": "Permission bitmask."},
           **_REASON_SCHEMA},
          ["guild_id", "name"]),
    _tool("discord_edit_role",
          "Rename a role or change its color, hoist, mentionable, or permissions. The @everyone role cannot be edited.",
          {**_GUILD_SCHEMA,
           "role_id": {"type": "string"},
           "name": {"type": "string", "maxLength": MAX_NAME_CHARS},
           "color": {"type": "integer"},
           "hoist": {"type": "boolean"},
           "mentionable": {"type": "boolean"},
           "permissions": {"type": "integer"},
           **_REASON_SCHEMA},
          ["guild_id", "role_id"]),
    _tool("discord_delete_role",
          "Permanently delete a role. The @everyone role cannot be deleted. Requires confirm=true.",
          {**_GUILD_SCHEMA, "role_id": {"type": "string"}, **_REASON_SCHEMA, **_CONFIRM_SCHEMA},
          ["guild_id", "role_id", "confirm"]),
    _tool("discord_assign_role",
          "Give a role to a guild member.",
          {**_GUILD_SCHEMA, "user_id": {"type": "string"}, "role_id": {"type": "string"}, **_REASON_SCHEMA},
          ["guild_id", "user_id", "role_id"]),
    _tool("discord_remove_role",
          "Remove a role from a guild member.",
          {**_GUILD_SCHEMA, "user_id": {"type": "string"}, "role_id": {"type": "string"}, **_REASON_SCHEMA},
          ["guild_id", "user_id", "role_id"]),
]

HANDLERS = {
    "discord_status": _status,
    "discord_list_guilds": _list_guilds,
    "discord_get_guild": _get_guild,
    "discord_list_members": _list_members,
    "discord_list_channels": _list_channels,
    "discord_get_channel": _get_channel,
    "discord_create_channel": _create_channel,
    "discord_edit_channel": _edit_channel,
    "discord_delete_channel": _delete_channel,
    "discord_set_channel_overwrite": _set_channel_overwrite,
    "discord_list_roles": _list_roles,
    "discord_create_role": _create_role,
    "discord_edit_role": _edit_role,
    "discord_delete_role": _delete_role,
    "discord_assign_role": _assign_role,
    "discord_remove_role": _remove_role,
}


# ── JSON-RPC plumbing ──────────────────────────────────────────────────────
def _reply(message: dict[str, Any]) -> dict[str, Any] | None:
    if "id" not in message:
        return None
    method = message.get("method")
    request_id = message["id"]
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": f"unknown tool: {name}"}}
        try:
            result = handler(params.get("arguments") or {})
            text = _scrub(json.dumps(result, ensure_ascii=False, default=str))
            is_error = isinstance(result, dict) and result.get("success") is False
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            }}
        except DiscordError as exc:
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": _scrub(str(exc)), "type": "DiscordError"})}],
                "isError": True,
            }}
        except Exception as exc:  # keep the pipe alive on unexpected bugs
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": _scrub(str(exc)), "type": type(exc).__name__})}],
                "isError": True,
            }}
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    # stdout is reserved for JSON-RPC; diagnostics go to stderr only.
    for line in sys.stdin:
        try:
            message = json.loads(line)
            response = _reply(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except Exception:
            try:
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"}}) + "\n")
                sys.stdout.flush()
            except Exception:
                break


if __name__ == "__main__":
    main()
