# ZeroScript-Win Remaster Reliability Services

This remaster adds reusable local-first services for:

- Bounded JSONL flight recording with sensitive-field redaction.
- Incremental SHA-256 project indexing and symbol/import search.
- Evidence-based acceptance-criteria verification.
- Conservative permission decisions for plan/default/auto modes.
- Isolated Git worktree creation and enumeration.
- Bounded TCP and command health probes.
- Workspace-relative file snapshots with traversal rejection.
- Model endpoint profiles that omit credential-like fields.
- Local artifact inventory with SHA-256 digests.
- Validation of common MCP JSON Schema argument fields.

CLI utilities: `zeroscript doctor`, `zeroscript index [PATH]`, `zeroscript gallery [PATH]`, `zeroscript worktrees [PATH]`, `zeroscript worktrees create NAME [REPO_PATH] [BASE]`, `zeroscript snapshot PATH FILE...`, `zeroscript verify CRITERIA.json EVIDENCE.json`, `zeroscript events RUN_ID`, `zeroscript visual-diff BASELINE ACTUAL [THRESHOLD]`, and `zeroscript profiles`. Save profiles with `zeroscript profiles set NAME BASE_URL MODEL [vision:true|false] [context_tokens]`, then select one with `zeroscript --profile NAME`.

The CLI flight recorder writes per-run events under the configured CLI session directory. Event payloads redact values when field names look sensitive. It is not a complete secret scanner, so do not include secrets in arbitrary log fields.

These helpers are not an operating-system sandbox and their presence does not imply every browser/bridge/Autopilot path uses the same policy. Full cross-runtime integration and end-to-end validation remain required before declaring the remaster complete.

Tests: `python -m unittest zeroscript_cli.test_remaster_services -v`.
