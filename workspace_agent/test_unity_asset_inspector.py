# SPDX-License-Identifier: GPL-3.0-or-later
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from workspace_agent import unity_asset_inspector as inspector
from workspace_agent.unity_recovery import UnityRecoveryError


class UnityAssetInspectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "YandereSimulator_Data").mkdir()
        (self.root / "YandereSimulator_Data" / "sharedassets0.assets").write_bytes(b"asset-data")

    def tearDown(self):
        self.temp.cleanup()

    def test_refuses_large_file_before_loading_parser(self):
        path = self.root / "YandereSimulator_Data" / "sharedassets0.assets"
        path.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaisesRegex(UnityRecoveryError, "Refusing to load"):
            inspector.inspect_unity_serialized_file(str(self.root), "YandereSimulator_Data/sharedassets0.assets", max_file_mb=1)

    def test_inspects_objects_and_reports_type_counts(self):
        class FakeObject:
            def __init__(self, path_id, kind, name):
                self.path_id = path_id
                self.type = types.SimpleNamespace(name=kind)
                self._name = name
            def read(self):
                return types.SimpleNamespace(m_Name=self._name)
        fake = types.SimpleNamespace(load=lambda _: types.SimpleNamespace(objects=[
            FakeObject(10, "Texture2D", "SchoolTexture"),
            FakeObject(11, "GameObject", "Student"),
            FakeObject(12, "Texture2D", "Floor"),
        ]))
        with mock.patch.dict(sys.modules, {"UnityPy": fake}):
            result = inspector.inspect_unity_serialized_file(str(self.root), "YandereSimulator_Data/sharedassets0.assets", max_objects=2)
        self.assertEqual(result["status"], "truncated")
        self.assertEqual(result["object_count"], 3)
        self.assertEqual(result["objects_returned"], 2)
        self.assertEqual(result["type_counts_returned"], {"GameObject": 1, "Texture2D": 1})
        self.assertEqual(result["objects"][1]["name"], "Student")

    def test_rejects_absolute_paths_and_unsupported_extensions(self):
        with self.assertRaisesRegex(UnityRecoveryError, "relative"):
            inspector.inspect_unity_serialized_file(str(self.root), str(self.root / "YandereSimulator_Data/sharedassets0.assets"))
        with self.assertRaisesRegex(UnityRecoveryError, "Unsupported"):
            inspector.inspect_unity_serialized_file(str(self.root), "YandereSimulator_Data/nope.exe")

    def test_missing_unitypy_is_actionable(self):
        with mock.patch.dict(sys.modules, {"UnityPy": None}):
            with self.assertRaisesRegex(UnityRecoveryError, "UnityPy is not installed"):
                inspector.inspect_unity_serialized_file(str(self.root), "YandereSimulator_Data/sharedassets0.assets")


if __name__ == "__main__":
    unittest.main()
