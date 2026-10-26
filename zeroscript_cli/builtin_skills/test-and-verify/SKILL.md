---
name: test-and-verify
description: Select meaningful tests for a change and report evidence-backed results.
argument-hint: [feature or test path]
---
Build a verification plan tied to the behavior being changed.

1. Identify the code path, existing tests, and relevant test commands from the repository configuration and documentation.
2. Prefer the smallest regression test that directly exercises the behavior, then run the relevant suite.
3. Include negative cases, boundary conditions, malformed inputs, and failure recovery where appropriate.
4. Capture real command output and exit status. Distinguish tests that passed from tests that could not run.
5. Do not weaken or delete a test merely because it fails. Diagnose the mismatch and determine whether code or expectation is wrong.
6. Finish with a concise evidence table: check, result, and any remaining gap.
