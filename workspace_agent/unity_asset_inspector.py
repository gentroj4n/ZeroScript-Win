# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded Unity serialized-file inspection via optional UnityPy.

This is intentionally a targeted inspector, not a whole-installation scanner.
Large resources.assets files are rejected before UnityPy is loaded to avoid
accidental multi-gigabyte allocations.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from workspace_agent.unity_recovery import UnityRecoveryError, _directory, _safe_workspace_path

DEFAULT_MAX_FILE_BYTES = 256 * 1024 * 1024
HARD_MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_OBJECTS = 5000
SUPPORTED_SUFFIXES = {".assets", ".asset", ".bundle", ".unity3d", ".sharedasset", ".ress"}


def inspect_unity_serialized_file(
    install_path: str,
    asset_path: str,
    max_file_mb: int = 256,
    max_objects: int = 1000,
) -> dict[str, Any]:
    """Inspect objects in one supported Unity serialized file without modifying it."""
    root = _directory(install_path, "Unity installation")
    if not isinstance(asset_path, str) or not asset_path.strip():
        raise UnityRecoveryError("asset_path must be a path relative to the Unity installation.")
    candidate = Path(asset_path)
    if candidate.is_absolute():
        raise UnityRecoveryError("asset_path must be relative to the Unity installation.")
    path = _safe_workspace_path(root, asset_path)
    if path.is_symlink() or not path.is_file():
        raise UnityRecoveryError(f"Unity asset file not found or is a symbolic link: {asset_path}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise UnityRecoveryError("Unsupported file extension. Expected a Unity serialized asset or bundle file.")
    max_file_mb = max(1, min(int(max_file_mb), HARD_MAX_FILE_BYTES // (1024 * 1024)))
    max_bytes = max_file_mb * 1024 * 1024
    size = path.stat().st_size
    if size > max_bytes:
        raise UnityRecoveryError(
            f"Refusing to load {size} bytes ({size / 1024 / 1024:.1f} MiB); "
            f"the configured limit is {max_file_mb} MiB. Large files such as resources.assets "
            "must not be loaded as a single inspection job. Select a smaller file or bundle."
        )
    try:
        import UnityPy  # type: ignore
    except ImportError as exc:
        raise UnityRecoveryError(
            "UnityPy is not installed in the bridge Python environment. Install it deliberately "
            "in that environment, then retry; no software is installed automatically."
        ) from exc

    max_objects = max(1, min(int(max_objects), MAX_OBJECTS))
    try:
        environment = UnityPy.load(str(path))
        objects = environment.objects
        total = len(objects)
    except Exception as exc:
        raise UnityRecoveryError(f"UnityPy could not parse the selected file: {type(exc).__name__}: {exc}") from exc

    counts: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    decode_errors = 0
    for obj in objects[:max_objects]:
        try:
            type_name = str(getattr(getattr(obj, "type", None), "name", None) or "Unknown")
            counts[type_name] += 1
            record: dict[str, Any] = {
                "path_id": int(getattr(obj, "path_id", 0)),
                "type": type_name,
            }
            try:
                data = obj.read()
                name = getattr(data, "m_Name", None) or getattr(data, "name", None)
                if isinstance(name, str) and name:
                    record["name"] = name[:500]
            except Exception as exc:
                decode_errors += 1
                record["decode_error"] = type(exc).__name__
            records.append(record)
        except Exception:
            decode_errors += 1

    return {
        "status": "complete" if total <= max_objects else "truncated",
        "install_path": str(root),
        "asset_path": path.relative_to(root).as_posix(),
        "size_bytes": size,
        "parser": "UnityPy",
        "object_count": total,
        "objects_returned": len(records),
        "type_counts_returned": dict(sorted(counts.items())),
        "objects": records,
        "decode_error_count": decode_errors,
        "truncated": total > max_objects,
        "limitations": [
            "This reports objects exposed by UnityPy for this one file; it does not infer complete cross-file dependencies.",
            "The selected file is read-only. UnityPy is optional and is not installed automatically.",
        ],
    }
