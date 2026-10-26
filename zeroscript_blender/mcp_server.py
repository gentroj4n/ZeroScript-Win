# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal stdio MCP server exposing Blender's bpy API to ZeroScript-Win.

Run this module with the Python interpreter that has the matching bpy wheel
installed. The bridge supervises this process separately so a Blender/native
extension crash cannot take down the main WebSocket bridge.
"""
from __future__ import annotations

import contextlib
import io
import json
import struct
import os
import sys
import traceback
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "zeroscript-bpy"
SERVER_VERSION = "0.1.0"
MAX_SCRIPT_CHARS = 100_000
MAX_OUTPUT_CHARS = 30_000
MAX_OBJECTS = 500
WORKSPACE_ROOT = Path(os.environ.get("ZS_WORKSPACE_ROOT", os.getcwd())).expanduser().resolve()


def _workspace_path(raw: str, *, must_exist: bool = False) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path must be a non-empty workspace-relative path")
    candidate = (WORKSPACE_ROOT / raw).resolve()
    try:
        candidate.relative_to(WORKSPACE_ROOT)
    except ValueError as exc:
        raise ValueError("path escapes the selected ZeroScript workspace") from exc
    if must_exist and not candidate.is_file():
        raise ValueError(f"file does not exist: {raw}")
    return candidate


def _bpy():
    try:
        import bpy
        return bpy
    except Exception as exc:
        raise RuntimeError(
            "The bpy module is unavailable in this Python environment. "
            "Install a bpy wheel compatible with this interpreter; see "
            "docs/BLENDER_BPY.md. This server does not fall back to blender.exe."
        ) from exc


def _status(_args: dict[str, Any]) -> dict[str, Any]:
    bpy = _bpy()
    return {
        "available": True,
        "backend": "bpy-python-module",
        "blender_version": bpy.app.version_string,
        "blender_version_tuple": list(bpy.app.version),
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "workspace_root": str(WORKSPACE_ROOT),
        "blend_file": bpy.data.filepath or None,
        "objects": len(bpy.data.objects),
        "render_engine": bpy.context.scene.render.engine if bpy.context.scene else None,
    }


def _inspect_scene(args: dict[str, Any]) -> dict[str, Any]:
    bpy = _bpy()
    limit = max(1, min(int(args.get("max_objects", 100)), MAX_OBJECTS))
    objects = []
    for obj in list(bpy.data.objects)[:limit]:
        item: dict[str, Any] = {
            "name": obj.name,
            "type": obj.type,
            "location": [round(float(v), 6) for v in obj.location],
            "rotation_euler": [round(float(v), 6) for v in obj.rotation_euler],
            "scale": [round(float(v), 6) for v in obj.scale],
            "collections": sorted(c.name for c in obj.users_collection),
            "materials": [m.name for m in obj.data.materials if m] if hasattr(obj.data, "materials") else [],
        }
        if obj.type == "MESH":
            item["vertices"] = len(obj.data.vertices)
            item["polygons"] = len(obj.data.polygons)
        objects.append(item)
    scene = bpy.context.scene
    return {
        "scene": scene.name if scene else None,
        "blend_file": bpy.data.filepath or None,
        "object_count": len(bpy.data.objects),
        "objects_returned": len(objects),
        "truncated": len(bpy.data.objects) > limit,
        "objects": objects,
        "render": {
            "engine": scene.render.engine,
            "resolution_x": scene.render.resolution_x,
            "resolution_y": scene.render.resolution_y,
            "resolution_percentage": scene.render.resolution_percentage,
            "fps": scene.render.fps,
        } if scene else None,
    }


def _execute_script(args: dict[str, Any]) -> dict[str, Any]:
    bpy = _bpy()
    source = args.get("script")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("script must be a non-empty string")
    if len(source) > MAX_SCRIPT_CHARS:
        raise ValueError(f"script exceeds the {MAX_SCRIPT_CHARS}-character limit")
    filename = str(args.get("filename") or "<zeroscript-bpy>")
    stdout = io.StringIO()
    stderr = io.StringIO()
    scope = {"bpy": bpy, "__name__": "__zeroscript_bpy__", "__file__": filename}
    try:
        compiled = compile(source, filename, "exec")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(compiled, scope, scope)
    except BaseException as exc:
        error = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        return {
            "success": False,
            "error_type": type(exc).__name__,
            "error": error[-MAX_OUTPUT_CHARS:],
            "stdout": stdout.getvalue()[-MAX_OUTPUT_CHARS:],
            "stderr": stderr.getvalue()[-MAX_OUTPUT_CHARS:],
            "blend_file": bpy.data.filepath or None,
        }
    return {
        "success": True,
        "stdout": stdout.getvalue()[-MAX_OUTPUT_CHARS:],
        "stderr": stderr.getvalue()[-MAX_OUTPUT_CHARS:],
        "blend_file": bpy.data.filepath or None,
        "objects": len(bpy.data.objects),
    }


def _save_project(args: dict[str, Any]) -> dict[str, Any]:
    bpy = _bpy()
    path = _workspace_path(args.get("path", "scene.blend"))
    if path.suffix.lower() != ".blend":
        raise ValueError("project path must end in .blend")
    if path.exists() and not bool(args.get("overwrite", False)):
        raise ValueError("target already exists; set overwrite=true to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    if not path.is_file():
        raise RuntimeError("Blender reported save completion but the project file was not found")
    return {"success": True, "path": str(path), "bytes": path.stat().st_size}


def _open_project(args: dict[str, Any]) -> dict[str, Any]:
    bpy = _bpy()
    path = _workspace_path(args.get("path", ""), must_exist=True)
    if path.suffix.lower() != ".blend":
        raise ValueError("project path must end in .blend")
    bpy.ops.wm.open_mainfile(filepath=str(path))
    return {"success": True, "path": str(path), "objects": len(bpy.data.objects), "scene": bpy.context.scene.name}


def _render(args: dict[str, Any]) -> dict[str, Any]:
    bpy = _bpy()
    path = _workspace_path(args.get("path", "render.png"))
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".webp"}:
        raise ValueError("render output must use a supported image extension")
    if path.exists() and not bool(args.get("overwrite", False)):
        raise ValueError("render target already exists; set overwrite=true to replace it")
    scene = bpy.context.scene
    width = max(16, min(int(args.get("width", 1024)), 8192))
    height = max(16, min(int(args.get("height", 1024)), 8192))
    percentage = max(1, min(int(args.get("percentage", 100)), 100))
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = percentage
    scene.render.filepath = str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.render.render(write_still=True)
    if not path.is_file():
        raise RuntimeError("render finished without creating the requested output file")
    return {"success": True, "path": str(path), "bytes": path.stat().st_size, "width": width, "height": height}


def _pack_textures(args: dict[str, Any]) -> dict[str, Any]:
    """Pack external image files into the current .blend."""
    bpy = _bpy()
    packed, skipped, errors = [], [], []
    for image in bpy.data.images:
        if image.source in {"GENERATED", "VIEWER", "TILED"} or image.packed_file is not None or not image.filepath:
            skipped.append({"name": image.name, "reason": "generated/viewer/tiled, already packed, or no filepath"})
            continue
        try:
            image.pack()
            if image.packed_file is None:
                raise RuntimeError("image.pack() did not create packed_file")
            packed.append({"name": image.name, "filepath": image.filepath})
        except Exception as exc:
            errors.append({"name": image.name, "filepath": image.filepath, "error": str(exc)})
    return {"success": not errors, "images_packed": len(packed), "packed": packed,
            "skipped_count": len(skipped), "skipped": skipped, "errors": errors}


def _validate_glb(args: dict[str, Any]) -> dict[str, Any]:
    """Validate the GLB v2 header and identify external image/buffer references."""
    path = _workspace_path(args.get("path", ""))
    if path.suffix.lower() != ".glb":
        raise ValueError("path must point to a .glb file")
    if not path.is_file():
        raise ValueError("GLB file does not exist")
    size = path.stat().st_size
    if size < 20:
        raise ValueError("GLB is too small to contain a valid header and JSON chunk")
    with path.open("rb") as handle:
        magic, version, declared_length = struct.unpack("<4sII", handle.read(12))
        if magic != b"glTF" or version != 2:
            raise ValueError("invalid GLB header; expected glTF version 2")
        if declared_length != size:
            raise ValueError(f"GLB length mismatch: header={declared_length}, actual={size}")
        chunk_header = handle.read(8)
        if len(chunk_header) != 8:
            raise ValueError("GLB JSON chunk header is missing")
        json_length, json_type = struct.unpack("<II", chunk_header)
        if json_type != 0x4E4F534A:
            raise ValueError("first GLB chunk is not JSON")
        raw = handle.read(json_length)
        if len(raw) != json_length:
            raise ValueError("GLB JSON chunk is truncated")
    try:
        doc = json.loads(raw.decode("utf-8").rstrip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"GLB JSON is invalid: {exc}") from exc
    images = doc.get("images") or []
    buffers = doc.get("buffers") or []
    external_images = [{"index": i, "uri": item["uri"]} for i, item in enumerate(images)
                       if isinstance(item, dict) and isinstance(item.get("uri"), str)
                       and not item["uri"].startswith("data:")]
    missing_images = [i for i, item in enumerate(images)
                      if isinstance(item, dict) and not item.get("uri") and "bufferView" not in item]
    external_buffers = [{"index": i, "uri": item["uri"]} for i, item in enumerate(buffers)
                        if isinstance(item, dict) and isinstance(item.get("uri"), str)
                        and not item["uri"].startswith("data:")]
    return {"success": not external_images and not missing_images and not external_buffers,
            "path": str(path), "bytes": size, "glb_version": version,
            "mesh_count": len(doc.get("meshes") or []),
            "material_count": len(doc.get("materials") or []),
            "image_count": len(images),
            "embedded_image_count": sum(1 for item in images if isinstance(item, dict) and "bufferView" in item),
            "external_images": external_images, "images_without_source": missing_images,
            "external_buffers": external_buffers,
            "note": "Structural check only; does not prove visual fidelity or Roblox importer compatibility."}


def _export(args: dict[str, Any]) -> dict[str, Any]:
    bpy = _bpy()
    path = _workspace_path(args.get("path", "export.glb"))
    ext = path.suffix.lower()
    if ext not in {".glb", ".gltf", ".fbx", ".obj", ".stl", ".ply"}:
        raise ValueError("supported export extensions are .glb, .gltf, .fbx, .obj, .stl, and .ply")
    if path.exists() and not bool(args.get("overwrite", False)):
        raise ValueError("export target already exists; set overwrite=true to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    operators = {
        ".glb": lambda: bpy.ops.export_scene.gltf(
            filepath=str(path), export_format="GLB",
            use_selection=bool(args.get("selected_only", False)),
            export_image_format="AUTO",
            export_materials="EXPORT",
            export_texcoords=True, export_normals=True,
            export_tangents=True, export_colors=True,
            export_cameras=bool(args.get("export_cameras", False)),
            export_lights=bool(args.get("export_lights", False)),
        ),
        ".gltf": lambda: bpy.ops.export_scene.gltf(
            filepath=str(path), export_format="GLTF_SEPARATE",
            use_selection=bool(args.get("selected_only", False)),
            export_image_format="AUTO", export_materials="EXPORT",
            export_texcoords=True, export_normals=True,
            export_tangents=True, export_colors=True,
        ),
        ".fbx": lambda: bpy.ops.export_scene.fbx(filepath=str(path), use_selection=bool(args.get("selected_only", False))),
        ".obj": lambda: bpy.ops.wm.obj_export(filepath=str(path)),
        ".stl": lambda: bpy.ops.wm.stl_export(filepath=str(path)),
        ".ply": lambda: bpy.ops.wm.ply_export(filepath=str(path)),
    }
    try:
        result = operators[ext]()
    except AttributeError as exc:
        raise RuntimeError(f"this bpy version does not expose the {ext} exporter operator") from exc
    if "FINISHED" not in result or not path.is_file():
        raise RuntimeError(f"export did not finish successfully (operator result: {result})")
    result_payload = {"success": True, "path": str(path), "bytes": path.stat().st_size, "format": ext[1:]}
    if ext == ".glb":
        validation = _validate_glb({"path": str(path.relative_to(WORKSPACE_ROOT))})
        result_payload["validation"] = validation
        if not validation["success"]:
            result_payload["success"] = False
            result_payload["error"] = "GLB contains external or missing image/buffer references; inspect validation details."
    return result_payload


TOOLS = [
    {
        "name": "bpy_status",
        "description": "Check the native bpy runtime, Blender/Python versions, current scene, and workspace.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "bpy_inspect_scene",
        "description": "Inspect the current Blender scene and bounded object/material/mesh metadata.",
        "inputSchema": {"type": "object", "properties": {"max_objects": {"type": "integer", "minimum": 1, "maximum": MAX_OBJECTS}}, "additionalProperties": False},
    },
    {
        "name": "bpy_execute_script",
        "description": "Execute Python directly in the persistent bpy runtime. Arbitrary code execution: can modify scene data and access local files/processes. Use only for the user's requested task.",
        "inputSchema": {"type": "object", "properties": {"script": {"type": "string", "minLength": 1, "maxLength": MAX_SCRIPT_CHARS}, "filename": {"type": "string"}}, "required": ["script"], "additionalProperties": False},
    },
    {
        "name": "bpy_open_project",
        "description": "Open a .blend file within the selected ZeroScript workspace. Replaces the currently loaded bpy project.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
    },
    {
        "name": "bpy_save_project",
        "description": "Save the current bpy scene to a workspace-relative .blend path. Existing files require overwrite=true.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}, "required": ["path"], "additionalProperties": False},
    },
    {
        "name": "bpy_render",
        "description": "Render the current scene to an image inside the selected workspace. Existing files require overwrite=true.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "width": {"type": "integer", "minimum": 16, "maximum": 8192}, "height": {"type": "integer", "minimum": 16, "maximum": 8192}, "percentage": {"type": "integer", "minimum": 1, "maximum": 100}, "overwrite": {"type": "boolean", "default": False}}, "required": ["path"], "additionalProperties": False},
    },
    {
        "name": "bpy_pack_textures",
        "description": "Pack external image textures into the current Blender project before export and report failures.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "bpy_validate_glb",
        "description": "Validate a workspace GLB v2 file and report external or missing image/buffer references before upload.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
    },
    {
        "name": "bpy_export",
        "description": "Export the current scene to GLB/glTF, FBX, OBJ, STL, or PLY. GLB is a single-file binary glTF with supported image textures embedded; materials, UVs, normals, tangents, and vertex colors are exported where supported. Output stays inside the workspace.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}, "selected_only": {"type": "boolean", "default": False}, "export_cameras": {"type": "boolean", "default": False}, "export_lights": {"type": "boolean", "default": False}}, "required": ["path"], "additionalProperties": False},
    },
]

HANDLERS = {
    "bpy_status": _status,
    "bpy_inspect_scene": _inspect_scene,
    "bpy_execute_script": _execute_script,
    "bpy_open_project": _open_project,
    "bpy_save_project": _save_project,
    "bpy_render": _render,
    "bpy_export": _export,
    "bpy_pack_textures": _pack_textures,
    "bpy_validate_glb": _validate_glb,
}


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
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": f"unknown tool: {name}"}}
        try:
            result = handler(params.get("arguments") or {})
            text = json.dumps(result, ensure_ascii=False, default=str)
            is_error = result.get("success") is False
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            }}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": json.dumps({"error": str(exc), "type": type(exc).__name__})}],
                "isError": True,
            }}
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}


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
            # Keep protocol output valid even for malformed requests.
            try:
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
                sys.stdout.flush()
            except Exception:
                break


if __name__ == "__main__":
    main()
