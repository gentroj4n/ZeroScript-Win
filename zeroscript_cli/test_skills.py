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


if __name__ == "__main__":
    unittest.main()
