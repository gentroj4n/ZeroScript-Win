# SPDX-License-Identifier: GPL-3.0-or-later
"""Claude Code-compatible, opt-in command hooks for the local CLI.

Hooks are only run when explicitly configured in a supported settings.json file.
Hook output is surfaced as untrusted diagnostic context; exit codes do not silently
block or rewrite the agent's normal tool flow.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOOK_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"})
_MAX_SETTINGS_BYTES = 128 * 1024
_MAX_HOOKS_PER_EVENT = 40
_MAX_COMMAND_CHARS = 4000
_MAX_OUTPUT_CHARS = 6000
_DEFAULT_TIMEOUT = 10.0


@dataclass(frozen=True)
class HookSpec:
    event: str
    command: str
    matcher: str
    settings_path: str


@dataclass(frozen=True)
class HookResult:
    event: str
    command: str
    settings_path: str
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def output(self) -> str:
        chunks = []
        if self.stdout.strip():
            chunks.append(self.stdout.strip())
        if self.stderr.strip():
            chunks.append("stderr: " + self.stderr.strip())
        if self.timed_out:
            chunks.append("Hook timed out.")
        elif self.returncode not in (0, None):
            chunks.append("Hook exited with status " + str(self.returncode) + ".")
        return "\n".join(chunks)[:_MAX_OUTPUT_CHARS]


def _read_settings(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_SETTINGS_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _event_groups(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _hook_commands(group: dict[str, Any]) -> list[str]:
    raw = group.get("hooks")
    if isinstance(raw, str):
        raw = [{"type": "command", "command": raw}]
    if not isinstance(raw, list):
        return []
    commands: list[str] = []
    for item in raw:
        if isinstance(item, str):
            command = item
        elif isinstance(item, dict) and str(item.get("type", "command")).lower() == "command":
            command = item.get("command", "")
        else:
            continue
        if isinstance(command, str) and command.strip():
            commands.append(command.strip()[:_MAX_COMMAND_CHARS])
    return commands


class HookRunner:
    """Read Claude-style hook groups and run their commands with bounded time/output."""

    def __init__(
        self,
        project_root: str | Path | None = None,
        home: str | Path | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self.project_root = Path(project_root).expanduser().resolve() if project_root else None
        self.home = Path(home).expanduser().resolve() if home else Path.home().resolve()
        self.timeout = max(0.1, min(float(timeout), 60.0))

    def settings_paths(self) -> list[Path]:
        paths = [
            self.home / ".claude" / "settings.json",
            self.home / ".zeroscript-win" / "settings.json",
        ]
        if self.project_root:
            paths.extend([
                self.project_root / ".claude" / "settings.json",
                self.project_root / ".claude" / "settings.local.json",
                self.project_root / ".zeroscript" / "settings.json",
            ])
        return paths

    def specs(self, event: str, match_value: str | None = None) -> list[HookSpec]:
        if event not in HOOK_EVENTS:
            return []
        found: list[HookSpec] = []
        for settings_path in self.settings_paths():
            config = _read_settings(settings_path)
            hooks = config.get("hooks") if config else None
            if not isinstance(hooks, dict):
                continue
            for group in _event_groups(hooks.get(event)):
                matcher = group.get("matcher", "")
                if matcher is not None and not isinstance(matcher, str):
                    continue
                matcher = matcher or ""
                if matcher and match_value is not None:
                    try:
                        if re.search(matcher, match_value) is None:
                            continue
                    except re.error:
                        continue
                for command in _hook_commands(group):
                    found.append(HookSpec(event, command, matcher, str(settings_path)))
                    if len(found) >= _MAX_HOOKS_PER_EVENT:
                        return found
        return found

    async def run(self, event: str, payload: dict[str, Any] | None = None) -> list[HookResult]:
        data = dict(payload or {})
        data.setdefault("hook_event_name", event)
        data.setdefault("cwd", str(self.project_root or Path.cwd()))
        match_value = str(data.get("tool_name") or data.get("prompt") or data.get("source") or "")
        specs = self.specs(event, match_value)
        results: list[HookResult] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(self.timeout * 2, 30.0)
        for spec in specs:
            remaining = deadline - loop.time()
            if remaining <= 0:
                results.append(HookResult(
                    event=event,
                    command="(remaining hooks skipped)",
                    settings_path="",
                    returncode=None,
                    stderr="Event-wide hook time budget exhausted.",
                    timed_out=True,
                ))
                break
            env = os.environ.copy()
            project_dir = str(self.project_root or "")
            env.update({
                "ZS_HOOK_EVENT": event,
                "ZS_PROJECT_ROOT": project_dir,
                "ZS_HOOK_SETTINGS": spec.settings_path,
                "CLAUDE_PROJECT_DIR": project_dir,
                "CLAUDE_HOOK_EVENT": event,
                "CLAUDE_SETTINGS_PATH": spec.settings_path,
            })
            if data.get("session_id") is not None:
                env["ZS_SESSION_ID"] = str(data["session_id"])
                env["CLAUDE_SESSION_ID"] = str(data["session_id"])
            if data.get("tool_name") is not None:
                env["ZS_TOOL_NAME"] = str(data["tool_name"])
                env["CLAUDE_TOOL_NAME"] = str(data["tool_name"])
            process = None
            try:
                process = await asyncio.create_subprocess_shell(
                    spec.command,
                    cwd=str(self.project_root) if self.project_root and self.project_root.is_dir() else None,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                timed_out = False
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(json.dumps(data, ensure_ascii=False).encode("utf-8")),
                        timeout=min(self.timeout, remaining),
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    process.kill()
                    stdout, stderr = await process.communicate()
                results.append(HookResult(
                    event=event,
                    command=spec.command,
                    settings_path=spec.settings_path,
                    returncode=process.returncode,
                    stdout=stdout.decode("utf-8", errors="replace")[-_MAX_OUTPUT_CHARS:],
                    stderr=stderr.decode("utf-8", errors="replace")[-_MAX_OUTPUT_CHARS:],
                    timed_out=timed_out,
                ))
            except (OSError, ValueError) as exc:
                results.append(HookResult(
                    event=event,
                    command=spec.command,
                    settings_path=spec.settings_path,
                    returncode=None,
                    stderr=str(exc),
                ))
        return results
