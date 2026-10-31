# Agent Prompt Remaster

## Purpose

The ArcScript browser agent must carry authorized implementation work through inspection, changes, verification, and an evidence-based report. The system prompt should reinforce task ownership without pretending the model can directly dispatch tools or bypass the extension's execution protocol.

## Findings from the existing runtime

The extension already has a sequential agent loop in `zeroscript-extension/core/main.js`:

1. `agentLoop()` waits for a model response.
2. A single parsed tool call is dispatched through `runTool()`.
3. The tool result is injected back into the provider conversation.
4. The loop continues and waits for the model's next response.

The parser/runtime intentionally rejects multiple tool calls in one assistant reply. This is a protocol constraint, not the same thing as ending the task after one tool call. The prompt must tell the model to make one call, use the returned evidence, and continue in subsequent turns while useful work remains.

The runtime also has deliberate terminal conditions for user Stop, unavailable or invalidated extension contexts, response timeouts, context limits, undelivered tool results, and final model prose. Those conditions should not be removed indiscriminately: the prompt must cooperate with the real runtime, while genuine continuation failures should be diagnosed in the loop itself.

## Prompt changes

The system prompt in `zeroscript-extension/core/config.js` now adds:

- **Task ownership and continuation:** A tool call is an intermediate action, not completion. Continue until the request is verified, blocked, stopped, or prevented by an execution/environment limit.
- **Evidence-driven recovery:** Read actual tool errors, change the approach when retrying, and avoid claims unsupported by tool results.
- **Completion criteria:** Compare the original request with the implementation and verification evidence before finalizing.
- **Context recovery:** Preserve goal, workspace/branch, acceptance criteria, completed and verified steps, failures, remaining work, and next action.
- **Protocol-aware serialization:** Explicitly states that one call per reply is required by the current extension protocol and does not justify stopping after one action.
- **A fuller implementation lifecycle:** Orient, define completion, plan, implement, verify, recover, audit, and report.

The existing workspace onboarding and bootstrap restrictions remain intact. In particular, an unconfigured workspace must not be inspected before the user chooses a project folder.

## Verification

The prompt regression test in `zeroscript-extension/core/config.test.cjs` asserts the new task-ownership and protocol-compatibility language, in addition to the existing tool catalogue and bootstrap checks.

Run locally from the repository root:

```powershell
node zeroscript-extension/core/config.test.cjs
```

## Follow-up runtime tests

The prompt update alone cannot prove that every provider reliably resumes after a tool result. Add or extend integration tests around `agentLoop()` to verify:

1. One tool call returns a result and the model is asked for its next turn without another user message.
2. A second dependent tool call executes only after the first result has been delivered.
3. A tool failure can be diagnosed and followed by a different action.
4. Stop, stale extension context, undelivered result, and response timeout remain terminal.
5. A final answer ends the loop only after the model decides the task is complete or blocked.
6. Multi-call replies are rejected with a corrective message rather than partially executed.

These tests should use controlled provider/bridge mocks where possible; they should not depend on a live AI website for deterministic unit coverage.
