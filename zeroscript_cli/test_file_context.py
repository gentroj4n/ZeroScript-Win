# SPDX-License-Identifier: GPL-3.0-or-later
import tempfile
import unittest
from pathlib import Path

from zeroscript_cli.file_context import expand_file_mentions


class FileContextTests(unittest.TestCase):
    def write(self, root: Path, relative: str, text: str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_attaches_workspace_file_and_preserves_user_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write(root, "src/main.py", "def main():\n    return 42\n")
            result = expand_file_mentions("Review @src/main.py and suggest a fix.", root)
            self.assertIn("Review [attached file: src/main.py] and suggest a fix.", result.prompt)
            self.assertIn("def main():", result.prompt)
            self.assertEqual(result.attached, ("src/main.py",))
            self.assertEqual(result.skipped, ())

    def test_supports_quoted_paths_with_spaces_and_deduplicates_references(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write(root, "docs/Design Notes.md", "Design constraints.\n")
            result = expand_file_mentions('Compare @"docs/Design Notes.md" with @"docs/Design Notes.md".', root)
            self.assertEqual(result.attached, ("docs/Design Notes.md",))
            self.assertEqual(result.prompt.count("Design constraints."), 1)
            self.assertIn("[attached file: docs/Design Notes.md]", result.prompt)

    def test_leaves_missing_and_outside_workspace_paths_unexpanded(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "repo"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_text("outside material", encoding="utf-8")
            result = expand_file_mentions("Check @missing.py and @" + str(outside) + ".", root)
            self.assertIn("@missing.py", result.prompt)
            self.assertIn("@" + str(outside), result.prompt)
            self.assertEqual(result.attached, ())

    def test_skips_secret_files_binary_files_and_large_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("API_KEY=example", encoding="utf-8")
            binary = root / "image.bin"
            binary.write_bytes(b"\x00\x01\x02")
            large = root / "large.txt"
            large.write_text("x" * (32 * 1024 + 1), encoding="utf-8")
            result = expand_file_mentions("Inspect @.env, @image.bin, and @large.txt.", root)
            self.assertEqual(result.attached, ())
            self.assertNotIn("API_KEY=example", result.prompt)

    def test_applies_file_count_and_total_byte_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write(root, "a.txt", "alpha")
            self.write(root, "b.txt", "bravo")
            result = expand_file_mentions("@a.txt @b.txt", root, max_files=1)
            self.assertEqual(result.attached, ("a.txt",))
            self.assertIn("@b.txt", result.prompt)
            self.assertEqual(result.skipped, ("b.txt",))

    def test_rejects_symlinked_files_when_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            external = Path(temp) / "external.txt"
            external.write_text("outside", encoding="utf-8")
            try:
                (root / "linked.txt").symlink_to(external)
            except (OSError, NotImplementedError):
                self.skipTest("Symlink creation is unavailable in this environment")
            result = expand_file_mentions("@linked.txt", root)
            self.assertEqual(result.attached, ())
            self.assertIn("@linked.txt", result.prompt)


if __name__ == "__main__":
    unittest.main()
