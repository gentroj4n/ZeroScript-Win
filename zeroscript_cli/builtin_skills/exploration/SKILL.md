---
name: exploration
description: Map an unfamiliar repository, identify important architecture, and locate the right implementation points.
argument-hint: [topic, subsystem, or goal]
---
Explore before editing.

1. Establish repository root, languages, package entry points, build/test commands, and working-tree state when available.
2. Find relevant files by symbols, imports, routes, tool registrations, and tests rather than reading every file.
3. Trace important data flow and dependencies across call sites.
4. Cite exact file paths and symbols; distinguish observed facts from assumptions.
5. Do not modify project files during exploration unless the user explicitly asks for implementation in the same task.
6. Return an architecture map, likely implementation points, relevant tests, and open questions.
