# SPDX-License-Identifier: GPL-3.0-or-later
"""Generic background job manager for the workspace layer.

One bounded primitive for long-running processes: dev servers, watchers,
test runners, build jobs. The specialized wrappers in browser_server.py and
assetripper_jobs.py keep their domain-specific validation; this module is
the general case they both anticipated.

Design boundaries, matching the rest of workspace_agent:
  - argv lists, never a shell string. shell=False always.
  - cwd must resolve inside the selected workspace.
  - Bounded timeouts and bounded log tail reads.
  - Durable per-workspace state under .zeroscript/jobs/ so status survives
    a bridge restart, but a restart never claims control over a PID this
    bridge instance did not spawn.
  - A module-level _ACTIVE registry maps job_id -> Popen. It is preserved
    across hot-reload by bridge._reload_workspace_modules, so a code reload
    does not silently orphan a running dev server.

Tools:
  - workspace_job_start    launch an argv command as a background job
  - workspace_job_status   poll one job by id
  - workspace_job_logs     bounded tail of a job's combined stdout+stderr
  - workspace_job_list     list every job recorded for this workspace
  - workspace_job_stop     terminate one job started by THIS bridge instance
  - workspace_wait_port    block until a loopback TCP port answers
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from workspace_agent.unity_recovery import (
    UnityRecoveryError,
    _directory,
    _safe_workspace_path,
)

_ACTIVE: dict[str, subprocess.Popen] = {}

JOB_ID_RE = re.compile(r"^job-[a-f0-9]{12}$")
MAX_CONCURRENT_JOBS = 10
DEFAULT_TIMEOUT_MINUTES = 60
MAX_TIMEOUT_MINUTES = 24 * 60
DEFAULT_LOG_TAIL_BYTES = 64 * 1024
MAX_LOG_TAIL_BYTES = 1024 * 1024
MIN_LOG_TAIL_BYTES = 1024


def _jobs_dir(root: Path) -> Path:
    path = _safe_workspace_path(root, ".zeroscript/jobs")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_file(root: Path, job_id: str) -> Path:
    return _safe_workspace_path(root, f".zeroscript/jobs/{job_id}.json")


def _log_file(root: Path, job_id: str) -> Path:
    return _safe_workspace_path(root, f".zeroscript/jobs/{job_id}.log")


def _save_state(root: Path, state: dict[str, Any]) -> None:
    path = _state_file(root, str(state["job_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _read_state(root: Path, job_id: str) -> dict[str, Any]:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise UnityRecoveryError("Unknown or invalid job ID (expected job-XXXXXXXXXXXX).")
    path = _state_file(root, job_id)
    if not path.is_file():
        raise UnityRecoveryError(f"Job was not found: {job_id}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnityRecoveryError(f"Could not read job state: {exc}") from exc
    if not isinstance(state, dict) or state.get("job_id") != job_id:
        raise UnityRecoveryError("Job state failed validation.")
    return state


def _pid_alive(pid: int) -> bool:
    if pid < 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _refresh(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Settle a job's status against its live process (or lack of one)."""
    job_id = state.get("job_id", "")
    process = _ACTIVE.get(job_id)
    now = int(time.time())
    if process is not None:
        code = process.poll()
        # Enforce the deadline on a still-running process.
        if code is None and int(state.get("deadline_unix", 0)) and now >= int(state["deadline_unix"]):
            state["timeout_requested"] = True
            state["status"] = "cancelling"
            _save_state(root, state)
            process.terminate()
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                code = process.wait(timeout=5)
            state["message"] = "Job exceeded its configured time limit and was terminated."
        if code is None:
            state["status"] = "running"
        else:
            if state.get("cancel_requested"):
                state["status"] = "cancelled"
            elif state.get("timeout_requested"):
                state["status"] = "failed"
            elif code == 0:
                state["status"] = "succeeded"
            else:
                state["status"] = "failed"
            state["exit_code"] = code
            state["finished_unix"] = now
            _ACTIVE.pop(job_id, None)
        _save_state(root, state)
    elif state.get("status") in {"starting", "running", "cancelling"}:
        # No live handle in this process. Do NOT terminate a PID we cannot
        # verify: it may belong to a different bridge instance, or the OS may
        # have reused it. Report liveness honestly and let the user decide.
        if _pid_alive(int(state.get("pid", 0))):
            state["status"] = "untracked_running"
            state["message"] = (
                "This bridge instance does not hold a live handle to the job. "
                "The PID appears alive, but stop is refused because the process "
                "cannot be verified as the one recorded here."
            )
        else:
            state["status"] = "interrupted"
            state["finished_unix"] = now
            state["message"] = (
                "The recorded process is no longer running and its exit code is "
                "unknown (bridge restart or crash). Inspect the log before retrying."
            )
        _save_state(root, state)
    return state


def _validate_command(command: Any) -> list[str]:
    if not isinstance(command, list) or not command:
        raise UnityRecoveryError("command must be a non-empty array of strings.")
    if len(command) > 100:
        raise UnityRecoveryError("command is limited to 100 argv entries.")
    out: list[str] = []
    for index, item in enumerate(command):
        if not isinstance(item, str) or not item:
            raise UnityRecoveryError(f"command[{index}] must be a non-empty string.")
        if "\x00" in item:
            raise UnityRecoveryError(f"command[{index}] contains a NUL byte.")
        out.append(item)
    return out


def _validate_env(env: Any) -> dict[str, str]:
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise UnityRecoveryError("env must be an object of string values.")
    out: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not key or "=" in key:
            raise UnityRecoveryError(f"Invalid env key: {key!r}")
        if not isinstance(value, str):
            raise UnityRecoveryError(f"env value for {key!r} must be a string.")
        out[key] = value
    return out


def _active_count() -> int:
    return sum(1 for p in _ACTIVE.values() if p.poll() is None)


def start_job(
    workspace_root: str,
    command: Any,
    cwd: str = ".",
    label: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    timeout_minutes: Optional[int] = None,
) -> dict[str, Any]:
    """Start an argv command as a background job scoped to this workspace."""
    root = _directory(workspace_root, "workspace")
    argv = _validate_command(command)
    env_overrides = _validate_env(env)
    work_dir = _safe_workspace_path(root, cwd or ".")
    if not work_dir.is_dir():
        raise UnityRecoveryError(f"cwd is not an existing directory inside the workspace: {work_dir}")

    try:
        timeout_minutes = int(timeout_minutes) if timeout_minutes is not None else DEFAULT_TIMEOUT_MINUTES
    except (TypeError, ValueError):
        timeout_minutes = DEFAULT_TIMEOUT_MINUTES
    timeout_minutes = max(1, min(timeout_minutes, MAX_TIMEOUT_MINUTES))

    if _active_count() >= MAX_CONCURRENT_JOBS:
        raise UnityRecoveryError(
            f"{MAX_CONCURRENT_JOBS} jobs are already running in this workspace. "
            "Stop one with workspace_job_stop before starting another."
        )

    _jobs_dir(root)  # ensure the directory exists before opening the log file
    job_id = "job-" + uuid.uuid4().hex[:12]
    log_path = _log_file(root, job_id)

    merged_env = dict(os.environ)
    merged_env.update(env_overrides)

    log_handle = open(log_path, "ab", buffering=0)
    kwargs: dict[str, Any] = {
        "cwd": str(work_dir),
        "env": merged_env,
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(argv, **kwargs)
    except FileNotFoundError as exc:
        log_handle.close()
        try:
            log_path.unlink()
        except OSError:
            pass
        raise UnityRecoveryError(
            f"Command not found: '{argv[0]}' - is it installed and on PATH? ({exc})"
        ) from exc
    except OSError as exc:
        log_handle.close()
        try:
            log_path.unlink()
        except OSError:
            pass
        raise UnityRecoveryError(f"Could not launch '{argv[0]}': {exc}") from exc
    finally:
        try:
            log_handle.close()
        except Exception:
            pass

    state = {
        "job_id": job_id,
        "kind": "workspace_job",
        "status": "starting",
        "label": (label or " ".join(argv[:2]))[:120],
        "command": argv,
        "cwd": str(work_dir),
        "pid": process.pid,
        "started_unix": int(time.time()),
        "deadline_unix": int(time.time()) + timeout_minutes * 60,
        "timeout_minutes": timeout_minutes,
        "log_path": str(log_path),
        "env_overrides": env_overrides,
        "cancel_requested": False,
        "bridge_instance_controllable": True,
    }
    _ACTIVE[job_id] = process
    _save_state(root, state)

    # Give the process a brief moment to prove it launches. Many legitimate
    # short-lived jobs (a one-shot script, a quick build check) exit 0 inside
    # this window; that is a success, not a start failure. Only a NON-ZERO exit
    # here is treated as a launch error worth raising, because it almost
    # always means a wrong binary, a bad argument, or a missing dependency.
    time.sleep(0.15)
    code = process.poll()
    if code is not None:
        _ACTIVE.pop(job_id, None)
        state["exit_code"] = code
        state["finished_unix"] = int(time.time())
        if code == 0:
            state["status"] = "succeeded"
            _save_state(root, state)
            return {
                "status": "succeeded",
                "job_id": job_id,
                "pid": process.pid,
                "label": state["label"],
                "command": argv,
                "cwd": str(work_dir),
                "log_path": str(log_path),
                "exit_code": 0,
                "message": (
                    "Job completed within 150ms of launch (exit 0). It is no "
                    "longer running; inspect workspace_job_logs for its output."
                ),
            }
        state["status"] = "failed"
        _save_state(root, state)
        raise UnityRecoveryError(
            f"Job exited immediately with code {code}. Inspect {log_path} for output."
        )
    state["status"] = "running"
    _save_state(root, state)
    return {
        "status": "running",
        "job_id": job_id,
        "pid": process.pid,
        "label": state["label"],
        "command": argv,
        "cwd": str(work_dir),
        "log_path": str(log_path),
        "timeout_minutes": timeout_minutes,
        "message": (
            "Background job started. Use workspace_job_status to poll, "
            "workspace_job_logs to tail, workspace_job_stop to terminate."
        ),
    }


def job_status(workspace_root: str, job_id: str) -> dict[str, Any]:
    root = _directory(workspace_root, "workspace")
    return _refresh(root, _read_state(root, job_id))


def job_list(workspace_root: str) -> dict[str, Any]:
    root = _directory(workspace_root, "workspace")
    jobs_dir = _jobs_dir(root)
    records: list[dict[str, Any]] = []
    for path in sorted(jobs_dir.glob("job-*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict) or not JOB_ID_RE.fullmatch(str(state.get("job_id", ""))):
            continue
        records.append(_refresh(root, state))
    records.sort(key=lambda r: int(r.get("started_unix", 0)), reverse=True)
    active = sum(1 for r in records if r.get("status") in {"starting", "running", "cancelling"})
    return {
        "count": len(records),
        "active": active,
        "max_concurrent": MAX_CONCURRENT_JOBS,
        "jobs": records,
    }


def _read_log_tail(path: Path, max_bytes: int) -> dict[str, Any]:
    if not path.is_file():
        return {
            "exists": False,
            "content": "",
            "body_encoding": "utf-8",
            "total_bytes": 0,
            "returned_bytes": 0,
            "truncated_start": False,
        }
    with open(path, "rb") as handle:
        handle.seek(0, 2)
        total = handle.tell()
        start = max(0, total - max_bytes)
        handle.seek(start)
        raw = handle.read()
    truncated_start = start > 0
    if truncated_start:
        # Drop the leading partial line so the first visible line is complete.
        newline = raw.find(b"\n")
        if newline >= 0:
            raw = raw[newline + 1:]
    try:
        content = raw.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="replace")
        encoding = "utf-8-replace"
    return {
        "exists": True,
        "content": content,
        "body_encoding": encoding,
        "total_bytes": total,
        "returned_bytes": len(raw),
        "truncated_start": truncated_start,
    }


def job_logs(
    workspace_root: str,
    job_id: str,
    max_bytes: int = DEFAULT_LOG_TAIL_BYTES,
) -> dict[str, Any]:
    root = _directory(workspace_root, "workspace")
    state = _refresh(root, _read_state(root, job_id))
    try:
        max_bytes = int(max_bytes)
    except (TypeError, ValueError):
        max_bytes = DEFAULT_LOG_TAIL_BYTES
    max_bytes = max(MIN_LOG_TAIL_BYTES, min(max_bytes, MAX_LOG_TAIL_BYTES))
    log_path = Path(str(state.get("log_path", "")))
    tail = _read_log_tail(log_path, max_bytes)
    return {
        "job_id": job_id,
        "status": state.get("status"),
        "label": state.get("label"),
        "pid": state.get("pid"),
        "log_path": str(log_path),
        **tail,
    }


def stop_job(workspace_root: str, job_id: str) -> dict[str, Any]:
    root = _directory(workspace_root, "workspace")
    state = _read_state(root, job_id)
    process = _ACTIVE.get(job_id)
    if process is None:
        state = _refresh(root, state)
        return {
            **state,
            "cancelled": False,
            "message": (
                "This bridge instance does not hold a live handle to the job. "
                "Refusing to terminate an unverified PID. If you are certain the "
                "process is still running and belongs to you, stop it manually."
            ),
        }
    if process.poll() is not None:
        return {**_refresh(root, state), "cancelled": False}
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


def wait_port(
    workspace_root: str,
    port: int,
    host: str = "127.0.0.1",
    timeout_seconds: float = 30.0,
    interval_ms: int = 200,
) -> dict[str, Any]:
    """Block until a TCP port accepts a connection, or the timeout elapses.

    TCP connect only; no data is sent. Restricted to loopback by default; the
    host can be overridden but the tool description cautions against using it
    as a network scanner.
    """
    _directory(workspace_root, "workspace")  # validates the workspace exists
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise UnityRecoveryError("port must be an integer.")
    if port < 1 or port > 65535:
        raise UnityRecoveryError("port must be between 1 and 65535.")
    try:
        timeout_seconds = float(timeout_seconds)
    except (TypeError, ValueError):
        timeout_seconds = 30.0
    timeout_seconds = max(0.1, min(timeout_seconds, 300.0))
    try:
        interval_ms = int(interval_ms)
    except (TypeError, ValueError):
        interval_ms = 200
    interval_ms = max(50, min(interval_ms, 5000))
    if not isinstance(host, str) or not host.strip():
        host = "127.0.0.1"

    start = time.monotonic()
    deadline = start + timeout_seconds
    attempts = 0
    last_error = ""
    while time.monotonic() < deadline:
        attempts += 1
        try:
            with socket.create_connection((host, port), timeout=0.5):
                elapsed = int((time.monotonic() - start) * 1000)
                return {
                    "ready": True,
                    "host": host,
                    "port": port,
                    "attempts": attempts,
                    "elapsed_ms": elapsed,
                }
        except OSError as exc:
            last_error = str(exc)
            time.sleep(interval_ms / 1000.0)
    elapsed = int((time.monotonic() - start) * 1000)
    return {
        "ready": False,
        "host": host,
        "port": port,
        "attempts": attempts,
        "elapsed_ms": elapsed,
        "last_error": last_error,
        "message": f"{host}:{port} did not accept a connection within {timeout_seconds:.1f}s.",
    }
