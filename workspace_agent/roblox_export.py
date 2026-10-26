# SPDX-License-Identifier: GPL-3.0-or-later
"""Composite Roblox Studio tree exporter.

The bridge performs all filesystem writes locally. Studio only returns bounded
pages of script source through its existing MCP execute_luau tool, keeping the
bulk payload out of the agent conversation.
"""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any

MAX_PAGE_SCRIPTS = 10
MAX_FILES_PER_WRITE = 200
MAX_BYTES_PER_WRITE = 9 * 1024 * 1024


def _safe_segment(value: str) -> str:
    value = re.sub(r'[<>:"/\\\\|?*\x00-\x1f]', "_", value).strip().rstrip(". ")
    return value or "_unnamed"


def _script_extension(class_name: str) -> str:
    return {
        "Script": ".server.lua",
        "LocalScript": ".client.lua",
        "ModuleScript": ".module.lua",
    }.get(class_name, ".lua")


def _decode_payload(text: str) -> dict[str, Any]:
    """Decode direct JSON or JSON embedded in an MCP text envelope."""
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except (TypeError, ValueError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    raise RuntimeError("Studio returned a non-JSON export page. The source payload may have been truncated.")


def _find_execute_tool(client):
    for tool in client.tools_cache or []:
        name = str(tool.get("name", ""))
        if name == "execute_luau" or name.endswith("/execute_luau"):
            return tool
    raise RuntimeError("Roblox Studio MCP does not advertise execute_luau. Refresh the bridge's Roblox tools and try again.")


def _build_execute_args(tool: dict[str, Any], code: str) -> dict[str, Any]:
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    args: dict[str, Any] = {}
    code_key = next((key for key in ("code", "source", "luau") if key in props), None)
    if not code_key:
        raise RuntimeError("The connected execute_luau tool has no recognized code/source argument.")
    args[code_key] = code
    for key in required:
        if key in args:
            continue
        if key in ("datamodel_type", "data_model_type"):
            args[key] = "Edit"
        elif key == "target":
            args[key] = "studio"
        elif key in ("studio_id", "studioId"):
            # Some Studio MCP versions require an explicit studio id. Resolve it
            # through the server's own discovery tool below when possible.
            continue
        elif key in props and "default" in props[key]:
            args[key] = props[key]["default"]
        else:
            raise RuntimeError(
                f"Cannot safely satisfy required execute_luau argument {key!r}; "
                "the connected Studio MCP schema differs from the supported versions."
            )
    return args


def _studio_id(client, tool_schema: dict[str, Any]) -> str | None:
    props = (tool_schema.get("inputSchema") or {}).get("properties") or {}
    if "studio_id" not in props and "studioId" not in props:
        return None
    for tool in client.tools_cache or []:
        name = str(tool.get("name", ""))
        if name not in ("list_roblox_studios", "get_studio_state") and not name.endswith("/list_roblox_studios"):
            continue
        try:
            result = client.call_tool(name, {}, timeout=20)
            payload = _decode_payload(result.get("text", ""))
            studios = payload if isinstance(payload, list) else (payload.get("studios") or payload.get("instances") or [])
            if studios and isinstance(studios[0], dict):
                return str(studios[0].get("studio_id") or studios[0].get("studioId") or studios[0].get("id") or "") or None
        except Exception:
            continue
    return None


def export_tree(manager, args: dict[str, Any]) -> dict[str, Any]:
    """Export all script containers under game to workspace-relative files."""
    if not getattr(manager, "workspace_configured", False) or manager.workspace is None:
        raise RuntimeError("Select a local workspace first with workspace_browse or workspace_set_root.")
    client = manager.clients.get("roblox")
    if client is None or not client.is_alive():
        raise RuntimeError("Roblox Studio MCP is not connected. Open Studio and enable 'Studio as MCP server'.")
    if not isinstance(args, dict):
        raise RuntimeError("Export options must be an object.")

    root_path = args.get("root_path", "RobloxExport")
    overwrite = args.get("overwrite", False)
    exclude = args.get("exclude", [])
    max_script_bytes = args.get("max_script_bytes", 1024 * 1024)
    if not isinstance(root_path, str) or not root_path.strip():
        raise RuntimeError("root_path must be a non-empty workspace-relative directory.")
    if not isinstance(overwrite, bool):
        raise RuntimeError("overwrite must be a boolean.")
    if not isinstance(exclude, list) or not all(isinstance(item, str) and item.strip() for item in exclude):
        raise RuntimeError("exclude must be an array of non-empty path/name substrings.")
    if len(exclude) > 100:
        raise RuntimeError("exclude supports at most 100 entries.")
    if not isinstance(max_script_bytes, int) or isinstance(max_script_bytes, bool) or not 1 <= max_script_bytes <= 1024 * 1024:
        raise RuntimeError("max_script_bytes must be an integer between 1 and 1048576.")
    root = PurePosixPath(root_path.replace("\\", "/"))
    if not root.parts or root == PurePosixPath(".") or root.is_absolute() or any(part in ("", ".", "..") for part in root.parts):
        raise RuntimeError("root_path must be a safe workspace-relative directory.")
    if ":" in root_path or root_path.startswith("//"):
        raise RuntimeError("root_path must not contain drive or UNC path syntax.")

    if not any(str(item.get("name", "")) == "execute_luau" or str(item.get("name", "")).endswith("/execute_luau") for item in (client.tools_cache or [])):
        client.refresh_tools()
    tool = _find_execute_tool(client)
    studio_id = _studio_id(client, tool)
    schema_props = (tool.get("inputSchema") or {}).get("properties") or {}
    required = (tool.get("inputSchema") or {}).get("required") or []
    studio_key = "studio_id" if "studio_id" in schema_props else ("studioId" if "studioId" in schema_props else None)
    if studio_key and studio_key in required and not studio_id:
        raise RuntimeError("Could not identify the active Roblox Studio session required by execute_luau.")

    # First request counts and indexes scripts only. Each subsequent request
    # re-enumerates the same deterministic ordering and returns a bounded page.
    # Roblox Studio's native MCP runs this in Edit context, where Script.Source
    # is readable under the tool's plugin-level security.
    code_template = r'''
local HttpService = game:GetService("HttpService")
local items = {}
for _, inst in ipairs(game:GetDescendants()) do
    if inst:IsA("LuaSourceContainer") then
        local segments = {}
        local node = inst
        while node and node ~= game do
            table.insert(segments, 1, node.Name)
            node = node.Parent
        end
        table.insert(items, {instance = inst, segments = segments, className = inst.ClassName})
    end
end
table.sort(items, function(a, b)
    local ap, bp = table.concat(a.segments, "."), table.concat(b.segments, ".")
    if ap == bp then return a.className < b.className end
    return ap < bp
end)
local first = __FIRST__
local limit = __LIMIT__
local maxScriptBytes = __MAX_SCRIPT_BYTES__
local excludes = HttpService:JSONDecode(__EXCLUDES_JSON__)
local page = {}
for i = first, math.min(#items, first + limit - 1) do
    local item = items[i]
    local hierarchy = table.concat(item.segments, "/")
    local excluded = false
    local lowerHierarchy = string.lower(hierarchy)
    for _, needle in ipairs(excludes) do
        if string.find(lowerHierarchy, string.lower(needle), 1, true) then
            excluded = true
            break
        end
    end
    if excluded then
        table.insert(page, {index = i, segments = item.segments, className = item.className, excluded = true})
    else
        local ok, source = pcall(function() return item.instance.Source end)
        local sourceBytes = ok and #source or 0
        local tooLarge = ok and sourceBytes > maxScriptBytes
        table.insert(page, {
            index = i,
            segments = item.segments,
            className = item.className,
            source = (ok and not tooLarge) and source or "",
            sourceBytes = sourceBytes,
            oversized = tooLarge,
            sourceError = (not ok) and tostring(source) or nil
        })
    end
end
return HttpService:JSONEncode({total = #items, first = first, files = page})
'''
    page_size = MAX_PAGE_SCRIPTS
    all_entries: list[dict[str, Any]] = []
    total: int | None = None
    start = 1
    while total is None or start <= total:
        code = (code_template
            .replace("__FIRST__", str(start))
            .replace("__LIMIT__", str(page_size))
            .replace("__MAX_SCRIPT_BYTES__", str(max_script_bytes))
            .replace("__EXCLUDES_JSON__", json.dumps(json.dumps(exclude))))
        call_args = _build_execute_args(tool, code)
        if studio_key and studio_id:
            call_args[studio_key] = studio_id
        result = client.call_tool(tool["name"], call_args, timeout=120)
        payload = _decode_payload(result.get("text", ""))
        if total is None:
            total_value = payload.get("total")
            if not isinstance(total_value, int) or total_value < 0:
                raise RuntimeError("Studio export did not return a valid script count.")
            total = total_value
        files = payload.get("files")
        if not isinstance(files, list):
            raise RuntimeError(f"Studio export page starting at {start} did not include a files array.")
        all_entries.extend(files)
        if not files and start <= (total or 0):
            raise RuntimeError(f"Studio export returned an empty page at index {start}; aborting rather than silently exporting an incomplete tree.")
        start += page_size
        if len(all_entries) > 5000:
            raise RuntimeError("Export exceeds the safety limit of 5,000 script containers.")

    output_files: list[dict[str, str]] = []
    used_paths: set[str] = set()
    skipped_excluded: list[dict[str, Any]] = []
    skipped_oversized: list[dict[str, Any]] = []
    for entry in all_entries:
        segments = entry.get("segments")
        source = entry.get("source")
        class_name = entry.get("className", "LuaSourceContainer")
        if not isinstance(segments, list) or not segments or not all(isinstance(part, str) for part in segments):
            raise RuntimeError("Studio returned a script with invalid hierarchy metadata.")
        hierarchy = "/".join(segments)
        if entry.get("excluded"):
            skipped_excluded.append({"path": hierarchy, "reason": "matched exclude substring"})
            continue
        if entry.get("sourceError"):
            raise RuntimeError(f"Studio could not read source for {hierarchy}: {entry['sourceError']}")
        if entry.get("oversized"):
            skipped_oversized.append({
                "path": hierarchy,
                "bytes": entry.get("sourceBytes", 0),
                "limit_bytes": max_script_bytes,
            })
            continue
        if not isinstance(source, str):
            raise RuntimeError(f"Studio did not return source text for script index {entry.get('index')}.")
        safe_segments = [_safe_segment(part) for part in segments]
        leaf = safe_segments[-1] + _script_extension(str(class_name))
        relative = str(root.joinpath(*safe_segments[:-1], leaf))
        key = relative.casefold()
        if key in used_paths:
            # Roblox allows same-named siblings. Keep both by disambiguating
            # the leaf instead of dropping a script or overwriting a peer.
            index = entry.get("index", len(output_files) + 1)
            relative = str(root.joinpath(*safe_segments[:-1], f"{safe_segments[-1]}__{index}{_script_extension(str(class_name))}"))
            key = relative.casefold()
        if key in used_paths:
            raise RuntimeError(f"Could not generate a unique workspace path for {relative}.")
        used_paths.add(key)
        output_files.append({"path": relative, "content": source})

    # Keep each write under both the writer's 200-file cap and its 10 MiB cap.
    batches: list[list[dict[str, str]]] = []
    batch: list[dict[str, str]] = []
    batch_bytes = 0
    for item in output_files:
        size = len(item["content"].encode("utf-8"))
        if size > max_script_bytes:
            skipped_oversized.append({
                "path": item["path"],
                "bytes": size,
                "limit_bytes": max_script_bytes,
            })
            continue
        if batch and (len(batch) >= MAX_FILES_PER_WRITE or batch_bytes + size > MAX_BYTES_PER_WRITE):
            batches.append(batch)
            batch, batch_bytes = [], 0
        batch.append(item)
        batch_bytes += size
    if batch:
        batches.append(batch)

    written = 0
    bytes_written = 0
    batch_results = []
    for number, chunk in enumerate(batches, 1):
        result = manager.workspace.write_files(chunk, overwrite=overwrite)
        written += result.get("files_written", len(chunk))
        bytes_written += result.get("bytes_written", sum(len(x["content"].encode("utf-8")) for x in chunk))
        batch_results.append({"batch": number, "files": len(chunk), "bytes": result.get("bytes_written", 0)})

    return {
        "status": "complete",
        "workspace_root": str(manager.workspace.root),
        "export_root": str(root),
        "scripts_discovered": len(all_entries),
        "files_written": written,
        "bytes_written": bytes_written,
        "skipped_excluded_count": len(skipped_excluded),
        "skipped_excluded": skipped_excluded,
        "skipped_oversized_count": len(skipped_oversized),
        "skipped_oversized": skipped_oversized,
        "max_script_bytes": max_script_bytes,
        "exclude": exclude,
        "overwrite": overwrite,
        "batches": batch_results,
        "notes": [
            "Script, LocalScript, and ModuleScript source is preserved with .server.lua, .client.lua, and .module.lua suffixes.",
            "Non-script instances are not exported; hierarchy is preserved through directory paths.",
            "Roblox instance names are sanitized for Windows-compatible filenames.",
            "Oversized scripts and excluded hierarchy paths are skipped and reported; they do not abort the export.",
            "exclude entries are case-insensitive substrings matched against slash-separated instance hierarchy paths.",
        ],
    }
