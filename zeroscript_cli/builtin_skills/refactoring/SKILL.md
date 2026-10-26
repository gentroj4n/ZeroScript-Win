---
name: refactor
description: Simplify and organize code while preserving externally observable behavior.
argument-hint: [module, subsystem, or concern]
---
Refactor around a clear behavioral contract.

1. Read call sites, public interfaces, fixtures, and tests before restructuring.
2. Name the code smell and define what behavior must remain unchanged.
3. Prefer cohesive modules and narrow interfaces over broad rewrites or new abstraction layers without a concrete benefit.
4. Preserve error behavior and compatibility unless the task explicitly changes them.
5. Add characterization or regression tests before risky transformations.
6. Run relevant tests and inspect the complete diff, including files changed indirectly.
