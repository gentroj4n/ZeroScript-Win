---
name: debugging
description: Diagnose a reproducible bug, trace its cause, and make a minimal verified fix.
argument-hint: [error, symptom, or reproduction]
---
Debug the reported problem systematically.

1. Reproduce or capture the actual failure before changing code. Keep the exact error and its relevant context.
2. Trace from the failing entry point through the affected call path and inspect adjacent code/tests.
3. Form a specific cause hypothesis and test it. Do not guess at fixes based only on a similar-looking error.
4. Make the smallest coherent change that addresses the cause without disabling unrelated functionality.
5. Add a regression test that would fail before the fix.
6. Run the narrowest relevant test first, then broader checks when practical. Inspect the final diff for accidental changes.
7. Report the cause, changed behavior, and exact checks run. Never say "fixed" if the failure was not verified.
