# SPDX-License-Identifier: GPL-3.0-or-later
"""Searchable ILSpy-backed managed-code intelligence for Unity Mono builds."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from workspace_agent.unity_recovery import (
    IGNORED_DIRS, UnityRecoveryError, _directory, _safe_workspace_path,
    MAX_DECOMPILE_SECONDS,
)

MAX_RESULTS = 500
MAX_SOURCE_BYTES = 8_000_000
LIST_MODES = {"types": "t", "classes": "c", "methods": "m", "fields": "f", "namespaces": "n"}


def _ilspy() -> str:
    exe = shutil.which("ilspycmd")
    if not exe:
        raise UnityRecoveryError("ilspycmd is not installed or not on PATH. Install ILSpy CLI deliberately; no software was installed automatically.")
    return exe


def _assembly(install_path: str, assembly_path: str = "") -> Path:
    if assembly_path:
        candidate = Path(assembly_path).expanduser().resolve()
        if not candidate.is_file() or candidate.suffix.lower() != ".dll":
            raise UnityRecoveryError("assembly_path must identify an existing DLL.")
        return candidate
    root = _directory(install_path, "Unity installation")
    candidates = sorted(root.glob("*_Data/Managed/Assembly-CSharp.dll"))
    if not candidates:
        raise UnityRecoveryError("Assembly-CSharp.dll was not found under <Game>_Data/Managed.")
    return candidates[0]


def list_unity_symbols(
    install_path: str,
    symbol_kind: str = "types",
    assembly_path: str = "",
    query: str = "",
    limit: int = 200,
    timeout: int = 90,
) -> dict[str, Any]:
    """List symbols using ILSpy's metadata listing; optionally filter by substring."""
    if symbol_kind not in LIST_MODES:
        raise UnityRecoveryError("symbol_kind must be one of: " + ", ".join(sorted(LIST_MODES)))
    limit = max(1, min(int(limit), MAX_RESULTS))
    timeout = max(1, min(int(timeout), MAX_DECOMPILE_SECONDS))
    assembly = _assembly(install_path, assembly_path)
    command = [_ilspy(), "-l", LIST_MODES[symbol_kind], str(assembly)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, shell=False, check=False)
    except subprocess.TimeoutExpired as exc:
        raise UnityRecoveryError(f"ILSpy symbol listing timed out after {timeout}s.") from exc
    if result.returncode:
        raise UnityRecoveryError(f"ILSpy symbol listing failed (exit {result.returncode}): {(result.stderr or result.stdout)[-3000:]}")
    all_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    needle = query.strip().casefold()
    filtered = [line for line in all_lines if not needle or needle in line.casefold()]
    return {
        "assembly_path": str(assembly), "symbol_kind": symbol_kind,
        "query": query, "total_reported": len(all_lines),
        "match_count": len(filtered), "symbols": filtered[:limit],
        "truncated": len(filtered) > limit, "tool": "ilspycmd",
    }


def search_unity_source(
    workspace_root: str,
    query: str,
    source_path: str = "recovery/decompiled",
    max_results: int = 100,
    case_sensitive: bool = False,
    regex: bool = False,
    context_lines: int = 2,
) -> dict[str, Any]:
    """Search decompiled C# files with bounded output and optional nearby context."""
    root = _directory(workspace_root, "workspace")
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise UnityRecoveryError("A non-empty search query of at most 1000 characters is required.")
    max_results = max(1, min(int(max_results), MAX_RESULTS))
    context_lines = max(0, min(int(context_lines), 5))
    source = _safe_workspace_path(root, source_path)
    if not source.is_dir():
        raise UnityRecoveryError(f"Decompiled source directory not found: {source}. Run export_unity_assembly first.")
    try:
        matcher = re.compile(query if regex else re.escape(query), 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        raise UnityRecoveryError(f"Invalid search regex: {exc}") from exc
    matches = []
    scanned_files = 0
    scanned_bytes = 0
    truncated_scan = False
    for current, dirs, files in __import__("os").walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not (current_path / d).is_symlink())
        for name in sorted(files):
            path = current_path / name
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {".cs", ".json", ".txt"}:
                continue
            try:
                size = path.stat().st_size
                if scanned_bytes + size > MAX_SOURCE_BYTES:
                    truncated_scan = True
                    break
                content = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            scanned_files += 1
            scanned_bytes += size
            for index, line in enumerate(content):
                if matcher.search(line):
                    lo = max(0, index - context_lines)
                    hi = min(len(content), index + context_lines + 1)
                    matches.append({
                        "path": path.relative_to(root).as_posix(),
                        "line": index + 1, "text": line[:1000],
                        "context_start_line": lo + 1,
                        "context": [{"line": j + 1, "text": content[j][:1000]} for j in range(lo, hi)],
                    })
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break
        if truncated_scan or len(matches) >= max_results:
            break
    return {
        "query": query, "source_path": str(source),
        "scanned_files": scanned_files, "scanned_bytes": scanned_bytes,
        "match_count": len(matches), "matches": matches,
        "truncated": truncated_scan or len(matches) >= max_results,
        "limitations": ["Search covers exported source files only; it is not a complete IL-level call graph."],
    }
