---
name: code-review
description: Review a change for correctness, regressions, security, and maintainability.
argument-hint: [file, directory, or diff]
---
Perform a code review, not an implementation by default.

1. Inspect the relevant files, call sites, tests, and current diff before forming conclusions.
2. Look for concrete defects: incorrect behavior, edge cases, error handling, data loss, concurrency problems, insecure boundaries, and compatibility regressions.
3. Prioritize findings by severity. Each finding should include a file/path and line or symbol reference, the triggering scenario, and the likely impact.
4. Distinguish verified defects from questions and style preferences. Do not invent findings to fill a quota.
5. Run focused tests or use existing tool output when useful. If you cannot verify a concern, say so.
6. If no substantive issues are found, state that clearly and name the coverage limits.
