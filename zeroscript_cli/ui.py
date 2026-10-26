# SPDX-License-Identifier: GPL-3.0-or-later
"""Polished terminal presentation and persistent prompt history for ZeroScript CLI."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from contextlib import nullcontext
from typing import Any, Iterable, Optional

try:
    from rich import box
    from rich.align import Align
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.theme import Theme
    _RICH = True
except ImportError:  # Keep the CLI usable if optional presentation dependencies are missing.
    Console = Markdown = Panel = Table = Text = Align = Theme = box = None
    _RICH = False

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import FuzzyCompleter, WordCompleter
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.formatted_text import HTML
    _PROMPT_TOOLKIT = True
except ImportError:
    PromptSession = FuzzyCompleter = WordCompleter = FileHistory = KeyBindings = HTML = None
    _PROMPT_TOOLKIT = False


COMMANDS = [
    "/help", "/status", "/new", "/sessions", "/load", "/resume", "/continue",
    "/compact", "/clear", "/model", "/tools", "/context", "/workspace", "/takeover",
    "/skills", "/skill", "/commands", "/agents", "/agent", "/reload", "/init", "/doctor", "/stop", "/return-browser", "/quit",
]


def _state_dir() -> Path:
    return Path(os.environ.get("ZS_CLI_HOME", str(Path.home() / ".zeroscript-win"))).expanduser()


def _prefer_utf8_console() -> None:
    # Windows CMD can default to a legacy code page; UI glyphs should never crash it.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


class TerminalUI:
    """One consistent terminal surface; gracefully degrades to standard input/output."""

    def __init__(self) -> None:
        _prefer_utf8_console()
        theme = Theme({
            "accent": "bold bright_cyan",
            "muted": "dim",
            "success": "bold green",
            "warning": "bold yellow",
            "error": "bold red",
            "tool": "bold bright_blue",
        }) if _RICH else None
        self.console = Console(theme=theme, highlight=False, soft_wrap=True) if _RICH else None
        self._session = None
        if _PROMPT_TOOLKIT:
            try:
                history_dir = _state_dir()
                history_dir.mkdir(parents=True, exist_ok=True)
                bindings = KeyBindings()

                @bindings.add("enter")
                def _submit_enter(event: Any) -> None:
                    event.current_buffer.validate_and_handle()

                @bindings.add("escape", "enter")
                def _insert_multiline_break(event: Any) -> None:
                    event.current_buffer.insert_text("\n")

                self._session = PromptSession(
                    history=FileHistory(str(history_dir / "input-history.txt")),
                    completer=FuzzyCompleter(WordCompleter(COMMANDS, sentence=True, match_middle=True)),
                    complete_while_typing=True,
                    multiline=True,
                    key_bindings=bindings,
                    enable_history_search=True,
                )
            except Exception:
                self._session = None

    @property
    def rich_enabled(self) -> bool:
        return self.console is not None

    def set_commands(self, commands: Iterable[str]) -> None:
        """Update slash-command completion after project skills/commands are discovered."""
        if not _PROMPT_TOOLKIT or self._session is None:
            return
        words = sorted({item for item in list(COMMANDS) + list(commands) if isinstance(item, str) and item.startswith("/")})
        try:
            self._session.completer = FuzzyCompleter(WordCompleter(words, sentence=True, match_middle=True))
        except Exception:
            # Completion is optional; the command dispatcher remains authoritative.
            pass

    def print(self, message: Any = "", style: Optional[str] = None) -> None:
        if self.console:
            self.console.print(message, style=style, markup=False)
        else:
            print(str(message))

    def error(self, message: str) -> None:
        self.print("✗ " + message, "error" if self.console else None)

    def success(self, message: str) -> None:
        self.print("✓ " + message, "success" if self.console else None)

    def info(self, message: str) -> None:
        self.print("› " + message, "accent" if self.console else None)

    def warning(self, message: str) -> None:
        self.print("! " + message, "warning" if self.console else None)

    def rule(self, title: str = "") -> None:
        if self.console:
            self.console.rule(title, style="bright_black")
        elif title:
            print("\n== " + title + " ==")

    def banner(self, model: str, endpoint: str, bridge_url: str) -> None:
        if not self.console:
            print("ZeroScript Code  |  model: " + (model or "not configured"))
            print("Endpoint: " + endpoint + "  |  Bridge: " + bridge_url)
            print("Type /help for commands. Enter submits. Esc then Enter inserts a newline for multiline prompts.")
            return
        heading = Text()
        heading.append("ZERO", style="bold white")
        heading.append("SCRIPT", style="bold bright_cyan")
        heading.append("  CODE", style="bold white")
        heading.append("   local-first coding agent", style="dim")
        details = Table.grid(expand=True, padding=(0, 1))
        details.add_column(style="dim", width=12)
        details.add_column(ratio=1)
        details.add_column(style="dim", width=12)
        details.add_column(ratio=2)
        details.add_row("MODEL", model or "not configured", "BRIDGE", bridge_url)
        details.add_row("ENDPOINT", endpoint, "", "")
        self.console.print(Panel(
            details,
            title=heading,
            subtitle="Esc then Enter submits multiline input  •  /help for commands",
            border_style="bright_blue",
            padding=(1, 2),
        ))
        self.console.print("[muted]Local project files stay on this machine unless the configured model endpoint sends prompt content elsewhere.[/muted]")

    def project_card(self, root: str, configured: bool) -> None:
        if not self.console:
            print("Workspace: " + (root if configured else "not selected"))
            return
        status = Text("READY  ", style="bold green") if configured else Text("NOT SELECTED  ", style="bold yellow")
        status.append(root if configured else "Choose a project workspace before asking for code changes.")
        self.console.print(Panel(
            status,
            title="Workspace",
            border_style="bright_black",
            padding=(0, 1),
        ))

    def markdown(self, content: str) -> None:
        if self.console:
            self.console.print(Markdown(content or "_No response text._"))
        else:
            print(content or "(no response text)")

    def panel(self, title: str, content: str, style: str = "bright_black") -> None:
        if self.console:
            self.console.print(Panel(Text(content, overflow="fold"), title=title, border_style=style, padding=(0, 1)))
        else:
            print("\n[" + title + "]\n" + content)

    def table(self, title: str, columns: Iterable[str], rows: Iterable[Iterable[Any]]) -> None:
        if not self.console:
            print("\n" + title)
            for row in rows:
                print("  " + " | ".join(str(item) for item in row))
            return
        table = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold bright_cyan", expand=True)
        for name in columns:
            table.add_column(str(name), overflow="fold")
        for row in rows:
            table.add_row(*(str(item) for item in row))
        self.console.print(table)

    def status(self, message: str):
        return self.console.status(message, spinner="dots", spinner_style="bright_cyan") if self.console else nullcontext()

    def clear(self) -> None:
        if self.console:
            self.console.clear()
        else:
            os.system("cls" if os.name == "nt" else "clear")

    async def prompt(self, title: str = "you") -> str:
        """Read input through prompt-toolkit async API."""
        if self._session:
            value = await self._session.prompt_async(
                HTML("<ansibrightcyan>❯ </ansibrightcyan>") if HTML else "❯ ",
                prompt_continuation=lambda width, line_number, is_soft_wrap: " " * (width - 1) + "· ",
            )
            return str(value).strip()
        # Preserve a responsive asyncio loop even when prompt-toolkit is absent.
        return (await asyncio.to_thread(input, "\n" + title + " › ")).strip()

    def tool_start(self, name: str, arguments: str) -> None:
        if self.console:
            self.console.print()
            self.console.print(Text("↳ " + name, style="tool"))
            if arguments:
                self.console.print(Panel(arguments, title="Arguments", border_style="bright_black", padding=(0, 1)))
        else:
            print("\n> " + name + " " + arguments)

    def tool_result(self, content: str, ok: bool = True) -> None:
        if self.console:
            style = "green" if ok else "red"
            self.console.print(Panel(Text(content, overflow="fold"), title="Result" if ok else "Tool error", border_style=style, padding=(0, 1)))
        else:
            print(content)

    def help(self) -> None:
        rows = [
            ("/help", "Show commands and keyboard shortcuts"),
            ("/status", "Show the bridge, workspace, and ownership state"),
            ("/new", "Start a fresh conversation without losing saved sessions"),
            ("/sessions", "List recent saved runs and conversations"),
            ("/load RUN_ID", "Load a saved conversation as the current context"),
            ("/resume RUN_ID", "Recover an interrupted CLI run"),
            ("/continue [message]", "Continue the current conversation"),
            ("/compact", "Ask the configured model to condense long conversation context"),
            ("/context", "Show active conversation and workspace context"),
            ("/skills [QUERY]", "List project and user skills"),
            ("/skill NAME [ARGS]", "Run a skill with optional arguments"),
            ("/commands [QUERY]", "List custom Markdown slash commands"),
            ("/agents [QUERY]", "List project and user agent profiles"),
            ("/agent NAME TASK", "Run a task using a named agent profile"),
            ("/reload", "Rescan built-in, project, and user skills/commands/agents"),
            ("/init", "Create a starter CLAUDE.md in the selected project"),
            ("/doctor", "Check local CLI and bridge prerequisites"),
            ("/workspace [PATH]", "Inspect or select the project workspace"),
            ("/model [NAME]", "Show or change the configured model name"),
            ("/tools [QUERY]", "Inspect available bridge tools, optionally filtered"),
            ("/takeover", "Arm the CLI and wait for a browser handoff"),
            ("/stop", "Request a safe cooperative stop"),
            ("/return-browser", "Return browser control when the CLI owns the task"),
            ("/clear", "Clear terminal output"),
            ("/quit", "Exit the CLI"),
        ]
        self.table("Command reference", ("Command", "Description"), rows)
        self.print("Enter submits the prompt. Esc then Enter inserts a new line.", "muted")
