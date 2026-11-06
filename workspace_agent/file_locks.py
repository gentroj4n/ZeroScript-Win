# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-path write locks shared by every agent that writes through the bridge.

All agents (browser tabs and CLIs) call ``Workspace`` methods in worker threads
of one bridge process, so a process-local lock table is enough to serialise
writes to the same file.  Reads are never locked.

Rules:
* Paths are normalised with ``os.path.normcase(os.path.abspath(path))``.
* ``hold()`` acquires every requested path atomically (all-or-nothing, one
  ``threading.Condition``).  A caller never holds part of its set while it
  waits, so two callers cannot deadlock on each other.
* A held path conflicts with a requested path if they are equal or one is an
  ancestor directory of the other.
* Re-entrant: the same owner on the same thread may nest ``hold()`` calls
  (e.g. ``multi_edit`` -> ``write_file``) without waiting on itself.  Two
  threads that share an owner name (or both use 'anonymous') still exclude
  each other, so a missing ``lock_owner`` can never silently disable locking.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import threading
import time
from pathlib import Path
from typing import Iterator, Union

DEFAULT_TIMEOUT_SECONDS = 60.0
ENV_TIMEOUT = "ZS_FILE_LOCK_TIMEOUT"
ANONYMOUS = "anonymous"

PathLike = Union[str, "os.PathLike[str]"]


class FileLockTimeout(RuntimeError):
    """Raised when a file stayed locked by another agent for the whole timeout."""

    def __init__(self, path: str, holder: str, timeout: float):
        self.path = path
        self.holder = holder
        self.timeout = timeout
        super().__init__(f"File '{path}' is locked by agent {holder}; gave up after {timeout:g}s.")


def _default_timeout() -> float:
    raw = os.environ.get(ENV_TIMEOUT, "").strip()
    if raw:
        try:
            value = float(raw)
            if value >= 0:
                return value
        except ValueError:
            pass
    return DEFAULT_TIMEOUT_SECONDS


def _normalize(path: PathLike) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_ancestor(parent: str, child: str) -> bool:
    prefix = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(prefix)


def _conflicts(a: str, b: str) -> bool:
    return a == b or _is_ancestor(a, b) or _is_ancestor(b, a)


class FileLockManager:
    def __init__(self, default_timeout: float | None = None):
        self._default_timeout = default_timeout
        self._cond = threading.Condition()
        # normalized path -> {(owner, thread_ident): nesting count}
        self._held: dict[str, dict[tuple[str, int], int]] = {}

    @property
    def default_timeout(self) -> float:
        return self._default_timeout if self._default_timeout is not None else _default_timeout()

    def _find_conflict(self, wanted: list[str], me: tuple[str, int]) -> tuple[str, str] | None:
        for path in wanted:
            for held_path, entries in self._held.items():
                if not _conflicts(path, held_path):
                    continue
                for key in entries:
                    if key != me:
                        return path, key[0]
        return None

    @contextlib.contextmanager
    def hold(self, paths: list[PathLike], owner: str | None = None,
             timeout: float | None = None) -> Iterator[None]:
        wanted = sorted({_normalize(p) for p in paths})
        if not wanted:
            yield
            return
        me = (owner if owner is not None else current_owner(), threading.get_ident())
        limit = self.default_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + limit
        with self._cond:
            while True:
                conflict = self._find_conflict(wanted, me)
                if conflict is None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FileLockTimeout(conflict[0], conflict[1], limit)
                self._cond.wait(remaining)
            for path in wanted:
                entries = self._held.setdefault(path, {})
                entries[me] = entries.get(me, 0) + 1
        try:
            yield
        finally:
            with self._cond:
                for path in wanted:
                    entries = self._held.get(path)
                    if not entries or me not in entries:
                        continue
                    entries[me] -= 1
                    if entries[me] <= 0:
                        del entries[me]
                    if not entries:
                        del self._held[path]
                self._cond.notify_all()

    def holders(self) -> dict[str, str]:
        """Snapshot of normalized path -> owner, for diagnostics."""
        with self._cond:
            return {path: sorted(key[0] for key in entries)[0]
                    for path, entries in self._held.items() if entries}


file_locks = FileLockManager()

_lock_owner: contextvars.ContextVar[str | None] = contextvars.ContextVar("zs_file_lock_owner", default=None)


def current_owner() -> str:
    return _lock_owner.get() or ANONYMOUS


@contextlib.contextmanager
def lock_owner(owner: str) -> Iterator[None]:
    token = _lock_owner.set(str(owner))
    try:
        yield
    finally:
        _lock_owner.reset(token)
