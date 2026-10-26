# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent handoff and single-owner control for the ZeroScript agent runtimes.

The bridge is the authority for this record. The CLI never edits this file directly;
it uses bridge messages so all clients see ownership transitions.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional


_ACTIVE_STATES = {"active", "stop_requested"}
_TERMINAL_STATES = {"completed", "failed", "cancelled", "blocked", "returned_to_browser"}
_MAX_GOAL = 12000
_MAX_CONTEXT = 40000
_MAX_LIST_ITEMS = 80
# A single CLI tool call can run for several minutes; don't hand the workspace
# back to the browser until beyond the bridge's bounded per-call timeout.
_STALE_AFTER = 360.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_run_id() -> str:
    return "zs-" + uuid.uuid4().hex[:16]


class AgentControlStore:
    """Atomic local JSON ledger for browser/CLI handoff and runtime ownership."""

    def __init__(self, path: Optional[str] = None, stale_after: float = _STALE_AFTER):
        default = Path.home() / ".zeroscript-win" / "agent-control.json"
        self.path = Path(path or os.environ.get("ZS_AGENT_CONTROL_PATH", str(default))).expanduser()
        self.stale_after = float(stale_after)
        self._lock = threading.RLock()

    def _empty(self) -> dict[str, Any]:
        return {
            "version": 1,
            "run_id": None,
            "state": "idle",
            "owner": "none",
            "goal": "",
            "context": "",
            "plan": [],
            "acceptance_criteria": [],
            "workspace": "",
            "session_id": None,
            "progress": "No active run",
            "summary": "",
            "stop_requested": False,
            "created_at": None,
            "updated_at": _utc_now(),
            "last_heartbeat": None,
            "finished_at": None,
        }

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict) and loaded.get("version") == 1:
                result = self._empty()
                result.update(loaded)
                return result
        except (OSError, ValueError, TypeError):
            pass
        return self._empty()

    def _write_unlocked(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + "." + str(os.getpid()) + ".tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(str(temp), str(self.path))

    def _mark_stale_unlocked(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("owner") != "cli" or state.get("state") not in _ACTIVE_STATES:
            return state
        last = state.get("last_heartbeat")
        try:
            stale = last is None or (time.time() - float(last)) > self.stale_after
        except (TypeError, ValueError):
            stale = True
        if stale:
            state["state"] = "interrupted"
            state["owner"] = "none"
            state["progress"] = "CLI heartbeat expired; run can be resumed"
            state["summary"] = "The CLI process stopped responding before it recorded a final result."
            state["stop_requested"] = False
            state["updated_at"] = _utc_now()
            state["finished_at"] = _utc_now()
            self._write_unlocked(state)
        return state

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._mark_stale_unlocked(self._read_unlocked())
            return copy.deepcopy(state)

    @staticmethod
    def _clean_text(value: Any, name: str, limit: int, required: bool = False) -> str:
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(name + " must be text.")
        value = value.strip()
        if required and not value:
            raise ValueError(name + " is required.")
        if len(value) > limit:
            raise ValueError(name + " exceeds the " + str(limit) + "-character limit.")
        return value

    @staticmethod
    def _clean_list(value: Any, name: str, max_items: int = _MAX_LIST_ITEMS) -> list[str]:
        if value is None:
            value = []
        if not isinstance(value, list):
            raise ValueError(name + " must be a list of text items.")
        if len(value) > max_items:
            raise ValueError(name + " may contain at most " + str(max_items) + " items.")
        result = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(name + " entries must be text.")
            item = item.strip()
            if item:
                result.append(item[:2000])
        return result

    def handoff(self, payload: dict[str, Any], workspace_info: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Queue a handoff requested from the browser agent; CLI must claim it."""
        payload = payload or {}
        goal = self._clean_text(payload.get("goal"), "goal", _MAX_GOAL, required=True)
        context = self._clean_text(payload.get("context"), "context", _MAX_CONTEXT)
        plan = self._clean_list(payload.get("plan"), "plan")
        criteria = self._clean_list(payload.get("acceptance_criteria"), "acceptance_criteria")
        info = workspace_info if isinstance(workspace_info, dict) else {}
        workspace = self._clean_text(info.get("root") if info.get("configured") is True else "", "workspace", 4000)
        with self._lock:
            state = self._mark_stale_unlocked(self._read_unlocked())
            if state.get("owner") == "cli" and state.get("state") in _ACTIVE_STATES:
                raise RuntimeError("The CLI already owns an active run: " + str(state.get("run_id")))
            if state.get("state") == "interrupted":
                raise RuntimeError("An interrupted CLI run is recoverable. Resume it with 'zeroscript resume " + str(state.get("run_id")) + "' before queuing a new handoff.")
            if state.get("state") == "queued":
                raise RuntimeError("A browser-to-CLI handoff is already queued: " + str(state.get("run_id")))
            now = _utc_now()
            new_state = {
                **self._empty(),
                "run_id": _new_run_id(),
                "state": "queued",
                "owner": "browser",
                "goal": goal,
                "context": context,
                "plan": plan,
                "acceptance_criteria": criteria,
                "workspace": workspace,
                "session_id": None,
                "progress": "Handoff queued; waiting for the CLI to claim this task",
                "summary": "",
                "stop_requested": False,
                "created_at": now,
                "updated_at": now,
                "last_heartbeat": None,
                "finished_at": None,
            }
            self._write_unlocked(new_state)
            return copy.deepcopy(new_state)

    def start_run(self, payload: dict[str, Any], workspace_info: Optional[dict[str, Any]], session_id: str) -> dict[str, Any]:
        """Create and claim a direct CLI task in one atomic operation."""
        payload = payload or {}
        goal = self._clean_text(payload.get("goal"), "goal", _MAX_GOAL, required=True)
        context = self._clean_text(payload.get("context"), "context", _MAX_CONTEXT)
        plan = self._clean_list(payload.get("plan"), "plan")
        criteria = self._clean_list(payload.get("acceptance_criteria"), "acceptance_criteria")
        info = workspace_info if isinstance(workspace_info, dict) else {}
        workspace = self._clean_text(info.get("root") if info.get("configured") is True else "", "workspace", 4000)
        with self._lock:
            old = self._mark_stale_unlocked(self._read_unlocked())
            if old.get("state") == "queued":
                raise RuntimeError("A browser handoff is queued. Use 'zeroscript takeover' to claim it.")
            if old.get("state") == "interrupted":
                raise RuntimeError("An interrupted CLI run is recoverable. Resume it with 'zeroscript resume " + str(old.get("run_id")) + "' before starting a new task.")
            if old.get("owner") == "cli" and old.get("state") in _ACTIVE_STATES:
                raise RuntimeError("The CLI already owns active run " + str(old.get("run_id")) + ". Resume or stop that run first.")
            now = _utc_now()
            state = {
                **self._empty(),
                "run_id": _new_run_id(),
                "state": "active",
                "owner": "cli",
                "goal": goal,
                "context": context,
                "plan": plan,
                "acceptance_criteria": criteria,
                "workspace": workspace,
                "session_id": str(session_id),
                "progress": "CLI agent starting",
                "summary": "",
                "stop_requested": False,
                "created_at": now,
                "updated_at": now,
                "last_heartbeat": time.time(),
                "finished_at": None,
            }
            self._write_unlocked(state)
            return copy.deepcopy(state)

    def claim(self, session_id: str, run_id: Optional[str] = None, browser_idle: bool = True) -> dict[str, Any]:
        """Claim a queued handoff or resume an interrupted run."""
        session_id = self._clean_text(session_id, "session_id", 200, required=True)
        with self._lock:
            state = self._mark_stale_unlocked(self._read_unlocked())
            current_id = state.get("run_id")
            if run_id and run_id != current_id:
                return {"ok": False, "busy": False, "error": "Run ID does not match the current handoff.", "control": copy.deepcopy(state)}
            if state.get("owner") == "cli" and state.get("state") in _ACTIVE_STATES:
                if state.get("session_id") == session_id:
                    return {"ok": True, "already_owned": True, "control": copy.deepcopy(state)}
                return {"ok": False, "busy": True, "error": "Another CLI session owns this run.", "control": copy.deepcopy(state)}
            if state.get("state") not in {"queued", "interrupted"}:
                return {"ok": False, "busy": False, "error": "No queued or interrupted run is available to claim.", "control": copy.deepcopy(state)}
            if state.get("state") in {"queued", "interrupted"} and not browser_idle:
                return {"ok": False, "busy": True, "error": "A browser tool call is still in flight. Retry the claim after it finishes.", "control": copy.deepcopy(state)}
            state["state"] = "active"
            state["owner"] = "cli"
            state["session_id"] = session_id
            state["progress"] = "CLI claimed control; preparing independent agent runtime"
            state["stop_requested"] = False
            state["updated_at"] = _utc_now()
            state["last_heartbeat"] = time.time()
            state["finished_at"] = None
            self._write_unlocked(state)
            return {"ok": True, "already_owned": False, "control": copy.deepcopy(state)}

    def heartbeat(self, run_id: str, session_id: str, progress: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            state = self._mark_stale_unlocked(self._read_unlocked())
            if state.get("run_id") != run_id or state.get("owner") != "cli" or state.get("session_id") != session_id:
                raise RuntimeError("This CLI session no longer owns the active run.")
            if progress is not None:
                state["progress"] = self._clean_text(progress, "progress", 500)
            state["last_heartbeat"] = time.time()
            state["updated_at"] = _utc_now()
            self._write_unlocked(state)
            return copy.deepcopy(state)

    def request_stop(self, run_id: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            state = self._mark_stale_unlocked(self._read_unlocked())
            if run_id and state.get("run_id") != run_id:
                raise RuntimeError("Run ID does not match the active run.")
            if state.get("state") == "queued":
                state["state"] = "cancelled"
                state["owner"] = "none"
                state["progress"] = "Queued handoff cancelled"
                state["summary"] = "The queued handoff was cancelled before the CLI claimed it."
                state["finished_at"] = _utc_now()
            elif state.get("owner") == "cli" and state.get("state") in _ACTIVE_STATES:
                state["state"] = "stop_requested"
                state["stop_requested"] = True
                state["progress"] = "Stop requested; waiting for the CLI to reach a safe checkpoint"
            else:
                raise RuntimeError("There is no queued or active run to stop.")
            state["updated_at"] = _utc_now()
            self._write_unlocked(state)
            return copy.deepcopy(state)

    def finish(self, run_id: str, session_id: str, status: str, summary: str = "") -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled", "blocked"}:
            raise ValueError("Unsupported final status: " + str(status))
        summary = self._clean_text(summary, "summary", 10000)
        with self._lock:
            state = self._mark_stale_unlocked(self._read_unlocked())
            if state.get("run_id") != run_id or state.get("owner") != "cli" or state.get("session_id") != session_id:
                raise RuntimeError("This CLI session no longer owns the active run.")
            state["state"] = status
            state["owner"] = "none"
            state["progress"] = "CLI run " + status
            state["summary"] = summary
            state["stop_requested"] = False
            state["updated_at"] = _utc_now()
            state["finished_at"] = _utc_now()
            state["last_heartbeat"] = None
            self._write_unlocked(state)
            return copy.deepcopy(state)

    def yield_to_browser(self, run_id: str, session_id: str, summary: str = "") -> dict[str, Any]:
        summary = self._clean_text(summary, "summary", 10000)
        with self._lock:
            state = self._mark_stale_unlocked(self._read_unlocked())
            if state.get("run_id") != run_id or state.get("owner") != "cli" or state.get("session_id") != session_id:
                raise RuntimeError("This CLI session no longer owns the active run.")
            state["state"] = "returned_to_browser"
            state["owner"] = "browser"
            state["progress"] = "CLI yielded control back to the browser agent"
            state["summary"] = summary
            state["stop_requested"] = False
            state["updated_at"] = _utc_now()
            state["finished_at"] = _utc_now()
            state["last_heartbeat"] = None
            self._write_unlocked(state)
            return copy.deepcopy(state)

    def browser_tool_allowed(self, tool_name: str) -> bool:
        """Enforce handoff and single-writer ownership at the bridge boundary."""
        if tool_name == "zeroscript_agent_control_status":
            return True
        state = self.status()
        # A queued handoff pauses further browser tool calls so the current
        # handoff call can settle and the CLI can claim without a race.
        if state.get("state") == "queued":
            return False
        return not (state.get("owner") == "cli" and state.get("state") in _ACTIVE_STATES)

    def reset_for_tests(self) -> None:
        with self._lock:
            self._write_unlocked(self._empty())
