# SPDX-License-Identifier: GPL-3.0-or-later
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_agent import unity_recovery as ur


class UnityRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.install = self.root / "YandereSimulator"
        self.workspace.mkdir()
        self.install.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def _mono_fixture(self):
        data = self.install / "YandereSimulator_Data"
        managed = data / "Managed"
        managed.mkdir(parents=True)
        (managed / "Assembly-CSharp.dll").write_bytes(b"managed assembly placeholder")
        (managed / "Assembly-CSharp-firstpass.dll").write_bytes(b"first pass")
        (data / "globalgamemanagers").write_bytes(b"\x00Unity 6000.2.8f1\x00")
        (data / "resources.assets").write_bytes(b"x" * 1024)
        (data / "sharedassets0.assets").write_bytes(b"y" * 20)
        (self.install / "UnityPlayer.dll").write_bytes(b"player")
        return data, managed

    def test_scan_reports_mono_version_and_large_assets_without_mutation(self):
        data, managed = self._mono_fixture()
        report = ur.scan_unity_installation(str(self.install), str(self.workspace))
        self.assertEqual(report["scripting_backend"], "Mono")
        self.assertEqual(report["unity_version"], "6000.2.8f1")
        self.assertEqual(report["asset_file_count"], 2)
        self.assertEqual(report["managed_assemblies"][0]["size_bytes"], len(b"managed assembly placeholder"))
        self.assertFalse(report["editable_project_present"])
        self.assertTrue((data / "resources.assets").is_file())
        self.assertFalse((self.install / "recovery").exists())

    def test_scan_detects_il2cpp_marker(self):
        data = self.install / "Sample_Data"
        data.mkdir()
        (data / "GameAssembly.dll").write_bytes(b"native")
        report = ur.scan_unity_installation(str(self.install), str(self.workspace))
        self.assertEqual(report["scripting_backend"], "IL2CPP")

    def test_catalog_resumes_and_only_writes_to_workspace(self):
        self._mono_fixture()
        payload = self.install / "Content"
        payload.mkdir()
        for name in ("a.png", "b.png", "c.png", "d.png"):
            (payload / name).write_bytes(name.encode())
        first = ur.catalog_unity_assets(str(self.install), str(self.workspace), batch_size=2)
        self.assertEqual(first["status"], "in_progress")
        self.assertEqual(first["batch_added"], 2)
        result = first
        for _ in range(10):
            if result["status"] == "complete":
                break
            result = ur.catalog_unity_assets(str(self.install), str(self.workspace), batch_size=2)
        self.assertEqual(result["status"], "complete")
        catalog_path = self.workspace / "recovery" / "asset-catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        self.assertTrue(catalog["completed"])
        self.assertGreaterEqual(len(catalog["files"]), 8)
        self.assertTrue((self.install / "Content" / "a.png").is_file())
        self.assertFalse((self.install / "recovery").exists())
        self.assertEqual(ur.catalog_unity_assets(str(self.install), str(self.workspace))["status"], "complete")

    def test_catalog_refuses_output_path_escape(self):
        with self.assertRaises(ur.UnityRecoveryError):
            ur.catalog_unity_assets(str(self.install), str(self.workspace), "../outside.json")

    def test_catalog_refuses_reusing_catalog_for_different_install(self):
        ur.catalog_unity_assets(str(self.install), str(self.workspace), batch_size=10)
        other = self.root / "other"
        other.mkdir()
        with self.assertRaises(ur.UnityRecoveryError):
            ur.catalog_unity_assets(str(other), str(self.workspace), batch_size=10)

    def test_missing_ilspy_reports_actionable_error(self):
        self._mono_fixture()
        with mock.patch.object(ur.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ur.UnityRecoveryError, "ilspycmd is not installed"):
                ur.decompile_unity_type(str(self.install), str(self.workspace), "StudentScript")

    def test_export_refuses_nonempty_directory(self):
        self._mono_fixture()
        output = self.workspace / "recovery" / "decompiled"
        output.mkdir(parents=True)
        (output / "keep.cs").write_text("// do not overwrite", encoding="utf-8")
        with self.assertRaisesRegex(ur.UnityRecoveryError, "Refusing to overwrite"):
            ur.export_unity_assembly(str(self.install), str(self.workspace), output_path="recovery/decompiled")

    def test_installation_path_must_exist(self):
        with self.assertRaises(ur.UnityRecoveryError):
            ur.scan_unity_installation(str(self.root / "missing"), str(self.workspace))


if __name__ == "__main__":
    unittest.main()
