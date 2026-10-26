# SPDX-License-Identifier: GPL-3.0-or-later
"""Claude Code-style project instructions, skills, and Markdown slash commands."""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import re
from pathlib import Path
from typing import Iterable

_MAX_DEFINITION_BYTES = 96 * 1024
_MAX_INSTRUCTIONS_BYTES = 40 * 1024
_MAX_ITEMS = 200
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")
_FRONTMATTER = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", re.S)


def _safe_read(path: Path, *, limit: int = _MAX_DEFINITION_BYTES) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text.strip()
    metadata: dict[str, str] = {}
    list_values: dict[str, list[str]] = {}
    active_list_key: str | None = None
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and active_list_key:
            value = stripped[2:].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", chr(34)}:
                value = value[1:-1]
            if value:
                list_values.setdefault(active_list_key, []).append(value)
            continue
        if ":" not in line:
            active_list_key = None
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace("_", "-")
        value = value.strip()
        if not value:
            active_list_key = key
            list_values.setdefault(key, [])
            continue
        active_list_key = None
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", chr(34)}:
            value = value[1:-1]
        metadata[key] = value
    for key, values in list_values.items():
        if values and key not in metadata:
            metadata[key] = "[" + ", ".join(values) + "]"
        elif not values:
            metadata.setdefault(key, "")
    return metadata, text[match.end():].strip()


def _truth(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _description(metadata: dict[str, str], body: str) -> str:
    result = metadata.get("description", "").strip()
    if not result:
        result = next((line.strip().lstrip("#").strip() for line in body.splitlines() if line.strip()), "")
    return re.sub(r"\s+", " ", result)[:240]


def _metadata_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    raw = value.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return tuple(part.strip().strip(chr(39) + chr(34)) for part in raw.split(",") if part.strip())


def _valid_name(name: str) -> bool:
    return bool(_NAME.fullmatch(name)) and not any(part in {".", ".."} for part in name.split("/"))


def _inside(path: Path, root: Path) -> bool:
    try:
        resolved_root = root.resolve()
        resolved_path = path.resolve()
        resolved_path.relative_to(resolved_root)
        current = path
        while current != root and current != current.parent:
            if current.is_symlink():
                return False
            current = current.parent
        return not path.is_symlink()
    except (OSError, ValueError, RuntimeError):
        return False


@dataclass(frozen=True)
class Definition:
    name: str
    description: str
    body: str
    path: Path
    source: str
    kind: str
    user_invocable: bool = True
    disable_model_invocation: bool = False
    argument_hint: str = ""
    allowed_tools: tuple[str, ...] = ()
    model: str = ""
    max_turns: int = 8

    def render(self, arguments: str = "") -> str:
        """Expand supported argument placeholders; never evaluate definition content."""
        args = arguments.strip()
        rendered = self.body.replace("$ARGUMENTS", args).replace("$ARGS", args)
        words = args.split()
        for index in range(min(9, len(words)), 0, -1):
            rendered = rendered.replace("$" + str(index), words[index - 1])
        header = [
            "Apply the following " + self.kind + " instructions as task-specific guidance.",
            "Definition: " + self.name,
            "Source: " + self.source + " (" + str(self.path) + ")",
        ]
        if args:
            header.append("Arguments: " + args)
        header.append("")
        return "\n".join(header) + rendered.strip()


class SkillCatalog:
    """Discover local skills and Markdown commands with project-over-user precedence."""

    def __init__(self, project_root: str | Path | None = None, home: str | Path | None = None):
        self.project_root = Path(project_root).expanduser().resolve() if project_root else None
        self.home = Path(home).expanduser() if home else Path.home()
        self._skills: list[Definition] = []
        self._commands: list[Definition] = []
        self._agents: list[Definition] = []

    def _roots(self, category: str) -> list[tuple[Path, str]]:
        project, user = [], []
        if self.project_root:
            project = [
                (self.project_root / ".claude" / category, "project:.claude"),
                (self.project_root / ".zeroscript" / category, "project:.zeroscript"),
            ]
        user = [
            (self.home / ".claude" / category, "user:.claude"),
            (self.home / ".zeroscript-win" / category, "user:.zeroscript-win"),
        ]
        bundled = {"skills": "builtin_skills", "agents": "builtin_agents"}.get(category)
        if bundled:
            user.append((Path(__file__).resolve().parent / bundled, "builtin"))
        return project + user

    def _scan(self, category: str, *, skill: bool, kind: str = "") -> list[Definition]:
        found: dict[str, Definition] = {}
        seen = 0
        for folder, source in self._roots(category):
            if not folder.is_dir() or folder.is_symlink():
                continue
            try:
                candidates: Iterable[Path]
                if skill:
                    candidates = sorted(folder.rglob("SKILL.md"))
                else:
                    candidates = sorted(folder.rglob("*.md"))
                for file in candidates:
                    seen += 1
                    if seen > _MAX_ITEMS:
                        return list(found.values())
                    if not _inside(file, folder):
                        continue
                    raw = _safe_read(file)
                    if raw is None:
                        continue
                    metadata, body = _parse_frontmatter(raw)
                    if not body:
                        continue
                    relative = file.parent.relative_to(folder).as_posix() if skill else file.relative_to(folder).with_suffix("").as_posix()
                    name = metadata.get("name", relative if skill else relative)
                    if not _valid_name(name):
                        continue
                    if skill and file.name.lower() != "skill.md":
                        continue
                    if not skill and not relative:
                        continue
                    key = name.casefold()
                    if key in found:
                        continue
                    found[key] = Definition(
                        name=name,
                        description=_description(metadata, body),
                        body=body,
                        path=file,
                        source=source,
                        kind=kind or ("skill" if skill else "custom command"),
                        user_invocable=_truth(metadata.get("user-invocable"), True),
                        disable_model_invocation=_truth(metadata.get("disable-model-invocation"), False),
                        argument_hint=metadata.get("argument-hint", "")[:120],
                        allowed_tools=_metadata_list(metadata.get("allowed-tools") or metadata.get("tools")),
                        model=metadata.get("model", "")[:80],
                        max_turns=max(1, min(20, int(metadata.get("max-turns", "8")))) if metadata.get("max-turns", "8").isdigit() else 8,
                    )
            except (OSError, ValueError, RuntimeError):
                continue
        return sorted(found.values(), key=lambda item: item.name.casefold())

    def skills(self, *, refresh: bool = False) -> list[Definition]:
        if refresh or not self._skills:
            self._skills = self._scan("skills", skill=True)
        return list(self._skills)

    def commands(self, *, refresh: bool = False) -> list[Definition]:
        if refresh or not self._commands:
            self._commands = self._scan("commands", skill=False)
        return list(self._commands)

    def find_skill(self, name: str) -> Definition | None:
        needle = name.strip().lstrip("/").casefold()
        return next((item for item in self.skills() if item.name.casefold() == needle), None)

    def find_command(self, name: str) -> Definition | None:
        needle = name.strip().lstrip("/").casefold()
        return next((item for item in self.commands() if item.name.casefold() == needle), None)

    def agents(self, *, refresh: bool = False) -> list[Definition]:
        if refresh or not self._agents:
            self._agents = self._scan("agents", skill=False, kind="agent profile")
        return list(self._agents)

    def find_agent(self, name: str) -> Definition | None:
        needle = name.strip().lstrip("/").casefold()
        return next((item for item in self.agents() if item.name.casefold() == needle), None)

    def instructions(self, task_text: str = "") -> list[tuple[str, str]]:
        """Load user/project instructions and matching path-scoped rules within a size budget."""
        output: list[tuple[str, str]] = []
        total = 0
        base_files: list[tuple[Path, Path, str]] = [
            (self.home / ".claude" / "CLAUDE.md", self.home, "~/.claude/CLAUDE.md"),
            (self.home / ".zeroscript-win" / "CLAUDE.md", self.home, "~/.zeroscript-win/CLAUDE.md"),
            (self.home / ".zeroscript-win" / "rules.md", self.home, "~/.zeroscript-win/rules.md"),
        ]
        if self.project_root:
            base_files.extend([
                (self.project_root / "CLAUDE.md", self.project_root, "CLAUDE.md"),
                (self.project_root / "AGENTS.md", self.project_root, "AGENTS.md"),
                (self.project_root / "ZEROSCRIPT.md", self.project_root, "ZEROSCRIPT.md"),
                (self.project_root / ".zeroscript" / "rules.md", self.project_root, ".zeroscript/rules.md"),
            ])
        for file, root, label in base_files:
            if total >= _MAX_INSTRUCTIONS_BYTES:
                break
            if not _inside(file, root):
                continue
            raw = _safe_read(file, limit=_MAX_INSTRUCTIONS_BYTES)
            if not raw or not raw.strip():
                continue
            remaining = _MAX_INSTRUCTIONS_BYTES - total
            body = raw.strip()[:remaining]
            output.append((label, body))
            total += len(body)

        task_paths = {
            token.replace("\\", "/").strip("'\".,:;()")
            for token in re.findall(
                r"(?<![A-Za-z0-9_])(?:[A-Za-z0-9_@.*?-]+[/\\])*[A-Za-z0-9_@.*?-]+\.[A-Za-z0-9_-]+",
                task_text,
            )
        }
        rule_roots: list[tuple[Path, str]] = [
            (self.home / ".claude" / "rules", "user:.claude/rules"),
            (self.home / ".zeroscript-win" / "rules", "user:.zeroscript-win/rules"),
        ]
        if self.project_root:
            rule_roots.extend([
                (self.project_root / ".claude" / "rules", "project:.claude/rules"),
                (self.project_root / ".zeroscript" / "rules", "project:.zeroscript/rules"),
            ])
        seen = 0
        for folder, source in rule_roots:
            if total >= _MAX_INSTRUCTIONS_BYTES:
                break
            if not folder.is_dir() or folder.is_symlink():
                continue
            try:
                candidates = sorted(folder.rglob("*.md"))
            except OSError:
                continue
            for file in candidates:
                seen += 1
                if seen > _MAX_ITEMS or total >= _MAX_INSTRUCTIONS_BYTES:
                    break
                if not _inside(file, folder):
                    continue
                raw = _safe_read(file, limit=16 * 1024)
                if raw is None:
                    continue
                metadata, body = _parse_frontmatter(raw)
                if not body:
                    continue
                patterns = _metadata_list(metadata.get("paths"))
                if patterns:
                    normalized_paths = {value.lstrip("./") for value in task_paths if value}
                    matching = any(
                        fnmatch.fnmatchcase(target, pattern.replace("\\", "/").lstrip("./"))
                        or fnmatch.fnmatchcase(target.rsplit("/", 1)[-1], pattern.replace("\\", "/").lstrip("./"))
                        for target in normalized_paths
                        for pattern in patterns
                    )
                    if not matching:
                        continue
                remaining = _MAX_INSTRUCTIONS_BYTES - total
                label = source + "/" + file.relative_to(folder).as_posix()
                block = body[:remaining]
                if patterns:
                    block = "Applies to paths: " + ", ".join(patterns) + "\n\n" + block
                output.append((label, block))
                total += len(block)
        return output

    def catalog_prompt(self, *, limit_chars: int = 8000, include_agents: bool = True) -> str:
        """Compact metadata for model-driven skill selection; definition bodies are not auto-loaded."""
        lines = ["Available project/user skills, custom commands, and agent profiles (load only relevant definitions when needed):"]
        items = self.skills() + self.commands()
        if include_agents:
            items += self.agents()
        for item in items:
            if item.disable_model_invocation:
                continue
            lines.append(
                "- " + item.kind + " /" + item.name + ": " + (item.description or "(no description)")
                + " [definition: " + str(item.path) + "]"
            )
        if len(lines) == 1:
            return ""
        result = "\n".join(lines)
        if len(result) > limit_chars:
            result = result[:limit_chars].rsplit("\n", 1)[0] + "\n- (catalog truncated)"
        return result
    def agent_tool(self) -> dict[str, object] | None:
        """Return a synthetic runtime tool for a bounded delegated agent run."""
        agents = [item for item in self.agents() if not item.disable_model_invocation]
        if not agents:
            return None
        return {
            "name": "zeroscript_run_subagent",
            "server": "zeroscript-runtime",
            "description": (
                "Delegate a distinct, well-scoped task to one local agent profile, then synthesize its findings. "
                "This starts a bounded child model/tool loop. Available profiles:\n"
                + "\n".join("- " + item.name + ": " + (item.description or "(no description)") for item in agents[:80])
            )[:8000],
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": [item.name for item in agents[:_MAX_ITEMS]]},
                    "task": {"type": "string", "minLength": 1, "maxLength": 8000},
                },
                "required": ["name", "task"],
                "additionalProperties": False,
            },
        }

    def model_tool(self) -> dict[str, object] | None:
        """Return a synthetic function tool that lets the model explicitly load a skill."""
        skills = [item for item in self.skills() if not item.disable_model_invocation]
        if not skills:
            return None
        skills = skills[:_MAX_ITEMS]
        catalog = "\n".join(
            "- " + item.name + ": " + (item.description or "(no description)")
            for item in skills
        )
        return {
            "name": "zeroscript_load_skill",
            "server": "zeroscript-runtime",
            "description": (
                "Load the full instructions for one relevant project/user skill before doing its task. "
                "This is a local runtime tool; it does not call the bridge. Available skills:\n" + catalog
            )[:8000],
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": [item.name for item in skills],
                        "description": "Exact skill name from the available skill list.",
                    },
                    "arguments": {
                        "type": "string",
                        "maxLength": 4000,
                        "description": "Optional task-specific arguments to substitute into the skill instructions.",
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        }

    def load_for_model(self, name: str, arguments: str = "") -> str:
        """Load one model-invocable skill and identify its bounded supporting files."""
        item = self.find_skill(name)
        if item is None or item.disable_model_invocation:
            raise ValueError("Skill is missing or disabled for model invocation: " + str(name))
        result = item.render(arguments)
        folder = item.path.parent
        supporting: list[str] = []
        try:
            candidates = sorted(folder.rglob("*"))
        except OSError:
            candidates = []
        for path in candidates:
            if len(supporting) >= 30:
                break
            if path == item.path or not path.is_file() or not _inside(path, folder):
                continue
            if any(part in {".git", "node_modules", "__pycache__", ".venv"} for part in path.parts):
                continue
            relative = path.relative_to(folder).as_posix()
            supporting.append(relative[:300])
        if supporting:
            result += (
                "\n\nSupporting files available beside this skill (read them only when relevant):\n"
                + "\n".join("- " + value for value in supporting)
            )
        return result

