# ArcScript — Local-First AI Development Environment

## Remaster reliability utilities

The CLI now exposes local diagnostics and project utilities: `zeroscript doctor`, `zeroscript index [PATH]`, `zeroscript gallery [PATH]`, `zeroscript worktrees [PATH]`, `zeroscript snapshot PATH FILE...`, and `zeroscript snapshot restore SNAPSHOT_DIR [ROOT] [--overwrite]`, `zeroscript profiles`, `zeroscript profiles set NAME BASE_URL MODEL`, `zeroscript --profile NAME`, `zeroscript events RUN_ID`, `zeroscript verify CRITERIA.json EVIDENCE.json`, `zeroscript worktrees create NAME [REPO_PATH] [BASE]`, and `zeroscript visual-diff BASELINE ACTUAL [THRESHOLD]`. Run `python -m unittest zeroscript_cli.test_remaster_services -v` to test the reliability primitives. These utilities do not require a model request; `doctor` reports bridge and local Ollama reachability. CLI dispatch validates tool argument schemas, while permission-mode and approval-prompt UI have been removed from the CLI. Browser Autopilot retains its separate role-aware policy. Saved model profiles support vision input and approximate context-window limits. Autopilot chooses among dependency-ready tasks by bounded priority.

ArcScript is a GPL-3.0-or-later, local-first, VS Code-inspired development environment powered through supported AI chat sites.

The extension preserves the existing browser-provider integrations and Roblox Studio capabilities. General project editing and Roblox Studio automation are separate domains: the local workspace layer works with Studio closed, while live Roblox tools use the explicit `roblox/` namespace.

**Current extension development version: 1.13.1.** The independent CLI package is version 1.16.0. Includes the remastered ZeroScript Code terminal experience alongside the existing browser-provider and Roblox Studio workflows. The CLI has persistent sessions, task-scoped MCP tools, bundled and custom skills, custom slash commands, agent profiles, bounded subagent delegation, session recovery, and a Rich terminal UI.

## Independent CLI coding agent

ZeroScript Code is an independent terminal coding runtime for local-first development. It reuses the existing workspace/Git/Roblox/Blender/MCP bridge while driving the model directly from the terminal instead of relying on a browser chat for every next step.

Features include a polished terminal dashboard, multiline prompt editor with history and completion, Markdown-rendered answers, model/tool progress indicators, persistent multi-turn conversations, local session browsing/loading, context compaction, task-scoped MCP tools, Claude-style project instructions and path-scoped rules, model-invoked skills, custom Markdown slash commands, agent profiles, and bounded subagent delegation. The Windows `zeroscript.bat` wrapper bootstraps the CLI's terminal UI dependencies. An Ollama-compatible endpoint can run on loopback without a hosted per-token API.

From the repository root, start `start.bat` for the bridge, then configure the model and run `zeroscript.bat`:

    set ZS_MODEL_BASE_URL=http://127.0.0.1:11434/v1
    set ZS_MODEL=qwen3:8b
    set ZS_MODEL_API_KEY=ollama
    zeroscript.bat

Use `/help` inside the prompt. Session commands include `/sessions`, `/load RUN_ID`, `/resume RUN_ID`, `/new`, and `/compact`. Bundled skills work immediately: `/code-review`, `/debugging`, `/test-and-verify`, `/exploration`, `/refactor`, and `/docs`; use `/skills` to list them and `/reload` after changing definitions. Project/user skills live at `.claude/skills/<name>/SKILL.md` (or compatible `.zeroscript/skills` and user-level folders). Custom Markdown commands live in `.claude/commands/`. Bundled agent profiles include `explorer`, `reviewer`, `test-runner`, `implementer`, `documenter`, and `planner`; custom profiles live in `.claude/agents/`. List profiles with `/agents`, run one with `/agent NAME TASK`, or let the model delegate bounded subtasks. Use `/hooks` to inspect configured lifecycle hooks. Project instructions load from `CLAUDE.md`, `AGENTS.md`, `ZEROSCRIPT.md`, and path-scoped `.claude/rules/*.md`. The browser and CLI remain separate runtimes: `/takeover` arms the CLI and waits for a browser agent to call `zeroscript_handoff_to_cli`. Several CLIs can be armed at once; a handoff goes to the CLI the agent or popup picks, or to any armed CLI on the same workspace, and only the handing-off tab pauses. Browser tabs, CLIs, and subagents all run concurrently with per-file write locks; see [Concurrent agents](docs/CONCURRENT_AGENTS.md).

The CLI also supports opt-in Claude-style command hooks via user/project `settings.json` files for session start, prompt submit, before/after tool use, and stop events. Hooks run as your current user, with bounded execution time; only configure commands you trust.\n\nSee [docs/CLI_AGENT.md](docs/CLI_AGENT.md) for setup, skills, agent profiles, privacy, and runtime limitations.

## Autopilot

The extension includes an opt-in bounded continuation toggle and a separate **Independent Autopilot run** coordinator in the popup. For a coordinated run, enter a goal and one acceptance criterion per line. ArcScript persists a dependency graph for project management, implementation, QA, code review, security, documentation, ideas & insights, and lead closeout.

The coordinator dispatches tasks to idle ArcScript sessions in open provider tabs. To get independent parallel workers, open multiple supported AI provider tabs, start the ArcScript agent in each, and leave the sessions idle. The scheduler leases tasks, tracks dependencies and evidence, retries worker failures within a bounded budget, recovers expired leases, and records blocked/unverified outcomes instead of treating a formatted answer as proof of success. The popup shows task state and can cancel the run; run state is stored locally and survives popup closure.

Worker sessions currently share the selected local workspace. Dispatch-boundary policy denies write/command tools to analysis and review roles, limits documentation edits to approved documentation paths, blocks destructive filesystem/Git operations, and serializes write-capable tasks while the workspace is shared. QA must cite an exact excerpt from a successful tool output for every acceptance criterion. These controls are not an OS sandbox; isolated Git worktrees and automatic patch conflict resolution remain unfinished. High-impact actions (publishing, deployment, destructive changes, Git state changes, workspace switching/moves, package installation, external communications, purchases, and credential/security changes) are blocked by policy in automated dispatch, including auto mode.
## Unity recovery toolchain

Operational sequence, tool contracts, limitations, and safety boundaries: [Unity Recovery Pipeline](docs/UNITY_RECOVERY_PIPELINE.md).

The selected workspace now exposes a first Unity recovery toolset:

- `scan_unity_installation` identifies Unity player metadata, detects Mono/IL2CPP from on-disk evidence, locates managed assemblies, summarizes large serialized asset files, and checks whether `Assets`, `Packages`, and `ProjectSettings` exist.
- `catalog_unity_assets` creates a deterministic, resumable file-level catalog in bounded batches. It records paths, extensions, categories, sizes, and timestamps without reading large asset payloads or modifying the installation.
- `inspect_unity_serialized_file` optionally uses UnityPy to inspect object IDs, Unity type names, names, and per-type counts in one selected serialized file. It rejects files over a configurable 256 MiB default / 512 MiB hard limit; it does not attempt to load the 1.64 GB `resources.assets` file wholesale.
- `update_reconstruction_feature` and `report_reconstruction_readiness` maintain a persistent, evidence-linked feature-parity inventory. Each subsystem can track decompiled source locators, asset files, browser implementation files, and regression tests; readiness reports gaps without pretending that recorded files prove tests passed.
- `inspect_recovery_tooling` reports whether ILSpy CLI, AssetRipper, Unity Editor, .NET, Node/npm, Git, and 7-Zip are available.
- `decompile_unity_type` and `export_unity_assembly` use `ilspycmd` when installed, with bounded timeouts and no shell execution. Assembly export refuses to overwrite a non-empty output directory.

These are the first recovery primitives, not a completed AssetRipper/Unity/Playwright pipeline. The catalog is file-level only; it does not yet parse serialized Unity object dependencies, extract assets, or prove browser feature parity. Tool installation is intentionally not automatic.

## Browser restoration QA

Two additional workspace tools support the browser port:

- `inspect_browser_port` audits `browser-port/index.html`, checks referenced local scripts/styles, reports missing files, and reads package scripts when present.
- `run_browser_qa` checks a running local URL over HTTP. If Python Playwright and Chromium are available, it also captures page errors, console errors, failed requests, page title, and a screenshot saved beneath the workspace. Optional bounded selector scenarios can click/fill/press, assert visibility/text, wait, and capture intermediate screenshots; interactive scenarios are localhost-only.
- `start_browser_server`, `browser_server_status`, and `stop_browser_server` manage a static server with a job ID, log file, and loopback-only `127.0.0.1` binding. The stop operation only terminates a process tracked by the current bridge instance.
- `start_assetripper_export`, `assetripper_job_status`, and `cancel_assetripper_job` manage an asynchronous export process with a durable job record, log file, timeout, output-directory protection, and cancellation when the current bridge owns the process. This requires an AssetRipper CLI-compatible executable at an explicit path; no download or installation occurs automatically.

The browser QA tool does not start the local server or install Playwright. Without Playwright, the result is explicitly an HTTP/static smoke test rather than a full browser regression test. It does not claim gameplay parity from a successful page load.

## Current status

**Workspace-first IDE and independent CLI remaster (v1.12.0).**

The bridge exposes an expanded, project-scoped toolset:

- Persistent local workspace selection, native folder picker exposed through the workspace tools, and a recent-project list for switching between saved workspaces.
- Project metadata, detected package manager, popular framework/tooling hints, package scripts, and source inventory.
- An agent-facing declared-task runner (`workspace_run_task`) that refuses scripts not present in the root `package.json`.
- Bounded file reads that preserve original CRLF/LF line endings, a dedicated `workspace_preview_image` tool that returns PNG/JPEG/WebP/GIF/SVG files as image content in the ArcScript preview panel (8 MB maximum), with SVG rasterization before provider upload, a depth-limited project tree, recursive file listing, glob search, regex/case-sensitive text search, and line/column matches.
- Source-file outlines for imports, headings, variables, and declarations, plus cross-language declaration/symbol discovery.
- Exact-match patch edits, validated multi-file edit batches, preview-first Replace in Files for literal or regex replacements, stale-preview protection, and complete-file writes that return unified diffs.
- Safe no-overwrite file/directory creation, rename/move, and single-file delete operations. Directory deletion and symbolic-link mutation are refused.
- Bulk `workspace_write_files` import for transferring source collected from Roblox Studio MCP into workspace-relative files, with preflight validation, overwrite protection by default, rollback on write failures, and bounded payload sizes.
- `roblox_export_tree` composite export: traverses the connected Studio DataModel, mirrors Script/LocalScript/ModuleScript source into hierarchy-preserving folders, and writes locally in bounded batches without sending the full source dump through the agent conversation.
  - Supports `exclude` hierarchy substrings and `max_script_bytes`; oversized scripts are skipped and reported instead of aborting the full export.
- Git status, structured changed-file listing, staged/unstaged diffs, branch listing, recent history, single-file stage/unstage, and confirmed local commits (no automatic push).
- A project-scoped workspace toolset exposed directly to the agent for file inspection/editing, search, project metadata, Git operations, task execution, and image preview. The `/workspace.html` editor opens from the popup's **Open Roblox UI Workspace** button.
- One-click project Test/Build/Lint/Check tasks discovered from package.json and package manager metadata, each with an explicit confirmation.
- Bounded command execution from the selected project directory using an argument array and `shell=False`.

## Choose a workspace

On a fresh install, the agent asks which project folder to use before inspecting or changing files. Provide an absolute folder path or ask the agent to open the native folder picker through the workspace tools. The extension popup displays the currently selected workspace; the `/workspace.html` editor is reachable from the popup (Open Roblox UI Workspace). The choice is saved in a private local `.zs-workspace.json` file and reused on subsequent starts.

An optional environment override is also supported:

```powershell
$env:ZS_WORKSPACE_ROOT = 'D:\Projects\MyApp'
py -3 bridge.py
```

The environment setting takes precedence over the saved folder. To change projects, ask the agent to use the workspace folder-selection tool or provide a new absolute path. The default fallback is the installation directory only for compatibility; the AI must not inspect it as a project until the user explicitly selects it.

## Two distinct tool domains

- **Workspace tools** (`workspace_*`) read and edit files on disk, inspect project structure, run relevant commands, and query Git.
- **Roblox Studio tools** (`roblox/*`) operate against the live Studio/DataModel through the optional Roblox MCP adapter.

The local workspace root and the open Roblox place are not interchangeable. Use each domain only for the actions it actually controls; use both only when a task needs both.

**Roblox MCP integrations are enabled by default.** The checked-in `config.json` keeps the existing `roblox` adapter and also registers **Dominus 2 / romcp** as `dominus` using `npx`. Dominus is a separate Studio plugin + local bridge: on first use, run `powershell -ExecutionPolicy Bypass -File .\\setup_dominus.ps1` from the repository root, follow its interactive setup, then open/reload Roblox Studio and verify the plugin reports connected. The installer uses the official `dominus-cli` npm package (now branded romcp) and its supported setup flow; it does not vendor or copy the closed-source implementation into this repository. The bridge launches configured MCP servers in parallel, so workspace tools and the existing Roblox adapter remain available independently. See [Dominus / romcp setup](docs/DOMINUS_MCP.md) for prerequisites, the 46-tool legacy catalog, current command names, and safety boundaries.

## Discord server management

The optional `discord` MCP server lets ZeroScript read and manage Discord guilds: channels, roles, permission overwrites, and member role assignments. It talks to the Discord REST API v10 directly over `urllib`, so it adds **no pip dependency**, and it is registered in `config.json` like any other MCP server.

The bot token is read only from the `DISCORD_BOT_TOKEN` environment variable — `config.json` holds a `$DISCORD_BOT_TOKEN` reference, never the secret itself — and is scrubbed from every tool response. Set `ZS_DISCORD_GUILD_IDS` to restrict the integration to specific guilds. Deleting a channel or role, and denying `VIEW_CHANNEL`/`SEND_MESSAGES`, require an explicit `confirm=true`. The `@everyone` role is immutable.

See [Discord MCP](docs/DISCORD_MCP.md) for bot setup, required permissions, the tool catalogue, and permission bitmasks.

## Run the workspace tests

Requires Python 3.10 or later.

```powershell
py -3 -m unittest workspace_agent.test_tools workspace_agent.test_bridge -v
py -3 -m unittest zeroscript_discord.test_mcp_server -v
node zeroscript-extension/core/config.test.cjs
node zeroscript-extension/workspace-diagnostics.test.cjs
node zeroscript-extension/workspace-editor-utils.test.cjs
```

## Security notes

The workspace layer checks resolved paths against the selected root, bounds text operations, and launches commands without shell expansion. These controls reduce accidental scope and shell-injection risk, but do **not** make arbitrary programs harmless or constitute a security sandbox. Command execution does not yet have a per-command approval dialog. Select only trusted folders and do not expose the local bridge beyond loopback.

See [Workspace Agent Remaster](docs/WORKSPACE_AGENT.md) for tool details and boundaries.

## Upstream project

Upstream attribution: ArcScript is derived from the [original ZeroScript Free project](https://github.com/sebattfg/ZeroScript-Free). Preserve original license and copyright notices and identify modifications made in this fork.

Licensed under [GNU GPL v3](LICENSE).


## Native Blender Python integration

The full remaster includes an optional native Blender `bpy` MCP integration. It runs Blender's Python module directly in a dedicated Python process rather than launching `blender.exe` for each task. Tools cover runtime status, scene inspection, script execution, `.blend` open/save, rendering, and model export.

See [Native Blender bpy integration](docs/BLENDER_BPY.md) for the Windows setup and tool configuration. Install the `bpy` wheel in a dedicated Python environment compatible with the published wheel, then merge the entry in `config.bpy.example.json` into the existing `config.json` `mcpServers` object. Preserve the existing Roblox MCP entry and all other configured servers. This integration is optional and does not replace the remaster's workspace or Roblox tools.


### Blender-to-Roblox asset pipeline

The project is licensed under [GNU GPL v3](LICENSE).


## Native Blender Python integration

An optional native `bpy` MCP server is available on the `feature/bpy-native-runtime`
branch. It imports Blender's Python module directly in a dedicated Python process
instead of launching `blender.exe` for each task. Tools include scene inspection,
script execution, `.blend` open/save, rendering, and mesh export.

See [Native Blender bpy integration](docs/BLENDER_BPY.md) for installation and
configuration. Keep the `bpy` wheel in a dedicated Python environment matching
its supported interpreter version, then merge the entry in
`config.bpy.example.json` into `config.json`. Do not replace existing MCP
entries. This is an optional integration; ordinary ZeroScript and Roblox tools
remain independent of `bpy`.
