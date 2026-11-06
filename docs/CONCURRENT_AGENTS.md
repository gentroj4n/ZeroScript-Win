# Concurrent agents

The bridge serves several agents at the same time: every browser provider tab, every ZeroScript CLI, and every CLI subagent is its own **session**. Sessions run tool calls in parallel and each keeps its own workspace root.

## Sessions and protocol

- Clients send a `session_id` and `protocol_version` (currently `2`) with their requests.
- A client that reports a different protocol version is rejected with an "update ZeroScript" error. Update the extension (1.14.0) and CLI (1.17.0) together with the bridge.
- A message without a session id is assigned a default session, so a stale extension tab keeps basic functionality instead of failing silently.
- Every workspace tool resolves paths against the calling session's workspace root (the `workspace_root` the extension sends per chat), not a bridge-wide root.

## File locks

- Writes, edits, moves, and deletes lock the files they touch for the duration of the call. Reads never lock.
- A writer that finds a file locked waits up to **60 seconds**, then fails with `File '<path>' is locked by agent <session>; gave up after 60s.`
- Change the wait with `ZS_FILE_LOCK_TIMEOUT` (seconds).

## MCP servers (Roblox Studio, Dominus, Blender, Discord)

- Calls on one MCP connection are multiplexed by request id.
- Known read-only tools run in parallel; every other tool is treated as mutating and runs one at a time per server.
- Blender and Discord currently process requests serially inside the server, so their parallel reads still queue there.

## Limits

| Setting | `config.json` key | Environment | Default |
| --- | --- | --- | --- |
| Tool calls in flight, all sessions | `max_inflight` | `ZS_MAX_INFLIGHT` | 16 |
| Tool calls in flight, per session | `max_inflight_per_session` | `ZS_MAX_INFLIGHT_PER_SESSION` | 4 |
| Seconds a call waits for a slot | `queue_timeout` | `ZS_QUEUE_TIMEOUT` | 120 |
| Parallel subagents per CLI | `--max-parallel-subagents` | `ZS_MAX_PARALLEL_SUBAGENTS` | 4 |

Environment variables override `config.json`. `0` disables a limit. Calls over a limit queue until a slot frees or the timeout expires.

## Browser → CLI handoff

- Any number of CLIs can be armed with `/takeover` at once. The agent-control ledger tracks each handoff as its own run (`run_id`, origin tab, target CLI session, workspace, state).
- `zeroscript_handoff_to_cli` accepts an optional `target_session_id`. Armed CLI ids are listed in `armed_cli_sessions` from `zeroscript_agent_control_status`, and in the popup's **CLI handoff** picker.
- Without a target, the bridge hands off to the longest-armed CLI on the same workspace. If none is armed there, the handoff is refused and the browser agent keeps control.
- Only the tab that handed off is paused while its run is out; other tabs and CLIs keep working.

## Parallel subagents

When a CLI model turn requests several subagents, they run in parallel up to the subagent limit, each as its own session. Other tool calls in the same turn run first, in order. Results are returned in the model's original order.
