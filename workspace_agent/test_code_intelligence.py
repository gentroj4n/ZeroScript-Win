# SPDX-License-Identifier: GPL-3.0-or-later
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_agent import code_intelligence as ci
from workspace_agent.unity_recovery import UnityRecoveryError


class CodeIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "recovery" / "decompiled").mkdir(parents=True)
        (self.root / "recovery" / "decompiled" / "StudentScript.cs").write_text(
            "class StudentScript {\n  void Update() { ScheduleManager.Tick(); }\n}\n",
            encoding="utf-8",
        )
        (self.root / "recovery" / "decompiled" / "ClockScript.cs").write_text(
            "class ClockScript {\n  void AdvanceTime() { }\n}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_source_search_returns_path_line_and_context(self):
        result = ci.search_unity_source(str(self.root), "ScheduleManager", context_lines=1)
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["path"], "recovery/decompiled/StudentScript.cs")
        self.assertEqual(result["matches"][0]["line"], 2)
        self.assertEqual(result["matches"][0]["context"][0]["text"], "class StudentScript {")

    def test_source_search_regex_and_case_sensitivity(self):
        result = ci.search_unity_source(str(self.root), r"void\s+\w+", regex=True)
        self.assertEqual(result["match_count"], 2)
        exact = ci.search_unity_source(str(self.root), "studentscript", case_sensitive=True)
        self.assertEqual(exact["match_count"], 0)

    def test_source_search_rejects_invalid_regex_and_missing_folder(self):
        with self.assertRaisesRegex(UnityRecoveryError, "Invalid search regex"):
            ci.search_unity_source(str(self.root), "[", regex=True)
        with self.assertRaisesRegex(UnityRecoveryError, "not found"):
            ci.search_unity_source(str(self.root), "x", source_path="missing")

    def test_source_search_rejects_path_escape(self):
        with self.assertRaises(UnityRecoveryError):
            ci.search_unity_source(str(self.root), "x", source_path="../outside")

    def test_list_symbols_uses_argument_array_and_filters(self):
        assembly = self.root / "Assembly-CSharp.dll"
        assembly.write_bytes(b"fake")
        fake = mock.Mock(returncode=0, stdout="StudentScript\nClockScript\nStudentManagerScript\n", stderr="")
        with mock.patch.object(ci, "_ilspy", return_value="ilspycmd"), mock.patch.object(ci.subprocess, "run", return_value=fake) as run:
            result = ci.list_unity_symbols(str(self.root), query="student", assembly_path=str(assembly))
        self.assertEqual(result["symbols"], ["StudentScript", "StudentManagerScript"])
        self.assertEqual(result["total_reported"], 3)
        self.assertEqual(run.call_args.args[0], ["ilspycmd", "-l", "t", str(assembly)])
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_list_symbols_validates_kind_and_missing_cli(self):
        with self.assertRaisesRegex(UnityRecoveryError, "symbol_kind"):
            ci.list_unity_symbols(str(self.root), symbol_kind="unknown")
        with mock.patch.object(ci.shutil, "which", return_value=None):
            with self.assertRaisesRegex(UnityRecoveryError, "ilspycmd is not installed"):
                ci.list_unity_symbols(str(self.root))


if __name__ == "__main__":
    unittest.main()
