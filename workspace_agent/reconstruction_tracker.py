# SPDX-License-Identifier: GPL-3.0-or-later
"""Evidence-linked inventory for browser restoration feature parity."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from workspace_agent.unity_recovery import UnityRecoveryError, _directory, _safe_workspace_path

TRACKER_PATH = "recovery/reconstruction/features.json"
FEATURE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
STATUSES = {"planned", "investigating", "implemented", "blocked"}


def _load(root: Path) -> dict[str, Any]:
    path = _safe_workspace_path(root, TRACKER_PATH)
    if not path.exists():
        return {"schema_version": 1, "created_unix": int(time.time()), "updated_unix": int(time.time()), "features": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnityRecoveryError(f"Reconstruction tracker is unreadable; refusing to overwrite it: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("features"), dict):
        raise UnityRecoveryError("Reconstruction tracker schema is invalid; refusing to overwrite it.")
    return data


def _save(root: Path, data: dict[str, Any]) -> Path:
    path = _safe_workspace_path(root, TRACKER_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    return path


def update_reconstruction_feature(
    workspace_root: str,
    feature_id: str,
    name: str,
    status: str = "planned",
    notes: str = "",
    source_evidence: list[dict[str, str]] | None = None,
    asset_evidence: list[str] | None = None,
    implementation_files: list[str] | None = None,
    test_files: list[str] | None = None,
) -> dict[str, Any]:
    """Create/update one feature record; evidence paths must resolve inside workspace."""
    root = _directory(workspace_root, "workspace")
    if not isinstance(feature_id, str) or not FEATURE_ID.fullmatch(feature_id):
        raise UnityRecoveryError("feature_id must use lowercase letters, digits, dot, underscore, or hyphen (max 80 chars).")
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise UnityRecoveryError("A feature name of 1-200 characters is required.")
    if status not in STATUSES:
        raise UnityRecoveryError("status must be one of: " + ", ".join(sorted(STATUSES)))
    if not isinstance(notes, str) or len(notes) > 4000:
        raise UnityRecoveryError("notes must be text of at most 4000 characters.")

    def validate_path(value: str, field: str, must_exist: bool = True) -> str:
        if not isinstance(value, str) or not value.strip():
            raise UnityRecoveryError(f"{field} contains an empty path.")
        candidate = _safe_workspace_path(root, value)
        if candidate.is_symlink():
            raise UnityRecoveryError(f"{field} cannot reference a symbolic link: {value}")
        if must_exist and not candidate.is_file():
            raise UnityRecoveryError(f"{field} path does not exist as a file: {value}")
        return candidate.relative_to(root).as_posix()

    source = []
    for item in source_evidence or []:
        if not isinstance(item, dict):
            raise UnityRecoveryError("Each source_evidence entry must contain path and locator.")
        path = validate_path(item.get("path", ""), "source_evidence")
        locator = item.get("locator", "")
        if not isinstance(locator, str) or not locator.strip() or len(locator) > 500:
            raise UnityRecoveryError("Each source evidence locator must identify a symbol, method, or line range.")
        source.append({"path": path, "locator": locator.strip()})
    assets = [validate_path(item, "asset_evidence") for item in (asset_evidence or [])]
    implementation = [validate_path(item, "implementation_files") for item in (implementation_files or [])]
    tests = [validate_path(item, "test_files") for item in (test_files or [])]

    data = _load(root)
    prior = data["features"].get(feature_id, {})
    feature = {
        "feature_id": feature_id,
        "name": name.strip(),
        "status": status,
        "notes": notes.strip(),
        "source_evidence": source if source_evidence is not None else prior.get("source_evidence", []),
        "asset_evidence": assets if asset_evidence is not None else prior.get("asset_evidence", []),
        "implementation_files": implementation if implementation_files is not None else prior.get("implementation_files", []),
        "test_files": tests if test_files is not None else prior.get("test_files", []),
        "created_unix": prior.get("created_unix", int(time.time())),
        "updated_unix": int(time.time()),
    }
    data["features"][feature_id] = feature
    data["updated_unix"] = int(time.time())
    path = _save(root, data)
    return {"status": "saved", "tracker_path": path.relative_to(root).as_posix(), "feature": feature}


def report_reconstruction_readiness(workspace_root: str, feature_id: str = "") -> dict[str, Any]:
    """Report missing evidence; never marks a feature verified merely from metadata."""
    root = _directory(workspace_root, "workspace")
    data = _load(root)
    features = data["features"]
    if feature_id:
        if feature_id not in features:
            raise UnityRecoveryError(f"Unknown reconstruction feature: {feature_id}")
        selected = {feature_id: features[feature_id]}
    else:
        selected = features
    reports = []
    for key, feature in sorted(selected.items()):
        missing = []
        if not feature.get("source_evidence"):
            missing.append("source evidence (decompiled behavior with symbol/method/line locator)")
        if not feature.get("asset_evidence"):
            missing.append("asset evidence (cataloged/extracted resource file)")
        if not feature.get("implementation_files"):
            missing.append("browser implementation file")
        if not feature.get("test_files"):
            missing.append("regression test file")
        if feature.get("status") == "blocked":
            missing.append("resolve documented blocker")
        reports.append({
            "feature_id": key,
            "name": feature.get("name", key),
            "status": feature.get("status", "planned"),
            "missing_evidence": missing,
            "ready_for_review": not missing and feature.get("status") == "implemented",
            "source_evidence": feature.get("source_evidence", []),
            "asset_evidence": feature.get("asset_evidence", []),
            "implementation_files": feature.get("implementation_files", []),
            "test_files": feature.get("test_files", []),
            "notes": feature.get("notes", ""),
        })
    return {
        "tracker_path": TRACKER_PATH,
        "feature_count": len(reports),
        "ready_for_review_count": sum(1 for item in reports if item["ready_for_review"]),
        "features": reports,
        "limitations": [
            "Readiness means required evidence paths are recorded; it does not execute tests or prove behavioral parity.",
            "The tracker never assigns a verified/pass status automatically.",
        ],
    }
