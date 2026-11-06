# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from workspace_agent.file_locks import (
    FileLockManager,
    FileLockTimeout,
    current_owner,
    file_locks,
    lock_owner,
)
from workspace_agent.tools import Workspace


class FileLockManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.locks = FileLockManager(default_timeout=5.0)

    def tearDown(self):
        self.temp.cleanup()

    def test_second_writer_waits_then_succeeds(self):
        target = self.root / "a.txt"
        order: list[str] = []
        first_in = threading.Event()

        def first():
            with self.locks.hold([target], owner="one"):
                order.append("one-start")
                first_in.set()
                time.sleep(0.2)
                order.append("one-end")

        def second():
            first_in.wait(2)
            with self.locks.hold([target], owner="two", timeout=5):
                order.append("two")

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(order, ["one-start", "one-end", "two"])
        self.assertEqual(self.locks.holders(), {})

    def test_timeout_message(self):
        target = self.root / "a.txt"
        results: list[BaseException] = []
        held = threading.Event()
        release = threading.Event()

        def holder():
            with self.locks.hold([target], owner="alpha"):
                held.set()
                release.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(2)
        try:
            with self.assertRaises(FileLockTimeout) as ctx:
                with self.locks.hold([target], owner="beta", timeout=0.05):
                    pass
        finally:
            release.set()
            thread.join(5)
        normalized = os.path.normcase(os.path.abspath(target))
        self.assertEqual(ctx.exception.path, normalized)
        self.assertEqual(ctx.exception.holder, "alpha")
        self.assertEqual(str(ctx.exception),
                         f"File '{normalized}' is locked by agent alpha; gave up after 0.05s.")
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertEqual(results, [])

    def test_different_files_run_in_parallel(self):
        barrier = threading.Barrier(2, timeout=3)
        errors: list[BaseException] = []

        def worker(name):
            try:
                with self.locks.hold([self.root / name], owner=name, timeout=1):
                    barrier.wait()  # both must be inside their locks at once
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("x.txt", "y.txt")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [])

    def test_directory_conflicts_with_child_both_ways(self):
        directory = self.root / "pkg"
        child = directory / "mod.py"
        with self.locks.hold([directory], owner="a"):
            with self.assertRaises(FileLockTimeout):
                with self.locks.hold([child], owner="b", timeout=0.05):
                    pass
        with self.locks.hold([child], owner="a"):
            with self.assertRaises(FileLockTimeout):
                with self.locks.hold([directory], owner="b", timeout=0.05):
                    pass
        # A sibling whose name merely starts with the directory name is not a child.
        with self.locks.hold([directory], owner="a"):
            with self.locks.hold([self.root / "pkg2.py"], owner="b", timeout=0.05):
                pass

    def test_all_or_nothing_acquisition(self):
        a, b = self.root / "a.txt", self.root / "b.txt"
        with self.locks.hold([b], owner="other"):
            with self.assertRaises(FileLockTimeout):
                with self.locks.hold([a, b], owner="me", timeout=0.05):
                    pass
            # 'a' must not have been left held by the failed attempt.
            self.assertEqual(list(self.locks.holders().values()), ["other"])

    def test_reentrant_for_same_owner(self):
        target = self.root / "a.txt"
        with self.locks.hold([target], owner="me", timeout=0.05):
            with self.locks.hold([target, self.root / "b.txt"], owner="me", timeout=0.05):
                self.assertEqual(len(self.locks.holders()), 2)
            self.assertEqual(len(self.locks.holders()), 1)
        self.assertEqual(self.locks.holders(), {})

    def test_released_after_exception(self):
        target = self.root / "a.txt"
        with self.assertRaises(ValueError):
            with self.locks.hold([target], owner="me"):
                raise ValueError("boom")
        self.assertEqual(self.locks.holders(), {})

    def test_owner_context_and_env_default(self):
        self.assertEqual(current_owner(), "anonymous")
        with lock_owner("session-1"):
            self.assertEqual(current_owner(), "session-1")
        self.assertEqual(current_owner(), "anonymous")
        previous = os.environ.get("ZS_FILE_LOCK_TIMEOUT")
        os.environ["ZS_FILE_LOCK_TIMEOUT"] = "7.5"
        try:
            self.assertEqual(FileLockManager().default_timeout, 7.5)
        finally:
            if previous is None:
                del os.environ["ZS_FILE_LOCK_TIMEOUT"]
            else:
                os.environ["ZS_FILE_LOCK_TIMEOUT"] = previous
        self.assertEqual(FileLockManager(default_timeout=3).default_timeout, 3)

    def test_owner_defaults_to_context_var(self):
        target = self.root / "a.txt"
        with lock_owner("ctx-owner"):
            with self.locks.hold([target]):
                self.assertEqual(list(self.locks.holders().values()), ["ctx-owner"])

    @unittest.skipUnless(sys.platform == "win32", "case-insensitive paths are Windows-only")
    def test_case_insensitive_on_windows(self):
        with self.locks.hold([self.root / "File.TXT"], owner="a"):
            with self.assertRaises(FileLockTimeout):
                with self.locks.hold([self.root / "file.txt"], owner="b", timeout=0.05):
                    pass


class WorkspaceLockingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ws = Workspace(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_concurrent_apply_patch_keeps_both_edits(self):
        (self.root / "f.txt").write_text("alpha\nbeta\n" * 1, encoding="utf-8", newline="")
        errors: list[BaseException] = []
        start = threading.Barrier(2, timeout=5)

        def patch(owner, old, new):
            try:
                with lock_owner(owner):
                    start.wait()
                    for _ in range(25):
                        self.ws.apply_patch("f.txt", old, new)
                        self.ws.apply_patch("f.txt", new, old)
                    self.ws.apply_patch("f.txt", old, new)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=patch, args=("agent-1", "alpha", "ALPHA")),
            threading.Thread(target=patch, args=("agent-2", "beta", "BETA")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual((self.root / "f.txt").read_text(encoding="utf-8"), "ALPHA\nBETA\n")
        self.assertEqual(file_locks.holders(), {})

    def test_write_blocked_by_other_agent_times_out_with_message(self):
        target = self.root / "f.txt"
        target.write_text("x", encoding="utf-8")
        held = threading.Event()
        release = threading.Event()

        def holder():
            with lock_owner("agent-1"), file_locks.hold([target]):
                held.set()
                release.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(2)
        old = file_locks.default_timeout
        file_locks._default_timeout = 0.05
        try:
            with lock_owner("agent-2"):
                with self.assertRaises(RuntimeError) as ctx:
                    self.ws.write_file("f.txt", "y")
                self.assertIn("is locked by agent agent-1; gave up after 0.05s.", str(ctx.exception))
                with self.assertRaises(RuntimeError):
                    self.ws.delete_file("f.txt")
                self.ws.read_file("f.txt")  # reads are never locked
        finally:
            file_locks._default_timeout = None if old == 60.0 else old
            release.set()
            thread.join(5)
        self.assertEqual(target.read_text(encoding="utf-8"), "x")


if __name__ == "__main__":
    unittest.main()
