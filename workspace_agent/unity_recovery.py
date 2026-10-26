# SPDX-License-Identifier: GPL-3.0-or-later
"""Targeted inspection and recovery helpers for installed Unity player builds.

This module never modifies the original game installation. Catalog output and
decompiled source are written beneath the selected ArcScript workspace.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv",
    "__pycache__", "Library", "Temp", "Logs", "obj", "bin",
}
ASSET_SUFFIXES = {
    ".assets": "unity_serialized_assets",
    ".bundle": "asset_bundle",
    ".unity3d": "asset_bundle",
    ".resource": "unity_resource",
    ".resS": "unity_streamed_resource",
    ".dll": "managed_or_native_assembly",
    ".so": "native_library",
    ".dylib": "native_library",
    ".exe": "executable",
    ".json": "json_data",
    ".manifest": "manifest",
}
MAX_CATALOG_BATCH = 2000
MAX_SCAN_SECONDS = 20
MAX_DECOMPILE_SECONDS = 180


class UnityRecoveryError(RuntimeError):
    """A bounded Unity recovery operation could not be completed safely."""


def _directory(value: str | os.PathLike[str], label: str = "directory") -> Path:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise UnityRecoveryError(f"A {label} path is required.")
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise UnityRecoveryError(f"The {label} does not exist or is not a directory: {path}")
    return path


def _safe_workspace_path(workspace_root: str | os.PathLike[str], relative: str) -> Path:
    root = _directory(workspace_root, "workspace")
    if not isinstance(relative, str) or not relative.strip():
        raise UnityRecoveryError("A workspace-relative output path is required.")
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise UnityRecoveryError("Output path escapes the selected workspace.")
    return candidate


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _version_from_global_managers(path: Path) -> str | None:
    try:
        data = path.read_bytes()[:2_000_000]
    except OSError:
        return None
    # Unity stores the editor/player version as an ASCII string in this file.
    match = re.search(rb"\b\d{4}\.\d+\.\d+[abfp]\d+\b", data)
    return match.group(0).decode("ascii") if match else None


def _classify_install(root: Path) -> dict[str, Any]:
    managed_dirs = sorted(root.glob("*_Data/Managed"))
    data_dirs = sorted(p for p in root.iterdir() if p.is_dir() and p.name.endswith("_Data"))
    assembly_csharp = []
    for managed in managed_dirs:
        for name in ("Assembly-CSharp.dll", "Assembly-CSharp-firstpass.dll"):
            p = managed / name
            if p.is_file():
                assembly_csharp.append({
                    "path": p.relative_to(root).as_posix(),
                    "size_bytes": p.stat().st_size,
                    "modified_unix": int(p.stat().st_mtime),
                })
    has_game_assembly = any((d / "GameAssembly.dll").is_file() for d in data_dirs) or (root / "GameAssembly.dll").is_file()
    has_mono = bool(assembly_csharp) or any((d / "MonoBleedingEdge").exists() for d in data_dirs)
    backend = "IL2CPP" if has_game_assembly else ("Mono" if has_mono else "unknown")
    version = None
    for data_dir in data_dirs:
        version = _version_from_global_managers(data_dir / "globalgamemanagers")
        if version:
            break
    asset_files = []
    for data_dir in data_dirs:
        for p in data_dir.iterdir():
            if p.is_file() and (p.name == "resources.assets" or p.suffix.lower() in {".assets", ".ress"}):
                stat = p.stat()
                asset_files.append({"path": p.relative_to(root).as_posix(), "size_bytes": stat.st_size})
    asset_files.sort(key=lambda x: x["size_bytes"], reverse=True)
    return {
        "install_path": str(root),
        "unity_version": version,
        "scripting_backend": backend,
        "evidence": {
            "assembly_csharp_found": bool(assembly_csharp),
            "game_assembly_found": has_game_assembly,
            "managed_directories": [p.relative_to(root).as_posix() for p in managed_dirs],
            "data_directories": [p.relative_to(root).as_posix() for p in data_dirs],
        },
        "managed_assemblies": assembly_csharp,
        "asset_files": asset_files,
        "asset_file_count": len(asset_files),
        "asset_bytes_total": sum(x["size_bytes"] for x in asset_files),
        "recommendations": [
            "Use ILSpy/ilspycmd for managed assemblies when the backend is Mono.",
            "Catalog assets before attempting bulk extraction; do not copy or overwrite originals.",
            "A player installation is not an editable Unity project unless Assets, Packages, and ProjectSettings are separately recovered.",
        ],
    }


def scan_unity_installation(install_path: str, workspace_root: str) -> dict[str, Any]:
    """Inspect a Unity player installation without writing to it."""
    root = _directory(install_path or workspace_root, "Unity installation")
    report = _classify_install(root)
    report["scan_time_unix"] = int(time.time())
    report["likely_player_build"] = bool(report["evidence"]["data_directories"])
    report["editable_project_markers"] = {
        name: (root / name).is_dir() for name in ("Assets", "Packages", "ProjectSettings")
    }
    report["editable_project_present"] = all(report["editable_project_markers"].values())
    return report


def _walk_files(root: Path):
    """Yield normalized relative file paths in deterministic order without following symlinks."""
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs[:] = sorted(
            d for d in dirs
            if d not in IGNORED_DIRS and not (current_path / d).is_symlink()
        )
        for name in sorted(files):
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                continue
            yield path, path.relative_to(root).as_posix()


def catalog_unity_assets(
    install_path: str,
    workspace_root: str,
    output_path: str = "recovery/asset-catalog.json",
    batch_size: int = 500,
) -> dict[str, Any]:
    """Build/update a resumable file-level catalog; this does not parse asset internals."""
    root = _directory(install_path or workspace_root, "Unity installation")
    batch_size = int(batch_size)
    if batch_size < 1 or batch_size > MAX_CATALOG_BATCH:
        raise UnityRecoveryError(f"batch_size must be between 1 and {MAX_CATALOG_BATCH}.")
    output = _safe_workspace_path(workspace_root, output_path)
    state_path = output.with_suffix(output.suffix + ".state.json")
    if output.exists():
        try:
            catalog = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UnityRecoveryError(f"Existing catalog is unreadable; refusing to overwrite it: {exc}") from exc
        if catalog.get("install_path") != str(root):
            raise UnityRecoveryError("Catalog belongs to a different installation; choose another output_path.")
    else:
        catalog = {
            "schema_version": 1,
            "install_path": str(root),
            "created_unix": int(time.time()),
            "completed": False,
            "cursor": "",
            "files": [],
            "summary": {},
        }
    if catalog.get("completed"):
        return {"status": "complete", "catalog_path": str(output), "file_count": len(catalog["files"]), "message": "Catalog is already complete."}

    cursor = str(catalog.get("cursor", ""))
    added = 0
    processed = 0
    started = time.monotonic()
    last_path = cursor
    more_after_batch = False
    # Sorting by normalized relative path makes the persisted cursor stable
    # across batches, even though os.walk itself is depth-first rather than
    # globally lexicographic.
    for path, rel in sorted(_walk_files(root), key=lambda item: item[1]):
        if rel <= cursor:
            continue
        if added >= batch_size or time.monotonic() - started > MAX_SCAN_SECONDS:
            more_after_batch = True
            break
        try:
            stat = path.stat()
        except OSError:
            continue
        suffix = path.suffix
        category = ASSET_SUFFIXES.get(suffix, ASSET_SUFFIXES.get(suffix.lower(), "other"))
        item = {
            "path": rel,
            "name": path.name,
            "extension": suffix,
            "category": category,
            "size_bytes": stat.st_size,
            "modified_unix": int(stat.st_mtime),
        }
        catalog["files"].append(item)
        last_path = rel
        added += 1
        processed += 1
    catalog["files"].sort(key=lambda x: x["path"].casefold())
    # Cursor is the last processed relative path, so a retry resumes after it.
    # The scan is file-metadata-only and does not read large asset contents.
    catalog["cursor"] = last_path
    # Detect whether another batch remains, without materializing the tree.
    if not more_after_batch:
        for _, rel in sorted(_walk_files(root), key=lambda item: item[1]):
            if rel > last_path:
                more_after_batch = True
                break
    catalog["completed"] = not more_after_batch
    summary: dict[str, dict[str, int]] = {}
    for item in catalog["files"]:
        group = summary.setdefault(item["category"], {"files": 0, "bytes": 0})
        group["files"] += 1
        group["bytes"] += item["size_bytes"]
    catalog["summary"] = summary
    catalog["updated_unix"] = int(time.time())
    _atomic_json(output, catalog)
    if more_after_batch:
        _atomic_json(state_path, {
            "install_path": str(root),
            "cursor": last_path,
            "catalog_path": str(output),
            "updated_unix": int(time.time()),
        })
    elif state_path.exists():
        state_path.unlink()
    return {
        "status": "complete" if catalog["completed"] else "in_progress",
        "catalog_path": str(output),
        "state_path": str(state_path) if more_after_batch else None,
        "batch_added": added,
        "file_count": len(catalog["files"]),
        "next_cursor": last_path if more_after_batch else None,
        "summary": summary,
        "message": "File-level inventory only; Unity serialized object dependencies are not parsed by this catalog.",
    }


def inspect_recovery_tooling(workspace_root: str) -> dict[str, Any]:
    """Report tool availability without installing software or touching game files."""
    root = _directory(workspace_root, "workspace")
    commands = ("dotnet", "ilspycmd", "AssetRipper.Console", "AssetRipper", "Unity", "node", "npm", "git", "7z", "7zz")
    found = {name: shutil.which(name) for name in commands}
    managed = sorted(str(p.relative_to(root)) for p in root.rglob("*.dll") if p.is_file() and len(str(p)) < 1000)[:200]
    browser_port = root / "browser-port"
    try:
        from importlib.util import find_spec
        python_playwright = find_spec("playwright") is not None
        unitypy_available = find_spec("UnityPy") is not None
    except (ImportError, ValueError):
        python_playwright = False
        unitypy_available = False
    return {
        "tools": found,
        "python_playwright_available": python_playwright,
        "unitypy_available": unitypy_available,
        "workspace": str(root),
        "browser_port_present": browser_port.is_dir(),
        "browser_port_manifests": [
            p.relative_to(root).as_posix()
            for p in (browser_port / "package.json", browser_port / "index.html", browser_port / "playwright.config.js")
            if p.is_file()
        ],
        "workspace_managed_assemblies": managed,
        "next_steps": [
            "If ilspycmd is installed, use decompile_unity_type or export_unity_assembly.",
            "Use catalog_unity_assets before launching large AssetRipper exports.",
            "If UnityPy is installed, inspect one bounded smaller serialized file with inspect_unity_serialized_file; do not load a multi-gigabyte resources.assets file wholesale.",
            "Use Playwright only after the browser port's local server and test command are identified.",
        ],
    }


def _ilspycmd() -> str:
    executable = shutil.which("ilspycmd")
    if executable:
        return executable
    dotnet = shutil.which("dotnet")
    raise UnityRecoveryError("ilspycmd is not installed or not on PATH. Install ILSpy's CLI deliberately, then retry; no software was installed automatically.")


def decompile_unity_type(
    install_path: str,
    workspace_root: str,
    type_name: str,
    assembly_path: str = "",
    timeout: int = 90,
) -> dict[str, Any]:
    """Decompile one managed type to text using ILSpy CLI, with bounded execution."""
    root = _directory(install_path, "Unity installation")
    workspace = _directory(workspace_root, "workspace")
    if not isinstance(type_name, str) or not type_name.strip() or len(type_name) > 500:
        raise UnityRecoveryError("A fully or partially qualified managed type name is required.")
    if assembly_path:
        assembly = Path(assembly_path).expanduser().resolve()
        if not assembly.is_file() or assembly.suffix.lower() != ".dll":
            raise UnityRecoveryError("assembly_path must identify an existing DLL.")
    else:
        candidates = sorted(root.glob("*_Data/Managed/Assembly-CSharp.dll"))
        if not candidates:
            raise UnityRecoveryError("Assembly-CSharp.dll was not found under <Game>_Data/Managed.")
        assembly = candidates[0]
    timeout = max(1, min(int(timeout), MAX_DECOMPILE_SECONDS))
    try:
        result = subprocess.run(
            [_ilspycmd(), "-t", type_name, str(assembly)],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UnityRecoveryError(f"ILSpy timed out after {timeout}s while decompiling {type_name}.") from exc
    if result.returncode != 0:
        raise UnityRecoveryError(f"ILSpy failed (exit {result.returncode}): {(result.stderr or result.stdout)[-4000:]}")
    return {
        "type_name": type_name,
        "assembly_path": str(assembly),
        "source": result.stdout[:100_000],
        "truncated": len(result.stdout) > 100_000,
        "tool": "ilspycmd",
    }


def export_unity_assembly(
    install_path: str,
    workspace_root: str,
    assembly_path: str = "",
    output_path: str = "recovery/decompiled",
    timeout: int = 180,
) -> dict[str, Any]:
    """Export one managed assembly into a new workspace directory via ILSpy CLI."""
    root = _directory(install_path, "Unity installation")
    workspace = _directory(workspace_root, "workspace")
    if assembly_path:
        assembly = Path(assembly_path).expanduser().resolve()
    else:
        candidates = sorted(root.glob("*_Data/Managed/Assembly-CSharp.dll"))
        if not candidates:
            raise UnityRecoveryError("Assembly-CSharp.dll was not found under <Game>_Data/Managed.")
        assembly = candidates[0]
    if not assembly.is_file() or assembly.suffix.lower() != ".dll":
        raise UnityRecoveryError("assembly_path must identify an existing DLL.")
    output = _safe_workspace_path(workspace, output_path)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise UnityRecoveryError(f"Refusing to overwrite a non-empty export directory: {output}")
    timeout = max(1, min(int(timeout), MAX_DECOMPILE_SECONDS))
    output.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [_ilspycmd(), "-p", "-o", str(output), str(assembly)],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UnityRecoveryError(f"ILSpy export timed out after {timeout}s.") from exc
    if result.returncode != 0:
        raise UnityRecoveryError(f"ILSpy export failed (exit {result.returncode}): {(result.stderr or result.stdout)[-4000:]}")
    files = [p.relative_to(output).as_posix() for p in output.rglob("*.cs") if p.is_file()]
    return {
        "assembly_path": str(assembly),
        "output_path": str(output),
        "source_file_count": len(files),
        "sample_files": files[:100],
        "tool": "ilspycmd",
    }
