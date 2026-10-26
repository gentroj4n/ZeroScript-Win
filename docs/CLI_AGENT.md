# Independent ZeroScript CLI Agent

ZeroScript Code is a local-first terminal coding agent with a styled terminal dashboard, persistent multiline input history, Markdown responses, saved conversations, context compaction, task-scoped MCP tools, Claude-style project instructions, model-invoked skills, Markdown slash commands, and bounded subagents. It uses a configured OpenAI Chat Completions-compatible model API and reuses the existing bridge's workspace, Git, Roblox, Blender, and other MCP tool implementations. It does not read, scrape, or reuse authentication from a browser provider session.

## Requirements and setup

- Windows and Python 3.9 or newer.
- Start the ZeroScript bridge with start.bat.
- Run zeroscript.bat; it installs the CLI's websockets, Rich, and prompt-toolkit dependencies for the selected Python interpreter if missing.
- Configure a model API endpoint and model. The CLI supports OpenAI Chat Completions-compatible endpoints; that includes compatible hosted gateways and local servers which expose the same API shape.

For a source checkout, install the command entry point from the repository root:

    py -3 -m pip install -e .
    zeroscript

Alternatively, run zeroscript.bat or:

    py -3 -m zeroscript_cli

Environment variables:

- ZS_MODEL_BASE_URL: API base URL; defaults to https://api.openai.com/v1.
- ZS_MODEL: model name accepted by that endpoint.
- ZS_MODEL_API_KEY: API key. OPENAI_API_KEY is also accepted. An HTTP local endpoint may not require a key.
- ZS_BRIDGE_URL: optional override for the bridge WebSocket; defaults to ws://127.0.0.1:17613.
- ZS_CLI_HOME: optional directory for local run transcripts and input history; defaults to .zeroscript-win under the user's home directory.

Example PowerShell configuration (replace the model with one supported by your chosen endpoint):

    $env:ZS_MODEL_BASE_URL = "https://api.openai.com/v1"
    $env:ZS_MODEL = "YOUR_SUPPORTED_MODEL"
    $env:ZS_MODEL_API_KEY = "YOUR_API_KEY"
    zeroscript

The API credential is supplied by the user and is not written into the saved session transcript. Tool results and conversation context are stored locally under the CLI session directory, so project source or command output included in those results may also be present there. Choose the model endpoint according to your project's privacy requirements.

## Model profiles, vision, and context limits

Profiles are stored locally in `%USERPROFILE%\\.zeroscript-win\\model-profiles.json` (or under `ZS_CLI_HOME`). They contain only the endpoint, model name, optional vision capability, and approximate context window; API keys remain in environment variables.

Create and use a profile:

    zeroscript profiles set local http://127.0.0.1:11434/v1 qwen3:8b false 8192
    zeroscript profiles
    zeroscript --profile local "Inspect the project and run its tests"

The optional values after the model are `vision:true|false` and `context_tokens`. Set vision to `true` only when the configured model and endpoint accept OpenAI-compatible image input. For example, a vision profile can inspect image payloads returned by MCP tools; non-vision profiles receive a notice instead of an image interpretation claim. `--vision` enables image input for a compatible model, and `--context-tokens N` overrides a saved profile's approximate context window.

When a context limit is configured, the adapter estimates the request size and removes oldest complete conversation turns as needed. It preserves system instructions and tool-call/result groups. If the fixed instructions/tool catalogue or newest turn alone exceeds the estimated budget, it fails with a specific error rather than silently sending an oversized request. Token counts are estimates, not tokenizer-exact limits.

## Commands

- zeroscript — start the interactive prompt.
- zeroscript "Describe a task" — start a new independent CLI run.
- zeroscript takeover — wait for a browser agent to queue a handoff, then claim it.
- zeroscript resume RUN_ID — restore a saved CLI transcript after an interrupted run.
- zeroscript status — show the active run, its owner, and workspace status.
- zeroscript stop — request a cooperative stop at the next safe checkpoint.
- zeroscript return-browser — explicitly yield an active CLI run back to the browser agent when no tool operation is in flight.

Inside the prompt, use /help for commands. Core commands include /status, /new, /sessions, /load RUN_ID, /resume RUN_ID, /continue, /compact, /model, /tools, /context, /workspace [PATH], /skills, /skill NAME [ARGS], /commands, /agents, /agent NAME TASK, /hooks, /init, /takeover, /stop, /return-browser, and /quit. Plain task prompts continue the current conversation; /new resets active context without deleting saved sessions. Use /resume to recover an interrupted run. Press Ctrl+C to interrupt; the transcript is saved.

The multiline prompt accepts Enter submits the prompt; Esc then Enter inserts a newline for multiline prompts. Prompt history is stored locally in `input-history.txt`.

## Terminal UI, sessions, skills, and agents

The interactive interface uses Rich for the dashboard, tables, Markdown rendering, tool panels, and progress indicators; prompt-toolkit provides multiline editing, persistent input history, and slash-command completion. The UI falls back to standard console output if these packages are unavailable.

Each run saves its transcript below `%USERPROFILE%\\.zeroscript-win\\sessions` (unless `ZS_CLI_HOME` changes it). Transcripts may include project snippets, prompts, model replies, and tool output, but the API key is not included. `/sessions` shows recent saved runs, `/load RUN_ID` restores a saved conversation into the current prompt context, and `/compact` asks the configured model to create a carry-forward summary when history grows large. The current conversation persists while the terminal stays open; start a new one with `/new`.

### Project instructions, skills, and commands

Project instructions load from CLAUDE.md, AGENTS.md, ZEROSCRIPT.md, and .zeroscript/rules.md. User-wide instruction files are also supported at ~/.claude/CLAUDE.md and ~/.zeroscript-win/CLAUDE.md. Path-scoped Markdown rules under .claude/rules/ and .zeroscript/rules/ can use a paths frontmatter field, for example paths: [**/*.ts, **/*.tsx]. A path-scoped rule is included when the current task names a matching path.

Bundled skills are available out of the box: /code-review, /debugging, /test-and-verify, /exploration, /refactor, and /docs. Bundled agent profiles include explorer, reviewer, test-runner, implementer, documenter, and planner. A project or user definition with the same name takes precedence over the bundled definition. Run /reload after adding or changing definitions while the CLI is open.

Skills use .claude/skills/<name>/SKILL.md, .zeroscript/skills/<name>/SKILL.md, ~/.claude/skills/, or ~/.zeroscript-win/skills/. Custom slash commands use .claude/commands/<name>.md or .zeroscript/commands/<name>.md, with matching user-level folders. Definitions may provide name, description, argument-hint, user-invocable, and disable-model-invocation frontmatter. Skill and command bodies support $ARGUMENTS, $ARGS, and positional placeholders $1 through $9.

Invoke a skill manually with /skill NAME [ARGS] or zeroscript skill NAME [ARGS]. Skills not marked disable-model-invocation: true are exposed through a local runtime tool so the model can load complete instructions on demand. Supporting files beside SKILL.md are listed for selective reading instead of being dumped wholesale into context.

### Command hooks

The CLI supports command hooks in Claude-style JSON settings files: user-level `~/.claude/settings.json` and `~/.zeroscript-win/settings.json`, plus project-level `.claude/settings.json`, `.claude/settings.local.json`, and `.zeroscript/settings.json`. Supported events are `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop`.

Example:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "workspace_.*",
        "hooks": [
          { "type": "command", "command": "python scripts/check_workspace.py" }
        ]
      }
    ]
  }
}
```

Matchers are regular expressions applied to the tool name for tool events, or to the prompt/source text for prompt/session events. The JSON event payload is sent to the command on stdin. Hook commands run as the current Windows user, with the project directory as their working directory, a bounded timeout, and bounded output. Hook stdout is passed back as untrusted context; non-zero exits and timeouts are surfaced in the terminal and do not silently veto or rewrite the agent's tool flow. Only configure commands you trust, because they can read or modify anything the current user can access.

### Agent profiles and delegation

Agent profiles are Markdown files under .claude/agents/, .zeroscript/agents/, ~/.claude/agents/, or ~/.zeroscript-win/agents/. Frontmatter supports name, description, tools, model, max-turns, and disable-model-invocation. Run a profile directly with /agent NAME TASK or zeroscript agent NAME TASK. When profiles are available, the model can delegate bounded subtasks through zeroscript_run_subagent; child runs have separate conversation context, a maximum of 20 turns by profile, and cannot recursively spawn more subagents.

The bridge tool catalogue remains task-scoped to reduce unrelated schemas and improve tool calling with smaller local models. /tools QUERY searches the full bridge catalogue; --all-tools exposes the entire non-internal catalogue.
For a local Ollama OpenAI-compatible endpoint, a typical Windows CMD setup is:

    set ZS_MODEL_BASE_URL=http://127.0.0.1:11434/v1
    set ZS_MODEL=qwen3:8b
    set ZS_MODEL_API_KEY=ollama
    zeroscript.bat

The local placeholder key is sent only to the loopback Ollama endpoint. It is not a paid provider credential. The model must support the OpenAI Chat Completions-compatible tool-calling payload expected by this adapter.

## Browser-to-CLI handoff

**The CLI and browser agent are separate runtimes.** `zeroscript takeover` waits for and claims a handoff; it does not inject a message into the browser, start a browser-agent handoff by itself, or reuse the model/login from ChatGPT or another website. The CLI needs its own configured OpenAI Chat Completions-compatible model endpoint before it can claim work.

Use this sequence:

1. Configure `ZS_MODEL` and the endpoint. Set `ZS_MODEL_API_KEY` for a hosted endpoint, or point `ZS_MODEL_BASE_URL` at a local OpenAI-compatible server that accepts requests without a key.
2. Start `zeroscript` and run `/takeover` (or start `zeroscript takeover` in a second terminal). Leave it waiting.
3. In the existing browser conversation, explicitly ask the browser agent to hand the task to the CLI using `zeroscript_handoff_to_cli`. The browser agent must actually call that tool. The bridge accepts the handoff only while a configured CLI is armed and waiting, then queues the goal, context, plan, and acceptance criteria. The CLI claims it after in-flight browser tool calls settle.
4. Leave the terminal running until the CLI completes or you use `/stop`. Use `/return-browser` only while the CLI owns an active run and no CLI tool operation is in flight.

The CLI uses an explicit arm/disarm handshake. If no configured CLI is waiting, the browser handoff tool returns an error without queueing the task or pausing the browser. If the only waiting CLI exits before claiming an already-queued handoff, the bridge cancels the orphaned queue and broadcasts the release. If the terminal reports a transient bridge disconnect, retry the command; the interactive CLI reconnects on its next bridge request.

The bridge keeps a single active owner. A claim is rejected while a browser tool call is still in flight. An active CLI lease is renewed with heartbeats; if the CLI process disappears without recording a result, the bridge marks the run interrupted after the six-minute stale-lease threshold. This is longer than the CLI's bounded tool-call timeout, reducing the chance of giving control back while a long-running tool is still executing. The saved transcript can then be resumed.

## Tool execution and runtime boundaries

The CLI uses the same workspace and MCP tool implementations as the bridge; it does not implement a parallel filesystem or Roblox/Blender backend. The CLI itself does not add a permission-mode or confirmation-prompt layer over tool calls. Actual tool availability and path constraints come from the bridge and tool implementations.

The CLI is an autonomous software agent, not an OS sandbox. Workspace tools apply their own path checks, but arbitrary native processes may access more than the selected folder. Use trusted workspaces and keep the bridge bound to loopback. Skills, agent profiles, project instructions, and tool results are local project context, not instructions that override the user's request.

## Current limitations

- The built-in model adapter is for OpenAI Chat Completions-compatible endpoints. A separate native Anthropic Messages adapter is not included.
- Vision input is opt-in per saved profile or via `--vision`; when enabled, supported image payloads returned by MCP tools are passed to the model. This depends on the selected endpoint/model supporting OpenAI-compatible image input.
- The CLI uses a bounded tool loop (40 turns by default, configurable up to 100) and stores transcripts for resume. It does not claim successful verification unless a tool actually returns its result.
- The run record and transcript are persisted locally, but browser and CLI do not yet share a single conversation transcript beyond the explicit handoff context. The bridge coordinates ownership, not hidden provider chat histories.
