# ZeroScript-Win Remaster Implementation Checklist

This checklist distinguishes integrated behavior from helper-only work. CI status and browser end-to-end behavior are not marked successful without recorded test results.

- [x] Agent Flight Recorder with redaction and bounded event retention; wired to CLI model/tool lifecycle.
- [ ] Isolated Git worktrees for implementation agents; CLI worktree utilities exist, but browser Autopilot workers still share the selected workspace. Write-capable roles are serialized to avoid concurrent edits.
- [x] Evidence-based acceptance criteria: the extension QA worker must cite successful tool output for every criterion, and the CLI exposes an evidence verification gate.
- [ ] One executable permission-policy source shared by every runtime; CLI and bridge now call the same fail-closed dispatch validator, and browser Autopilot is checked against the same denylist fixture, but its role-aware policy remains a separate implementation.
- [x] Incremental project intelligence index and symbol/import search.
- [x] Saved model profiles with vision capability selection and context-window budgeting.
- [x] Integration health and bounded recovery diagnostics.
- [x] Workspace snapshots and hash-verified restore workflow with overwrite protection.
- [x] Artifact gallery with SHA-256 inventory and optional visual regression comparisons.
- [x] Recursive validation for common MCP JSON Schema constraints in CLI dispatch; the bridge also validates tool arguments before routing.
- [x] Bounded per-role Autopilot task priorities; ready tasks are sorted by priority after dependency checks.
- [ ] End-to-end browser/bridge/CLI scenarios for start, stop, resume, handoff, cancellation, and worktree lifecycle.
- [ ] Green CI on the current branch; recent Actions runs fail within seconds before any steps are recorded. GitHub reports no assigned runner, no steps, and no job logs, so these runs do not establish whether the code tests pass.

## Recent reliability work

- CLI schema validation now checks nested object requirements, array item schemas, numeric bounds, string lengths/patterns, enums, and unexpected properties.
- The CLI and local bridge call the same fail-closed dispatch validator for tool existence, schema validation, and hard-denied actions. Browser Autopilot and CLI denylist are checked against the same docs/permission-policy-cases.json fixture so dangerous-action drift is caught by regression tests.
- Bridge dispatch now rejects unknown tools and tools without an object input schema instead of silently skipping schema validation.
- Automatic mode still cannot override the shared irreversible-action denylist; the fixture is checked in both default and auto modes, including workspace switching/moves, Git state mutations, and package installation.
- CLI dispatch now refuses tools without a valid input schema; a missing schema is normalized to a strict no-argument contract rather than an arbitrary-argument escape hatch.
- Worktree utility tests exercise creation/listing in a temporary Git repository and reject nested targets and option-like base refs.
- Model profiles now apply the configured vision flag and context-token budget to the active provider.
- Worktree creation/listing remains a CLI service only; browser Autopilot has not yet routed each worker's tools into an isolated worktree or safely merged reviewed changes back into the base branch.

A feature is not complete merely because documentation or a helper module exists. Runtime call paths must be wired and validated, and environment-dependent checks must have actual results.
