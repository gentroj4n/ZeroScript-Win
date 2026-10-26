# SPDX-License-Identifier: GPL-3.0-or-later
import tempfile
import unittest
from pathlib import Path

from zeroscript_cli.skills import SkillCatalog


class SkillCatalogTests(unittest.TestCase):
    def write(self, root: Path, relative: str, body: str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def test_discovers_project_and_user_skills_with_project_precedence(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project, home = base / "repo", base / "home"
            self.write(home, ".claude/skills/review/SKILL.md",
                       "---\nname: review\ndescription: Global review\n---\nGlobal skill body")
            self.write(project, ".claude/skills/review/SKILL.md",
                       "---\nname: review\ndescription: Project review\n---\nProject skill body")
            self.write(project, ".zeroscript/skills/tests/SKILL.md",
                       "---\nname: tests\ndescription: Test workflow\n---\nRun the test suite.")
            catalog = SkillCatalog(project, home)
            skills = {item.name: item for item in catalog.skills()}
            self.assertEqual(skills["review"].source, "project:.claude")
            self.assertEqual(skills["review"].body, "Project skill body")
            self.assertIn("tests", skills)

    def test_discovers_nested_custom_commands_and_expands_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self.write(base, "repo/.claude/commands/review.md",
                       "---\ndescription: Review selected files\nargument-hint: <path>\n---\nReview $ARGUMENTS and inspect $1.")
            catalog = SkillCatalog(base / "repo", base / "home")
            command = catalog.find_command("review")
            self.assertIsNotNone(command)
            self.assertIn("Review src/main.py", command.render("src/main.py"))
            self.assertIn("inspect src/main.py", command.render("src/main.py"))

    def test_instructions_load_all_supported_project_guidance_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write(root, "CLAUDE.md", "Use small focused commits.")
            self.write(root, "AGENTS.md", "Run the tests.")
            self.write(root, ".zeroscript/rules.md", "Preserve existing APIs.")
            loaded = dict(SkillCatalog(root, root / "home").instructions())
            self.assertEqual(set(loaded), {"CLAUDE.md", "AGENTS.md", ".zeroscript/rules.md"})
            self.assertIn("Run the tests.", loaded["AGENTS.md"])

    def test_ignores_invalid_names_and_oversized_definitions(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self.write(base, "repo/.claude/skills/../escape/SKILL.md",
                       "---\nname: ../escape\n---\nunsafe")
            huge = "x" * (96 * 1024 + 1)
            self.write(base, "repo/.claude/skills/huge/SKILL.md", huge)
            catalog = SkillCatalog(base / "repo", base / "home")
            self.assertEqual(catalog.skills(), [])

    def test_catalog_prompt_omits_model_disabled_skills(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self.write(base, "repo/.claude/skills/private/SKILL.md",
                       "---\nname: private\ndescription: Only user invoked\ndisable-model-invocation: true\n---\nInstructions")
            self.write(base, "repo/.claude/skills/public/SKILL.md",
                       "---\nname: public\ndescription: Safe to recommend\n---\nInstructions")
            prompt = SkillCatalog(base / "repo", base / "home").catalog_prompt()
            self.assertIn("/public", prompt)
            self.assertNotIn("/private", prompt)


    def test_model_skill_tool_lists_only_model_invocable_skills(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self.write(base, "repo/.claude/skills/review/SKILL.md",
                       "---\nname: review\ndescription: Review code\n---\nReview the requested files.")
            self.write(base, "repo/.claude/skills/manual/SKILL.md",
                       "---\nname: manual\ndescription: User-only workflow\ndisable-model-invocation: true\n---\nOnly invoke manually.")
            catalog = SkillCatalog(base / "repo", base / "home")
            tool = catalog.model_tool()
            self.assertIsNotNone(tool)
            self.assertEqual(tool["name"], "zeroscript_load_skill")
            self.assertEqual(tool["server"], "zeroscript-runtime")
            self.assertEqual(tool["inputSchema"]["properties"]["name"]["enum"], ["review"])
            self.assertIn("Review code", tool["description"])

    def test_model_skill_loader_expands_arguments_and_lists_supporting_files(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self.write(base, "repo/.claude/skills/review/SKILL.md",
                       "---\nname: review\ndescription: Review code\n---\nReview $ARGUMENTS and focus on $1.")
            self.write(base, "repo/.claude/skills/review/references/checklist.md", "Checklist")
            catalog = SkillCatalog(base / "repo", base / "home")
            result = catalog.load_for_model("review", "src/main.py")
            self.assertIn("Review src/main.py", result)
            self.assertIn("focus on src/main.py", result)
            self.assertIn("references/checklist.md", result)

    def test_model_skill_loader_rejects_model_disabled_skill(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self.write(base, "repo/.claude/skills/private/SKILL.md",
                       "---\nname: private\ndisable-model-invocation: true\n---\nPrivate instructions.")
            catalog = SkillCatalog(base / "repo", base / "home")
            with self.assertRaises(ValueError):
                catalog.load_for_model("private")


    def test_discovers_agent_profiles_with_tool_hints_and_turn_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self.write(base, "repo/.claude/agents/reviewer.md",
                       "---\nname: reviewer\ndescription: Review code independently\ntools:\n  - Read\n  - Grep\n  - Bash\nmodel: inherit\nmax-turns: 12\n---\nReview the task and return evidence.")
            catalog = SkillCatalog(base / "repo", base / "home")
            profile = catalog.find_agent("reviewer")
            self.assertIsNotNone(profile)
            self.assertEqual(profile.kind, "agent profile")
            self.assertEqual(profile.allowed_tools, ("Read", "Grep", "Bash"))
            self.assertEqual(profile.max_turns, 12)
            tool = catalog.agent_tool()
            self.assertEqual(tool["name"], "zeroscript_run_subagent")
            self.assertEqual(tool["inputSchema"]["properties"]["name"]["enum"], ["reviewer"])

    def test_loads_path_scoped_rules_only_for_matching_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            self.write(root, ".claude/rules/typescript.md",
                       "---\npaths:\n  - \"**/*.ts\"\n  - \"**/*.tsx\"\n---\nUse TypeScript strict mode.")
            self.write(root, ".claude/rules/python.md",
                       "---\npaths: [\"**/*.py\"]\n---\nUse type hints.")
            self.write(root, ".claude/rules/global.md",
                       "---\ndescription: General repo rule\n---\nPrefer small focused changes.")
            loaded = dict(SkillCatalog(root, Path(temp) / "home").instructions("Update src/App.tsx"))
            self.assertIn("Use TypeScript strict mode.", loaded["project:.claude/rules/typescript.md"])
            self.assertNotIn("project:.claude/rules/python.md", loaded)
            self.assertIn("Prefer small focused changes.", loaded["project:.claude/rules/global.md"])

    def test_loads_user_global_claude_instructions(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self.write(base, ".claude/CLAUDE.md", "Prefer compact diffs.")
            loaded = dict(SkillCatalog(base / "repo", base).instructions())
            self.assertEqual(loaded["~/.claude/CLAUDE.md"], "Prefer compact diffs.")


if __name__ == "__main__":
    unittest.main()
