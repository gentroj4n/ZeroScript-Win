# SPDX-License-Identifier: GPL-3.0-or-later
"""Durable status records and cancellable AssetRipper CLI export jobs."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from workspace_agent.unity_recovery import UnityRecoveryError, _directory, _safe_workspace_path

_ACTIVE: dict[str, subprocess.Popen] = {}
MODES = {"Unity", "Primary"}
SCRIPT_LEVELS = {"Level0", "Level1", "Level2"}


def _state_file(root: Path, job_id: str) -> Path:
    return _safe_workspace_path(root, f"recovery/jobs/{job_id}.json")


def _save_state(root: Path, state: dict[str, Any]) -> None:
    path = _state_file(root, str(state["job_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _read_state(root: Path, job_id: str) -> dict[str, Any]:
    if not isinstance(job_id, str) or not re.fullmatch(r"assetripper-[a-f0-9]{12}", job_id):
        raise UnityRecoveryError("Unknown or invalid AssetRipper job ID.")
    path = _state_file(root, job_id)
    if not path.is_file():
        raise UnityRecoveryError(f"AssetRipper job was not found: {job_id}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnityRecoveryError(f"Could not read job state: {exc}") from exc
    if not isinstance(state, dict) or state.get("job_id") != job_id:
        raise UnityRecoveryError("Job state failed validation.")
    return state


def _refresh(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    process = _ACTIVE.get(state["job_id"])
    if process is not None:
        code = process.poll()
        if code is None and int(state.get("deadline_unix", 0)) and time.time() >= int(state["deadline_unix"]):
            state["timeout_requested"] = True
            state["status"] = "cancelling"
            _save_state(root, state)
            process.terminate()
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                code = process.wait(timeout=5)
            state["message"] = "AssetRipper exceeded its configured time limit and was terminated."
        if code is None:
            state["status"] = "running"
        else:
            if state.get("cancel_requested"):
                state["status"] = "cancelled"
            elif state.get("timeout_requested"):
                state["status"] = "failed"
            elif code == 0:
                output = Path(str(state.get("output_path", "")))
                if output.is_dir() and any(output.iterdir()):
                    state["status"] = "succeeded"
                else:
                    state["status"] = "failed"
                    state["message"] = "AssetRipper exited successfully but the output directory is empty."
            else:
                state["status"] = "failed"
            state["exit_code"] = code
            state["finished_unix"] = int(time.time())
            _ACTIVE.pop(state["job_id"], None)
        _save_state(root, state)
    elif state.get("status") in {"queued", "running", "cancelling"}:
        # Across bridge restarts, report liveness but do not claim the child is
        # controllable. We never terminate an untracked/reused PID.
        try:
            os.kill(int(state.get("pid", 0)), 0)
            alive = True
        except (OSError, ValueError):
            alive = False
        if not alive:
            state["status"] = "interrupted"
            state["finished_unix"] = int(time.time())
            state["message"] = "Bridge restarted or process exited; exit code is unavailable. Inspect the log before retrying."
            _save_state(root, state)
        else:
            state["status"] = "untracked_running"
    return state


def start_assetripper_export(
    workspace_root: str,
    executable_path: str,
    input_path: str,
    output_path: str = "recovery/assetripper-export",
    mode: str = "Primary",
    script_content_level: str = "Level1",
    timeout_minutes: int = 240,
) -> dict[str, Any]:
    """Start an AssetRipper CLI-compatible build as a bounded background job."""
    root = _directory(workspace_root, "workspace")
    executable = Path(executable_path).expanduser().resolve()
    source = _directory(input_path, "Unity input")
    if not executable.is_file():
        raise UnityRecoveryError(f"AssetRipper executable not found: {executable}")
    if "assetripper" not in executable.name.casefold():
        raise UnityRecoveryError("executable_path must identify an AssetRipper executable, not an arbitrary program.")
    if mode not in MODES:
        raise UnityRecoveryError(f"mode must be one of: {', '.join(sorted(MODES))}.")
    if script_content_level not in SCRIPT_LEVELS:
        raise UnityRecoveryError(f"script_content_level must be one of: {', '.join(sorted(SCRIPT_LEVELS))}.")
    timeout_minutes = max(1, min(int(timeout_minutes), 24 * 60))
    output = _safe_workspace_path(root, output_path)
    if output == source or source in output.parents:
        raise UnityRecoveryError("Export output cannot be inside the original Unity input directory.")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise UnityRecoveryError(f"Refusing to overwrite a non-empty export path: {output}")
    output.mkdir(parents=True, exist_ok=True)
    job_id = "assetripper-" + uuid.uuid4().hex[:12]
    log_path = _safe_workspace_path(root, f"recovery/jobs/{job_id}.stdout.log")
    asset_log_path = _safe_workspace_path(root, f"recovery/jobs/{job_id}.assetripper.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(executable), "--cli", "--input", str(source), "--output", str(output),
        "--mode", mode, "--script-content-level", script_content_level,
        "--log-path", str(asset_log_path),
    ]
    log_file = open(log_path, "ab", buffering=0)
    kwargs: dict[str, Any] = {
        "cwd": str(root), "stdin": subprocess.DEVNULL,
        "stdout": log_file, "stderr": subprocess.STDOUT, "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        log_file.close()
        raise UnityRecoveryError(f"Could not start AssetRipper: {exc}") from exc
    log_file.close()
    state = {
        "job_id": job_id,
        "kind": "assetripper_export",
        "status": "running",
        "pid": process.pid,
        "command": command,
        "input_path": str(source),
        "output_path": str(output),
        "mode": mode,
        "script_content_level": script_content_level,
        "timeout_minutes": timeout_minutes,
        "started_unix": int(time.time()),
        "deadline_unix": int(time.time()) + timeout_minutes * 60,
        "log_path": str(log_path),
        "assetripper_log_path": str(asset_log_path),
        "cancel_requested": False,
        "bridge_instance_controllable": True,
    }
    _ACTIVE[job_id] = process
    _save_state(root, state)
    return {
        "status": "running",
        "job_id": job_id,
        "pid": process.pid,
        "input_path": str(source),
        "output_path": str(output),
        "log_path": str(log_path),
        "assetripper_log_path": str(asset_log_path),
        "mode": mode,
        "command": command,
        "timeout_minutes": timeout_minutes,
        "message": "Export started asynchronously. Poll assetripper_job_status; inspect the log before retrying a failed or interrupted job.",
    }


def assetripper_job_status(workspace_root: str, job_id: str) -> dict[str, Any]:
    root = _directory(workspace_root, "workspace")
    state = _refresh(root, _read_state(root, job_id))
    return state


def cancel_assetripper_job(workspace_root: str, job_id: str) -> dict[str, Any]:
    root = _directory(workspace_root, "workspace")
    state = _read_state(root, job_id)
    process = _ACTIVE.get(job_id)
    if process is None:
        state = _refresh(root, state)
        return {
            **state,
            "cancelled": False,
            "message": "This bridge cannot safely cancel an untracked process. Do not terminate a PID unless its command line has been independently verified.",
        }
    if process.poll() is not None:
        return _refresh(root, state)
    state["cancel_requested"] = True
    state["status"] = "cancelling"
    state["cancel_requested_unix"] = int(time.time())
    _save_state(root, state)
    process.terminate()
    try:
        code = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        code = process.wait(timeout=5)
    state["exit_code"] = code
    state["status"] = "cancelled"
    state["finished_unix"] = int(time.time())
    _ACTIVE.pop(job_id, None)
    _save_state(root, state)
    return {**state, "cancelled": True}
