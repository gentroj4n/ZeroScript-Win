# SPDX-License-Identifier: GPL-3.0-or-later
"""Loopback-only static browser-port server lifecycle."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from workspace_agent.unity_recovery import UnityRecoveryError, _directory, _safe_workspace_path

_ACTIVE: dict[str, subprocess.Popen] = {}


def _state_path(root: Path) -> Path:
    return root / ".arcscript-browser-server.json"


def _load_state(root: Path) -> dict[str, Any] | None:
    path = _state_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_state(root: Path, data: dict[str, Any]) -> None:
    path = _state_path(root)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _pid_alive(pid: int) -> bool:
    if pid < 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def browser_server_status(workspace_root: str) -> dict[str, Any]:
    root = _directory(workspace_root, "workspace")
    state = _load_state(root)
    if not state:
        return {"running": False, "message": "No ArcScript-managed browser server is recorded for this workspace."}
    if state.get("stopped_unix"):
        return {**state, "running": False, "tracked_by_current_bridge": False}
    pid = int(state.get("pid", 0))
    job_id = str(state.get("job_id", ""))
    process = _ACTIVE.get(job_id)
    if process is not None:
        code = process.poll()
        alive = code is None
        if not alive:
            state["status"] = "exited"
            state["exit_code"] = code
            state["finished_unix"] = int(time.time())
            _ACTIVE.pop(job_id, None)
            _write_state(root, state)
    else:
        alive = _pid_alive(pid)
        if not alive and state.get("status") in {"running", "starting"}:
            state["status"] = "interrupted"
            state["message"] = "The recorded server process is no longer running."
            _write_state(root, state)
    return {**state, "running": alive, "tracked_by_current_bridge": process is not None and alive}


def start_browser_server(workspace_root: str, port_path: str = "browser-port", port: int = 4173) -> dict[str, Any]:
    root = _directory(workspace_root, "workspace")
    target = _safe_workspace_path(root, port_path)
    if not target.is_dir() or not (target / "index.html").is_file():
        raise UnityRecoveryError(f"Static browser port needs an existing index.html: {target}")
    port = int(port)
    if port < 1024 or port > 65535:
        raise UnityRecoveryError("port must be between 1024 and 65535.")
    existing = browser_server_status(str(root))
    if existing.get("running"):
        return {"status": "already_running", **existing, "url": f"http://127.0.0.1:{existing['port']}/"}
    # A loopback bind prevents accidental LAN exposure.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise UnityRecoveryError(f"Port {port} is unavailable on 127.0.0.1: {exc}") from exc
    log_path = _safe_workspace_path(root, "recovery/browser-server.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "ab", buffering=0)
    command = [
        sys.executable, "-m", "http.server", str(port),
        "--bind", "127.0.0.1", "--directory", str(target),
    ]
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
        raise UnityRecoveryError(f"Could not start the static browser server: {exc}") from exc
    log_file.close()
    job_id = "browser-server-" + uuid.uuid4().hex[:12]
    state = {
        "job_id": job_id,
        "pid": process.pid,
        "port": port,
        "url": f"http://127.0.0.1:{port}/",
        "workspace": str(root),
        "served_directory": str(target),
        "log_path": str(log_path),
        "command": command,
        "started_unix": int(time.time()),
        "bind_address": "127.0.0.1",
    }
    _ACTIVE[job_id] = process
    _write_state(root, state)
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _ACTIVE.pop(job_id, None)
            raise UnityRecoveryError(f"Static server exited during startup; inspect {log_path}.")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return {"status": "started", "running": True, **state}
        except OSError:
            time.sleep(0.1)
    return {"status": "starting", "running": process.poll() is None, **state, "message": "Server process started but did not answer within 4 seconds."}


def stop_browser_server(workspace_root: str) -> dict[str, Any]:
    root = _directory(workspace_root, "workspace")
    state = _load_state(root)
    if not state:
        return {"status": "not_found", "running": False}
    job_id = str(state.get("job_id", ""))
    process = _ACTIVE.get(job_id)
    if process is None:
        return {
            "status": "not_tracked",
            "running": _pid_alive(int(state.get("pid", 0))),
            "job_id": job_id,
            "message": "This bridge did not start the recorded process, so it will not terminate a PID it cannot verify. Restart the bridge-managed server only after confirming the old process has exited.",
        }
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    _ACTIVE.pop(job_id, None)
    state["stopped_unix"] = int(time.time())
    state["running"] = False
    _write_state(root, state)
    return {"status": "stopped", "running": False, "job_id": job_id, "pid": process.pid, "port": state.get("port")}
