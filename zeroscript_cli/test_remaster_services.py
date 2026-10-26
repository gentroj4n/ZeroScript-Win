# SPDX-License-Identifier: GPL-3.0-or-later
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from zeroscript_cli.remaster_services import (
    AgentFlightRecorder, ArtifactGallery, IntegrationHealth, ModelProfiles,
    ProjectIntelligenceIndex, TaskVerificationGate,
    WorkspaceSnapshot, validate_tool_arguments, validate_tool_dispatch, WorktreeManager,
)

class RemasterServiceTests(unittest.TestCase):
    def test_recorder_redacts_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"events.jsonl"; AgentFlightRecorder(p).record("tool", api_key="never-store")
            self.assertNotIn("never-store",p.read_text())
    def test_index_is_incremental_and_searchable(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td,"a.py").write_text("class Widget:\n pass\n")
            idx=ProjectIntelligenceIndex(td); self.assertEqual(idx.build()["files_changed"],1)
            self.assertEqual(idx.search("Widget")[0]["path"],"a.py")
            self.assertEqual(idx.build()["files_changed"],0)
    def test_verification_requires_evidence_for_every_criterion(self):
        result=TaskVerificationGate.evaluate(["tests pass","UI verified"],[{"criterion":"tests pass","passed":True,"source":"pytest","summary":"10 passed"}])
        self.assertEqual(result["status"],"unverified")
    def test_shared_dispatch_validator_validates_schema_and_arguments(self):
        catalogue = [
            {"name": "workspace_read_file", "inputSchema": {
                "type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"], "additionalProperties": False,
            }},
            {"name": "untyped_tool"},
        ]
        self.assertEqual(validate_tool_dispatch("workspace_read_file", {"path": "README.md"}, catalogue), [])
        self.assertTrue(validate_tool_dispatch("workspace_read_file", {}, catalogue))
        self.assertTrue(validate_tool_dispatch("missing_tool", {}, catalogue))
        self.assertTrue(validate_tool_dispatch("untyped_tool", {}, catalogue))
        self.assertTrue(validate_tool_dispatch("workspace_read_file", [] , catalogue))
        self.assertEqual(validate_tool_dispatch("workspace_read_file", {"path": "README.md"}, catalogue, mode="legacy"), [])
        
    def test_nested_json_schema_constraints_are_checked(self):
        schema = {"type": "object", "properties": {
            "config": {"type": "object", "required": ["name"], "additionalProperties": False,
                       "properties": {"name": {"type": "string", "minLength": 3},
                                      "ports": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 65535}}}}
        }, "required": ["config"], "additionalProperties": False}
        self.assertEqual(validate_tool_arguments(schema, {"config": {"name": "api", "ports": [80, 443]}}), [])
        errors = validate_tool_arguments(schema, {"config": {"name": "x", "ports": [0, "bad"], "surprise": True}})
        self.assertTrue(any("too short" in error for error in errors))
        self.assertTrue(any("unknown property" in error for error in errors))
        self.assertTrue(any("below minimum" in error for error in errors))
        self.assertTrue(any("must be integer" in error for error in errors))

    def test_worktree_manager_rejects_non_git_directory(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError): WorktreeManager(td).create("worker-1")
    def test_worktree_manager_creates_and_lists_isolated_worktree(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(repo), *args],
                    check=True, capture_output=True, text=True, timeout=30,
                )
            git("init")
            git("config", "user.email", "zeroscript-test@example.invalid")
            git("config", "user.name", "ZeroScript Test")
            (repo / "README.md").write_text("test repository\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-m", "initial test commit")
            manager = WorktreeManager(repo)
            with self.assertRaisesRegex(ValueError, "outside the selected repository"):
                manager.create("nested", directory=repo / "nested-worktree")
            with self.assertRaisesRegex(ValueError, "Base ref"):
                manager.create("bad-base", base="--upload-pack=anything")
            made = manager.create("worker-1")
            self.assertEqual(made["branch"], "zeroscript/worker-1")
            self.assertTrue((Path(made["path"]) / "README.md").is_file())
            worktrees = manager.list()
            self.assertTrue(any(Path(item.get("worktree", "")).resolve() == Path(made["path"]).resolve()
                                for item in worktrees))
    def test_health_probe_returns_structured_failure(self):
        result=IntegrationHealth.tcp("127.0.0.1",1,timeout=.1)
        self.assertFalse(result["ok"]); self.assertIn("latency_ms",result)
    def test_snapshot_captures_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td,"file.txt").write_text("sample")
            self.assertEqual(WorkspaceSnapshot(td).capture(["file.txt"])["files"],1)
            with self.assertRaises(ValueError): WorkspaceSnapshot(td).capture(["../outside"])
    def test_snapshot_restore_requires_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); target=root/"file.txt"; target.write_text("before")
            snapshot=WorkspaceSnapshot(root); made=snapshot.capture(["file.txt"])
            target.write_text("after")
            result=snapshot.restore(made["snapshot"])
            self.assertEqual(result["skipped_existing"],["file.txt"])
            self.assertEqual(target.read_text(),"after")
            result=snapshot.restore(made["snapshot"],overwrite=True)
            self.assertEqual(result["restored"],["file.txt"])
            self.assertEqual(target.read_text(),"before")
    def test_model_profiles_omit_api_keys_and_preserve_capabilities(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"profiles.json"
            store=ModelProfiles(p)
            store.save("local",{"base_url":"http://127.0.0.1:11434/v1","model":"qwen3:8b","vision":True,"context_tokens":8192,"api_key":"private"})
            saved=json.loads(p.read_text())["local"]
            self.assertNotIn("api_key",saved)
            self.assertTrue(saved["vision"])
            self.assertEqual(saved["context_tokens"],8192)
            self.assertEqual(store.load()["local"]["model"],"qwen3:8b")
            with self.assertRaisesRegex(ValueError, "context_tokens"):
                store.save("bad", {"base_url":"http://127.0.0.1:11434/v1","model":"tiny","context_tokens":64})
    def test_artifact_gallery_includes_hash(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td,"image.png").write_bytes(b"fake")
            row=ArtifactGallery.scan(td)[0]
            self.assertEqual(row["path"],"image.png"); self.assertEqual(len(row["sha256"]),64)
    def test_schema_validator_rejects_missing_and_wrong_type(self):
        schema={"type":"object","required":["path"],"properties":{"path":{"type":"string"},"limit":{"type":"integer"}}}
        errors=validate_tool_arguments(schema,{"limit":"2"})
        self.assertTrue(any("Missing required" in e for e in errors))
        self.assertTrue(any("must be integer" in e for e in errors))

if __name__ == "__main__": unittest.main()
