# Skills and custom commands

ZeroScript supports the plain-Markdown skill format used by Claude Code, plus project/user scoped command files. Definitions are data: their Markdown instructions are added to a task or supplied as model context; Python or shell inside a definition is never executed directly.

## Where definitions live

Project definitions take precedence over user definitions with the same name.

| Purpose | Project scope | User scope |
|---|---|---|
| Skills | \`.claude/skills/<name>/SKILL.md\` or \`.zeroscript/skills/<name>/SKILL.md\` | \`~/.claude/skills/<name>/SKILL.md\` or \`~/.zeroscript-win/skills/<name>/SKILL.md\` |
| Custom commands | \`.claude/commands/<name>.md\` or \`.zeroscript/commands/<name>.md\` | \`~/.claude/commands/<name>.md\` or \`~/.zeroscript-win/commands/<name>.md\` |

Nested names are supported. For example, \`.claude/commands/review/security.md\` is invoked as \`/review/security\`.

## Example skill

Create \`.claude/skills/review/SKILL.md\`:

\`\`\`markdown
---
name: review
description: Review changed code for bugs, regressions, and missing tests.
argument-hint: [files or scope]
user-invocable: true
disable-model-invocation: false
---

Review $ARGUMENTS.

1. Inspect the repository guidance and current diff.
2. Trace changed behavior through its callers.
3. Identify correctness, security, and compatibility risks.
4. Run focused tests and report exact results.
5. Prioritize findings by severity and cite file/line locations.
\`\`\`

Run it using \`/review src/file.py\`, \`/skill review src/file.py\`, or \`zeroscript skill review src/file.py\`. \`$ARGUMENTS\` and positional placeholders \`$1\` through \`$9\` are expanded as plain text. Definition bodies are not templated as Python or shell.

## Example custom command

Create \`.claude/commands/test.md\`:

\`\`\`markdown
---
description: Run the project's focused tests.
argument-hint: [test selector]
---

Inspect the repository's test configuration, then run the most relevant tests for $ARGUMENTS. If no selector is provided, determine the smallest appropriate test suite. Report commands and actual exit codes.
\`\`\`

Invoke it with \`/test zeroscript_cli\` or \`zeroscript test zeroscript_cli\`.

## Commands

- \`/skills [query]\` lists discovered skills.
- \`/skill NAME [args]\` invokes an explicitly named skill.
- \`/commands [query]\` lists custom Markdown commands.
- Any discovered user-invocable skill or command can be invoked as \`/name args\`.
- \`/init\` creates a starter \`CLAUDE.md\` in the selected workspace without overwriting an existing file.
- \`zeroscript skills\` and \`zeroscript commands\` list definitions in the current CLI workspace. Use the interactive commands to inspect the selected bridge workspace.

## Project guidance

The CLI loads all available guidance files from the selected project, in this order: \`CLAUDE.md\`, \`AGENTS.md\`, \`ZEROSCRIPT.md\`, and \`.zeroscript/rules.md\`. The combined context is bounded to prevent instruction files from exhausting the model window. User-level and project-level skill and command metadata is exposed to the model; definition bodies are only loaded when explicitly invoked or read as relevant.

Keep definitions short, specific, and verifiable. Use \`description\` to explain when a skill should be applied. \`disable-model-invocation: true\` omits that definition from the model-facing catalogue but still permits explicit user invocation. \`user-invocable: false\` hides the skill from user slash-command dispatch. The currently supported frontmatter fields are \`name\`, \`description\`, \`argument-hint\`, \`user-invocable\`, \`disable-model-invocation\`, and \`allowed-tools\`.
