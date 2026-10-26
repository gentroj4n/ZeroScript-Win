# Native Blender `bpy` integration

ZeroScript-Win's Blender backend is the Blender Python module (`bpy`) running
inside a dedicated Python process. It does **not** launch `blender.exe` for
each tool call. The long-lived MCP process retains its in-memory scene between
calls, and the bridge supervises it separately from the main WebSocket process.

## Requirements

- Windows x86-64 (the primary supported ZeroScript-Win platform).
- Python matching a published `bpy` wheel. The current documented pairing is
  CPython 3.13 + `bpy==5.2.2`; the wheel is large (hundreds of MB).
- Network access for the first package install.

Check the current wheel matrix at <https://pypi.org/project/bpy/> before changing
versions. Do not install `bpy` into the bridge's Python environment unless its
Python version matches the wheel. Keep it in a dedicated virtual environment so
Blender's native dependencies cannot destabilize ZeroScript's normal tools.

## Install

From the ZeroScript-Win repository root in PowerShell:

```powershell
py -3.13 -m venv .venv-bpy
.\.venv-bpy\Scripts\python.exe -m pip install --upgrade pip
.\.venv-bpy\Scripts\python.exe -m pip install bpy==5.2.2
.\.venv-bpy\Scripts\python.exe -c "import bpy; print(bpy.app.version_string)"
```

The final command must print a Blender version. If Python 3.13 or the specified
wheel is unavailable on your machine, check PyPI for a `bpy` release that has
a wheel for your installed Python version and architecture. Adjust both the
Python launcher and pinned package version together.

## Enable the ZeroScript tools

1. Back up your current `config.json`.
2. Copy the `bpy` server entry from `config.bpy.example.json` into the existing
   `mcpServers` object. Do not replace other MCP entries such as Roblox.
3. Set `command` to the full path of `.venv-bpy\Scripts\python.exe` (JSON
   backslashes must be escaped as `\\`) and retain the arguments
   `["-m", "zeroscript_blender.mcp_server"]`.
4. Restart ZeroScript-Win's bridge. The bridge will discover the tools through
   its normal MCP tool catalogue.
5. Call `bpy_status` to verify the runtime before creating or changing a scene.

The server inherits the bridge environment, including `ZS_WORKSPACE_ROOT`.
Project paths accepted by save/open/render/export tools are workspace-relative.
Do not add the server to the live config until the dedicated environment passes
the import check.

## Available tools

- `bpy_status`: runtime and scene status.
- `bpy_inspect_scene`: bounded structured scene/object inspection.
- `bpy_execute_script`: execute Python directly in the persistent `bpy` process.
- `bpy_open_project` / `bpy_save_project`: open and save workspace `.blend` files.
- `bpy_render`: render a still image into the workspace.
- `bpy_pack_textures`: pack external image textures into the current `.blend` and report failures.
- `bpy_validate_glb`: validate GLB v2 structure and report external/missing image or buffer references.
- `bpy_export`: export GLB/glTF, FBX, OBJ, STL, or PLY. GLB exports are structurally validated automatically.

## Important runtime constraints

- `bpy` is a large native extension. Keeping it in a child MCP process means a
  native crash should not directly terminate the main bridge.
- One `bpy` process owns one current scene/project at a time. Opening another
  `.blend` file replaces the current project in that process.
- This is not a security sandbox. `bpy_execute_script` runs arbitrary Python
  with the privileges of the current Windows user. It can access local files,
  start processes, and perform network operations. Only use it with trusted
  instructions and a trusted workspace.
- Save/open/render/export tools restrict output/input paths to the selected
  workspace and require explicit `overwrite=true` before replacing an existing
  output. Arbitrary script execution can bypass these path checks.
- Rendering may take substantial time and memory. The bridge's MCP timeout
  must be configured to accommodate real workloads; avoid treating a timeout as
  proof that a render or script had no side effects.
- Export operator names vary by Blender version. Unsupported operators return a
  clear error; they are not silently replaced with a CLI fallback.

## Official references

- Blender Python API: <https://docs.blender.org/api/current/>
- `bpy` wheels and supported Python/platform tags: <https://pypi.org/project/bpy/>
- Blender as a Python module: <https://docs.blender.org/api/3.5/info_advanced_blender_as_bpy.html>


## Blender-to-Roblox asset handoff

For a reliable handoff, export an FBX into the selected workspace with
`bpy_export`, then verify the file exists and is non-empty before calling the
Roblox upload tool. Supply the configured creator identity and all required
upload parameters during preflight. Preserve the returned Roblox asset ID and
moderation/upload status; if Studio disconnects after upload, resume at
insertion rather than uploading again. Refresh the Studio list before
insertion if a stale session is detected, and read back the inserted instance
to confirm its name, class, descendants, and approximate bounds.

The current bridge exposes the underlying tools separately; it does not yet
provide a fully automated transaction across Blender, Roblox upload, and Studio
insertion. Until that orchestration is added and exercised against the live
tool schemas, perform these steps explicitly and report insertion and visual
verification as separate states.


### Texture-safe GLB export

Use `bpy_export(path="exports/model.glb")` for the default Blender-to-Roblox
handoff. The GLB exporter explicitly exports materials, UV coordinates, normals,
tangents, vertex colors, and supported image textures in the single binary
container. Cameras and lights are excluded by default and can be enabled with
`export_cameras=true` or `export_lights=true`. Set `selected_only=true` to
export only selected objects.

GLB embedding does not guarantee every Blender shader node or procedural
texture can be represented by glTF. For predictable results, use image textures
connected through Principled BSDF inputs and pack external images in Blender
before export. Inspect the exported GLB in a viewer before upload. A successful
file write confirms the export artifact exists; it does not prove every material
matches Blender or that Roblox's importer supports every feature.


### Recommended pre-upload sequence

1. Call `bpy_pack_textures` and resolve any reported image-packing errors.
2. Call `bpy_export` with a workspace-relative `.glb` path. The response includes a structural validation report.
3. If the export validation reports external images/buffers or missing image sources, do not upload it. Call `bpy_validate_glb` to inspect the exact references.
4. Inspect the GLB in a viewer for material fidelity before uploading. Structural validation cannot prove that procedural Blender shaders match glTF or that Roblox supports every material feature.
5. Upload once, record the returned asset ID and moderation status, insert into Studio, and read back the inserted instance. Resume from the failed stage rather than repeating a successful upload.


### Recommended pre-upload sequence

1. Call `bpy_pack_textures` and resolve any reported image-packing errors.
2. Call `bpy_export` with a workspace-relative `.glb` path. The result includes structural validation.
3. If validation reports external images/buffers or missing image sources, do not upload; call `bpy_validate_glb` for details.
4. Inspect the GLB in a viewer for material fidelity. Structural validation cannot prove procedural shaders match glTF or that Roblox supports every material feature.
5. Upload once, preserve the returned asset ID and moderation state, insert into Studio, then read back the inserted instance. Resume from the failed stage rather than repeating a successful upload.
