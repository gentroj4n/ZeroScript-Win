# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent multi-run handoff ledger for the ZeroScript agent runtimes.

The bridge is the authority for this record. Browser chat tabs and CLI sessions
never edit the file directly; they go through bridge messages so every client
sees the same ownership transitions.

Schema (version 2)::

    {"version": 2,
     "runs":     {run_id: run},
     "sessions": {session_id: session}}

Each run is owned by one origin session (the browser tab that handed it off, or
the CLI session that started it) and handled by one target CLI session.
Many runs and many armed CLI sessions can exist at the same time.
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


_VERSION = 2
_ACTIVE_STATES = {"active", "stop_requested"}
_HANDED_OFF_STATES = {"queued", "active", "stop_requested"}
_TERMINAL_STATES = {"completed", "failed", "stopped", "yielded", "stale"}
_FINISH_STATUSES = {"completed", "failed", "stopped"}
# Legacy final statuses from the single-run ledger, mapped to the v2 vocabulary.
_FINISH_ALIASES = {"cancelled": "stopped", "blocked": "failed"}
_ALWAYS_ALLOWED_BROWSER_TOOLS = {"zeroscript_agent_control_status"}
_MAX_TERMINAL_RUNS = 50
_MAX_GOAL = 12000
_MAX_CONTEXT = 40000
_MAX_LIST_ITEMS = 80
# A single CLI tool call can run for several minutes; don't treat a run as dead
# until beyond the bridge's bounded per-call timeout.
_STALE_AFTER = 360.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_run_id() -> str:
    return "zs-" + uuid.uuid4().hex[:16]


def _workspace_key(path: Any) -> Optional[str]:
    """Normalised comparison key for a workspace path; None when unset."""
    if not isinstance(path, str) or not path.strip():
        return None
    return os.path.normcase(os.path.abspath(path.strip()))


class AgentControlStore:
    """Atomic local JSON ledger for many concurrent browser/CLI runs."""

    def __init__(self, path: Optional[str] = None, stale_after: float = _STALE_AFTER):
        default = Path.home() / ".zeroscript-win" / "agent-control.json"
        self.path = Path(path or os.environ.get("ZS_AGENT_CONTROL_PATH", str(default))).expanduser()
        self.stale_after = float(stale_after)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ storage

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": _VERSION, "runs": {}, "sessions": {}}

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if (
                isinstance(loaded, dict)
                and loaded.get("version") == _VERSION
                and isinstance(loaded.get("runs"), dict)
                and isinstance(loaded.get("sessions"), dict)
            ):
                return {"version": _VERSION, "runs": loaded["runs"], "sessions": loaded["sessions"]}
        except (OSError, ValueError, TypeError):
            pass
        # Missing, corrupt, or a v1 single-record file: start empty (no migration).
        return self._empty()

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self._prune_unlocked(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + "." + str(os.getpid()) + ".tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(str(temp), str(self.path))

    @staticmethod
    def _prune_unlocked(data: dict[str, Any]) -> None:
        runs = data["runs"]
        terminal = [
            (float(run.get("finished_ts") or 0.0), run_id)
            for run_id, run in runs.items()
            if run.get("state") in _TERMINAL_STATES
        ]
        excess = len(terminal) - _MAX_TERMINAL_RUNS
        if excess > 0:
            terminal.sort()
            for _, run_id in terminal[:excess]:
                runs.pop(run_id, None)

    def _mark_stale_unlocked(self, data: dict[str, Any]) -> bool:
        """Mark running runs with an expired heartbeat as stale. True if changed."""
        changed = False
        now = time.time()
        for run in data["runs"].values():
            if run.get("state") not in _ACTIVE_STATES:
                continue
            try:
                last = run.get("heartbeat_at")
                stale = last is None or (now - float(last)) > self.stale_after
            except (TypeError, ValueError):
                stale = True
            if stale:
                self._finalize(
                    run, "stale",
                    "CLI heartbeat expired; the run was marked stale",
                    "The CLI process stopped responding before it recorded a final result.",
                )
                changed = True
        return changed

    def _load_unlocked(self) -> dict[str, Any]:
        """Read the ledger, applying (and persisting) per-run stale detection."""
        data = self._read_unlocked()
        if self._mark_stale_unlocked(data):
            self._write_unlocked(data)
        return data

    @staticmethod
    def _finalize(run: dict[str, Any], state: str, progress: str, summary: Optional[str] = None) -> None:
        run["state"] = state
        run["stop_requested"] = False
        run["progress"] = progress
        if summary is not None:
            run["summary"] = summary
        run["updated_at"] = _utc_now()
        run["finished_at"] = _utc_now()
        run["finished_ts"] = time.time()

    # ------------------------------------------------------------------ helpers

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

    def _clean_session_id(self, value: Any) -> str:
        return self._clean_text(value, "session_id", 200, required=True)

    def _clean_workspace(self, value: Any) -> Optional[str]:
        text = self._clean_text(value, "workspace", 4000)
        return text or None

    def _clean_run_fields(self, payload: Optional[dict[str, Any]], workspace_info: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = payload or {}
        info = workspace_info if isinstance(workspace_info, dict) else {}
        # An explicitly unconfigured workspace never counts as a real root.
        root = info.get("root") if info.get("configured") is not False else ""
        return {
            "goal": self._clean_text(payload.get("goal"), "goal", _MAX_GOAL, required=True),
            "context": self._clean_text(payload.get("context"), "context", _MAX_CONTEXT),
            "plan": self._clean_list(payload.get("plan"), "plan"),
            "acceptance_criteria": self._clean_list(payload.get("acceptance_criteria"), "acceptance_criteria"),
            "workspace": self._clean_text(root, "workspace", 4000),
        }

    @staticmethod
    def _new_run(fields: dict[str, Any], origin: str, origin_role: str, target: Optional[str], state: str, progress: str) -> dict[str, Any]:
        now = _utc_now()
        return {
            "run_id": _new_run_id(),
            "origin_session": origin,
            "origin_role": origin_role,
            "target_session": target,
            "workspace": fields["workspace"],
            "state": state,
            "goal": fields["goal"],
            "context": fields["context"],
            "plan": fields["plan"],
            "acceptance_criteria": fields["acceptance_criteria"],
            "progress": progress,
            "summary": "",
            "stop_requested": False,
            "created_at": now,
            "updated_at": now,
            "heartbeat_at": time.time() if state == "active" else None,
            "finished_at": None,
            "finished_ts": None,
        }

    @staticmethod
    def _touch_session(session: dict[str, Any]) -> None:
        session["last_seen"] = _utc_now()

    def _ensure_session(self, data: dict[str, Any], session_id: str, role: str) -> dict[str, Any]:
        session = data["sessions"].get(session_id)
        if session is None:
            session = {"session_id": session_id, "role": role, "workspace": None, "armed": False, "armed_at": None, "last_seen": _utc_now()}
            data["sessions"][session_id] = session
        else:
            self._touch_session(session)
        return session

    @staticmethod
    def _require_run(data: dict[str, Any], run_id: Any) -> dict[str, Any]:
        run = data["runs"].get(run_id) if isinstance(run_id, str) else None
        if run is None:
            raise RuntimeError("Run " + str(run_id) + " was not found (it may have been pruned).")
        return run

    def _require_owned_running(self, data: dict[str, Any], run_id: str, session_id: str) -> dict[str, Any]:
        run = self._require_run(data, run_id)
        if run.get("target_session") != session_id:
            raise RuntimeError("CLI session " + str(session_id) + " is not the target of run " + str(run_id) + ".")
        if run.get("state") not in _ACTIVE_STATES:
            raise RuntimeError("Run " + str(run_id) + " is " + str(run.get("state")) + "; CLI session " + str(session_id) + " no longer owns an active run.")
        return run

    @staticmethod
    def _armed_sort_key(session: dict[str, Any]) -> float:
        try:
            return float(session.get("armed_at") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ----------------------------------------------------------------- sessions

    def register_session(self, session_id: str, role: str, workspace_root: Optional[str] = None) -> dict[str, Any]:
        session_id = self._clean_session_id(session_id)
        if role not in ("browser", "cli"):
            raise ValueError("role must be 'browser' or 'cli', got " + repr(role) + " for session " + session_id + ".")
        workspace = self._clean_workspace(workspace_root)
        with self._lock:
            data = self._load_unlocked()
            session = data["sessions"].get(session_id)
            if session is None:
                session = {"session_id": session_id, "role": role, "workspace": workspace, "armed": False, "armed_at": None, "last_seen": _utc_now()}
                data["sessions"][session_id] = session
            else:
                # Re-registering is idempotent and keeps the armed flag for CLIs.
                session["role"] = role
                if workspace is not None:
                    session["workspace"] = workspace
                self._touch_session(session)
            if role != "cli":
                session["armed"] = False
                session["armed_at"] = None
            self._write_unlocked(data)
            return copy.deepcopy(session)

    def unregister_session(self, session_id: str) -> None:
        session_id = self._clean_session_id(session_id)
        with self._lock:
            data = self._load_unlocked()
            data["sessions"].pop(session_id, None)
            for run in data["runs"].values():
                if run.get("state") == "queued" and run.get("target_session") == session_id:
                    run["target_session"] = None
                    run["progress"] = "Target CLI session left; waiting for another CLI to claim this task"
                    run["updated_at"] = _utc_now()
            # Active runs owned by this session are left for stale detection.
            self._write_unlocked(data)

    def set_session_workspace(self, session_id: str, workspace_root: Optional[str]) -> dict[str, Any]:
        session_id = self._clean_session_id(session_id)
        workspace = self._clean_workspace(workspace_root)
        with self._lock:
            data = self._load_unlocked()
            session = data["sessions"].get(session_id)
            if session is None:
                raise ValueError("Session " + session_id + " is not registered.")
            session["workspace"] = workspace
            self._touch_session(session)
            self._write_unlocked(data)
            return copy.deepcopy(session)

    def arm(self, session_id: str, workspace_root: Optional[str]) -> dict[str, Any]:
        """Mark a CLI session as waiting for handoffs. Many may be armed at once."""
        session_id = self._clean_session_id(session_id)
        workspace = self._clean_workspace(workspace_root)
        with self._lock:
            data = self._load_unlocked()
            session = self._ensure_session(data, session_id, "cli")
            if session.get("role") != "cli":
                raise ValueError("Session " + session_id + " is a " + str(session.get("role")) + " session; only CLI sessions can be armed.")
            for run in data["runs"].values():
                if run.get("state") in _ACTIVE_STATES and run.get("target_session") == session_id:
                    raise RuntimeError("CLI session " + session_id + " already owns active run " + str(run.get("run_id")) + "; finish or stop it before arming.")
            if workspace is not None:
                session["workspace"] = workspace
            if not session.get("armed"):
                session["armed_at"] = time.time()
            session["armed"] = True
            self._write_unlocked(data)
            return copy.deepcopy(session)

    def disarm(self, session_id: str) -> dict[str, Any]:
        session_id = self._clean_session_id(session_id)
        with self._lock:
            data = self._load_unlocked()
            session = data["sessions"].get(session_id)
            if session is None:
                raise ValueError("Session " + session_id + " is not registered.")
            session["armed"] = False
            session["armed_at"] = None
            self._touch_session(session)
            self._write_unlocked(data)
            return copy.deepcopy(session)

    def armed_sessions(self, workspace_root: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            data = self._load_unlocked()
            return self._armed_unlocked(data, workspace_root, filter_workspace=workspace_root is not None)

    def _armed_unlocked(self, data: dict[str, Any], workspace_root: Optional[str], filter_workspace: bool) -> list[dict[str, Any]]:
        wanted = _workspace_key(workspace_root)
        found = [
            s for s in data["sessions"].values()
            if s.get("role") == "cli" and s.get("armed")
            and (not filter_workspace or _workspace_key(s.get("workspace")) == wanted)
        ]
        found.sort(key=self._armed_sort_key)
        return copy.deepcopy(found)

    # --------------------------------------------------------------------- runs

    def handoff(
        self,
        payload: dict[str, Any],
        workspace_info: Optional[dict[str, Any]],
        origin_session_id: str,
        target_session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Queue a handoff from a browser session; a CLI session must claim it."""
        fields = self._clean_run_fields(payload, workspace_info)
        origin = self._clean_session_id(origin_session_id)
        target = self._clean_text(target_session_id, "target_session_id", 200) or None
        with self._lock:
            data = self._load_unlocked()
            for run in data["runs"].values():
                if run.get("origin_session") == origin and run.get("state") not in _TERMINAL_STATES:
                    raise ValueError("Session " + origin + " already has a non-terminal run: " + str(run.get("run_id")) + " (" + str(run.get("state")) + ").")
            origin_session = self._ensure_session(data, origin, "browser")
            if origin_session.get("role") != "browser":
                raise ValueError("Session " + origin + " is a " + str(origin_session.get("role")) + " session; handoffs must originate from a browser session.")
            if target is not None:
                target_session = data["sessions"].get(target)
                if target_session is None or target_session.get("role") != "cli":
                    raise ValueError("Target session " + target + " is not a registered CLI session.")
            else:
                key = _workspace_key(fields["workspace"])
                pending_targets = {
                    r.get("target_session") for r in data["runs"].values() if r.get("state") == "queued" and r.get("target_session")
                }
                # Longest-armed first; a CLI already earmarked for a queued run is skipped
                # so parallel handoffs spread across armed CLIs instead of piling up.
                candidates = [
                    s for s in self._armed_unlocked(data, None, filter_workspace=False)
                    if _workspace_key(s.get("workspace")) == key and s["session_id"] not in pending_targets
                ]
                if candidates:
                    target = candidates[0]["session_id"]
            progress = (
                "Handoff queued for CLI session " + target + "; waiting for it to claim this task"
                if target else
                "Handoff queued; waiting for a CLI on this workspace to claim this task"
            )
            run = self._new_run(fields, origin, "browser", target, "queued", progress)
            data["runs"][run["run_id"]] = run
            self._write_unlocked(data)
            return copy.deepcopy(run)

    def claim(self, session_id: str, run_id: Optional[str] = None) -> dict[str, Any]:
        """Claim a queued run targeted at this session, or an unassigned one on its workspace."""
        session_id = self._clean_session_id(session_id)

        def fail(message: str) -> dict[str, Any]:
            return {"ok": False, "run": None, "error": message}

        with self._lock:
            data = self._load_unlocked()
            session = self._ensure_session(data, session_id, "cli")
            if session.get("role") != "cli":
                return fail("Session " + session_id + " is not a CLI session.")
            for existing in data["runs"].values():
                if existing.get("state") in _ACTIVE_STATES and existing.get("target_session") == session_id:
                    return fail("CLI session " + session_id + " already owns active run " + str(existing.get("run_id")) + ".")
            session_key = _workspace_key(session.get("workspace"))

            def claimable(candidate: dict[str, Any]) -> bool:
                if candidate.get("state") != "queued":
                    return False
                if candidate.get("target_session") == session_id:
                    return True
                return candidate.get("target_session") is None and _workspace_key(candidate.get("workspace")) == session_key

            if run_id:
                run = data["runs"].get(run_id)
                if run is None:
                    return fail("Run " + str(run_id) + " was not found.")
                if run.get("state") != "queued":
                    return fail("Run " + run_id + " is " + str(run.get("state")) + ", not queued.")
                if run.get("target_session") not in (None, session_id):
                    return fail("Run " + run_id + " is targeted at CLI session " + str(run.get("target_session")) + ", not " + session_id + ".")
                if not claimable(run):
                    return fail("Run " + run_id + " is for a different workspace than CLI session " + session_id + ".")
            else:
                queued = [r for r in data["runs"].values() if claimable(r)]
                targeted = [r for r in queued if r.get("target_session") == session_id]
                pool = targeted or queued
                if not pool:
                    return fail("No queued run is available for CLI session " + session_id + ".")
                run = pool[0]  # oldest first (dict preserves creation order)
            run["target_session"] = session_id
            run["state"] = "active"
            run["progress"] = "CLI claimed control; preparing independent agent runtime"
            run["stop_requested"] = False
            run["heartbeat_at"] = time.time()
            run["updated_at"] = _utc_now()
            session["armed"] = False
            session["armed_at"] = None
            self._write_unlocked(data)
            return {"ok": True, "run": copy.deepcopy(run), "error": None}

    def start_run(self, payload: dict[str, Any], workspace_info: Optional[dict[str, Any]], session_id: str) -> dict[str, Any]:
        """Create and activate a CLI-started run (origin == target == session)."""
        fields = self._clean_run_fields(payload, workspace_info)
        session_id = self._clean_session_id(session_id)
        with self._lock:
            data = self._load_unlocked()
            for existing in data["runs"].values():
                if existing.get("state") in _ACTIVE_STATES and existing.get("target_session") == session_id:
                    raise RuntimeError("CLI session " + session_id + " already owns active run " + str(existing.get("run_id")) + ". Resume or stop that run first.")
            session = self._ensure_session(data, session_id, "cli")
            if session.get("role") != "cli":
                raise ValueError("Session " + session_id + " is a " + str(session.get("role")) + " session; only CLI sessions can start runs.")
            if fields["workspace"]:
                session["workspace"] = fields["workspace"]
            session["armed"] = False
            session["armed_at"] = None
            run = self._new_run(fields, session_id, "cli", session_id, "active", "CLI agent starting")
            data["runs"][run["run_id"]] = run
            self._write_unlocked(data)
            return copy.deepcopy(run)

    def heartbeat(self, run_id: str, session_id: str, progress: Optional[str] = None) -> dict[str, Any]:
        if progress is not None:
            progress = self._clean_text(progress, "progress", 500)
        with self._lock:
            data = self._load_unlocked()
            run = self._require_owned_running(data, run_id, session_id)
            if progress is not None:
                run["progress"] = progress
            run["heartbeat_at"] = time.time()
            run["updated_at"] = _utc_now()
            session = data["sessions"].get(session_id)
            if session is not None:
                self._touch_session(session)
            self._write_unlocked(data)
            return copy.deepcopy(run)

    def request_stop(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            data = self._load_unlocked()
            run = self._require_run(data, run_id)
            state = run.get("state")
            if state == "queued":
                self._finalize(run, "stopped", "Queued handoff cancelled", "The queued handoff was cancelled before a CLI claimed it.")
            elif state in _ACTIVE_STATES:
                run["state"] = "stop_requested"
                run["stop_requested"] = True
                run["progress"] = "Stop requested; waiting for the CLI to reach a safe checkpoint"
                run["updated_at"] = _utc_now()
            else:
                raise RuntimeError("Run " + str(run_id) + " is " + str(state) + "; there is nothing to stop.")
            self._write_unlocked(data)
            return copy.deepcopy(run)

    def finish(self, run_id: str, session_id: str, status: str, summary: str = "") -> dict[str, Any]:
        status = _FINISH_ALIASES.get(status, status)
        if status not in _FINISH_STATUSES:
            raise ValueError("Unsupported final status: " + str(status))
        summary = self._clean_text(summary, "summary", 10000)
        with self._lock:
            data = self._load_unlocked()
            run = self._require_owned_running(data, run_id, session_id)
            self._finalize(run, status, "CLI run " + status, summary)
            self._write_unlocked(data)
            return copy.deepcopy(run)

    def yield_to_browser(self, run_id: str, session_id: str, summary: str = "") -> dict[str, Any]:
        summary = self._clean_text(summary, "summary", 10000)
        with self._lock:
            data = self._load_unlocked()
            run = self._require_owned_running(data, run_id, session_id)
            self._finalize(run, "yielded", "CLI yielded control back to the browser agent", summary)
            self._write_unlocked(data)
            return copy.deepcopy(run)

    def browser_tool_allowed(self, tool_name: str, session_id: Optional[str]) -> bool:
        """Block only the browser session whose own run is handed off to a CLI."""
        if tool_name in _ALWAYS_ALLOWED_BROWSER_TOOLS:
            return True
        if not session_id:
            return True
        with self._lock:
            data = self._load_unlocked()
            return not any(
                run.get("origin_session") == session_id
                and run.get("origin_role") == "browser"
                and run.get("state") in _HANDED_OFF_STATES
                for run in data["runs"].values()
            )

    # ------------------------------------------------------------------ queries

    def run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            found = self._load_unlocked()["runs"].get(run_id) if isinstance(run_id, str) else None
            return copy.deepcopy(found) if found is not None else None

    def status(self, session_id: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            data = self._load_unlocked()
            runs = list(data["runs"].values())
            if session_id:
                runs = [r for r in runs if session_id in (r.get("origin_session"), r.get("target_session"))]
            return {
                "version": _VERSION,
                "runs": copy.deepcopy(runs),
                "sessions": copy.deepcopy(list(data["sessions"].values())),
            }

    def reset_for_tests(self) -> None:
        with self._lock:
            self._write_unlocked(self._empty())
