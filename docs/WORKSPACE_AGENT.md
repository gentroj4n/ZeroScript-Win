# Workspace agent remaster

ArcScript exposes a local-first coding workspace independently from its optional MCP integrations. The browser extension remains the agent UI and the loopback bridge owns local filesystem and process operations.

## First-run selection

- The first-run workspace falls back to the bridge directory for compatibility, but it is marked unconfigured.
- The bootstrap tool catalogue includes the current workspace path and its configured state in the `workspace_info` description. The agent uses that state to ask for a project folder before performing inspection or edits.
- The user can provide a full path through chat (`workspace_set_root`) or use the native operating-system folder dialog (`workspace_browse`). The picker returns a proposed path only; `workspace_set_root` activates and persists that exact path. The popup's Explorer launchpad exposes folder selection, recent-project switching, project overview, and Git status. It does not open a separate editor page; tool access stays in the bridge and agent workflow.
- The selected folder is persisted in `.zs-workspace.json` beside the bridge together with the last 10 recent project roots. This file is ignored by Git because it contains machine-specific paths.
- `ZS_WORKSPACE_ROOT`, when set, takes precedence over the saved folder.

## Workspace tool catalogue

| Tool | Purpose |
| --- | --- |
| `workspace_info` | Current root and explicit-selection status. |
| `workspace_recent_roots` | List the last 10 locally saved project roots and whether each folder still exists. |
| `workspace_open_recent` | Activate one saved recent root, only after the user explicitly selects it. |
| `workspace_set_root` | Validate and persist a folder explicitly supplied by the user. |
| `workspace_browse` | Open the native folder picker and return a candidate path without activating it. |
| `workspace_project_info` | Detect common manifests, project-type hints, package scripts, Git presence, and file-extension inventory. |
| `workspace_create_file` | Create a UTF-8 file without overwriting an existing path; its parent directory must already exist. |
| `workspace_create_directory` | Create a workspace-relative directory without overwriting an existing path. |
| `workspace_move_path` | Move/rename a file or directory within the workspace without overwriting a destination. |
| `workspace_delete_file` | Delete a single regular file; directories and symbolic links are refused. Use only after an explicit user request. |
| `workspace_tree` | Return a depth-limited project tree while skipping generated folders and symbolic links. |
| `workspace_list_files` | List project files while skipping common generated directories and symbolic links. |
| `workspace_find_files` | Find paths using glob patterns. |
| `workspace_read_file` | Read bounded UTF-8 text, optionally by 1-based line range. |
| `workspace_preview_image` | Return a workspace image as actual image content for vision-capable providers (PNG, JPEG, WebP, GIF, SVG; max 8 MB); do not use text reads for binary images. |
| `workspace_file_outline` | Extract import statements, headings, variables, and likely declarations with line numbers. |
| `workspace_search` | Folder-scoped text search with optional case sensitivity and regular expressions; matches include line and column. The Explorer defaults to the workspace root and accepts workspace-relative folder paths. |
| `workspace_symbols` | Best-effort discovery of function, type, and variable declarations across multiple language families. |
| `workspace_write_file` | Create or replace a UTF-8 file and return a unified diff. |
| `workspace_write_files` | Import up to 200 UTF-8 source files from another tool domain (such as Roblox Studio MCP) into workspace-relative paths in one batch. Validates all files before writing, protects existing files by default, supports explicit overwrite, and caps the payload at 10 MB. |
| `workspace_apply_patch` | Apply one exact source replacement; repeated matches are rejected unless the caller explicitly opts into replace-all. |
| `workspace_multi_edit` | Validate and apply a batch of exact replacements across multiple files, refusing ambiguous/missing matches before writes and returning per-file diffs. |
| `workspace_replace_in_files` | Preview literal/regex replacements across project files; apply only with the preview hash and after user confirmation, rejecting stale previews. |
| `workspace_git_status` | Read Git branch and working-tree status. |
| `workspace_git_changed_files` | List changed paths with staged, unstaged, untracked, added, deleted, or renamed state. |
| `workspace_git_diff` | Read staged or unstaged diffs, optionally for one path. |
| `workspace_git_branches` | List local and remote Git branches. |
| `workspace_git_stage_file` | Stage one file in the Git index; does not stage directories or commit. |
| `workspace_git_unstage_file` | Remove one file from the Git index without changing working-tree contents. |
| `workspace_git_commit` | Create a local commit from staged changes; does not push. |
| `workspace_git_log` | Read recent commit summaries. |
| `workspace_run_task` | Run a root package.json script using npm/pnpm/yarn/bun, or a whitelisted Python/Rust/Go/.NET/Maven/Gradle/CMake task. |
| `workspace_run_command` | Run argv-array commands from the selected root, without shell expansion and with bounded time/output. |

## Separate Roblox Studio domain

Roblox MCP tools are advertised under the `roblox/` namespace, while on-disk project operations use `workspace_*` names. This is a deliberate capability boundary:

- Roblox tools act on the live Studio/DataModel and may need an open place.
- Workspace tools act on local files and the selected folder, regardless of whether Studio is running.
- Tool names, working roots, output, and assumptions must not be mixed just because a task relates to Roblox.
- A Roblox project may use both domains, but the agent must decide explicitly whether it needs an on-disk edit, a live Studio operation, or both.

Legacy conversations calling bare Roblox tool names are routed to the matching Roblox server when the name is unambiguous. New prompts should always use the namespaced `roblox/` form.

## Safety and operational boundaries

- Workspace paths are resolved and checked against the selected root, including resolved symlinks.
- Text reads/writes and search results are bounded. Image previews accept PNG/JPEG/WebP/GIF/SVG, enforce an 8 MB limit, validate raster signatures, reject active/external SVG content, and remain inside the selected workspace. Common generated directories are excluded by default.
- Targeted patches require a non-empty exact fragment. Ambiguous matches are refused by default.
- Command execution uses argv arrays and `shell=False`, which avoids shell expansion but does not sandbox the selected executable.
- Git stage, unstage, and commit operations are separate tools. They should only be invoked after explicit user intent; the Workspace Explorer additionally asks for confirmation before these UI actions.
- The current foundation does not yet provide a per-command approval dialog for agent-issued tools. Treat command execution as powerful: select trusted workspaces and never expose the loopback bridge on a public interface.
- The native folder picker can depend on the local Python install having Tk support. If unavailable, the agent can use a folder path explicitly provided by the user.

## Tests

From the repository root:

```powershell
py -3 -m unittest workspace_agent.test_tools workspace_agent.test_bridge -v
```

## Explorer launchpad and independent CLI

The extension popup's Explorer launchpad provides native folder selection, recent-project switching, a project overview, and Git status. The separate Workspace Explorer/editor page is intentionally absent. Project files continue to be accessed through the selected-workspace tools.

For an independent model-driven terminal agent, see [Independent ZeroScript CLI Agent](CLI_AGENT.md). The CLI uses the same bridge and tool implementations, accepts a browser-to-CLI handoff, and holds an exclusive tool-execution lease while it owns the task. It does not read browser-session credentials or hidden provider conversations.


## Roblox Studio tree export

The bridge exposes `roblox_export_tree` when the Roblox integration is configured. It requires a selected local workspace and an active Roblox Studio MCP session.

- Uses Studio's `execute_luau` tool in Edit context to enumerate all `LuaSourceContainer` instances and read source in bounded pages.
- Exports `Script`, `LocalScript`, and `ModuleScript` files beneath a workspace-relative `root_path` (default: `RobloxExport`).
- Preserves the DataModel hierarchy as folders and uses `.server.lua`, `.client.lua`, and `.module.lua` suffixes to retain script roles.
- Writes locally through the workspace writer in bounded batches; script source is not copied into the agent conversation.
- Existing files are not overwritten unless the caller explicitly sets `overwrite=true`. The writer validates each batch before writing.
- Instance names are sanitized for Windows-compatible paths. Non-script instances and binary assets are not exported.

Example arguments: `{"root_path":"RobloxExport","overwrite":false}`.

The export is text-source mirroring, not a full place-file backup. It does not include instances, properties, assets, packages, or other non-script state.


Export options:

- `root_path`: workspace-relative output directory (default `RobloxExport`).
- `overwrite`: replace existing files only when explicitly true.
- `max_script_bytes`: maximum source size per script in UTF-8 bytes (default 1,048,576; accepted range 1 through 1,048,576). Oversized scripts are skipped before their source is included in the MCP response, then reported with hierarchy path, measured byte count, and limit. They do not abort the export.
- `exclude`: up to 100 case-insensitive substrings matched against slash-separated DataModel hierarchy paths. Matching scripts are excluded before source is read. For example, `["ServerStorage/Legacy", "Packages/"]`.

The result includes `skipped_oversized_count` and `skipped_oversized`, plus `skipped_excluded_count` and `skipped_excluded`, so skipped items are explicit rather than silently lost. Studio responses are paged in groups of 10 scripts to reduce transport-size failures.
