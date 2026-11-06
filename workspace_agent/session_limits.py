# SPDX-License-Identifier: GPL-3.0-or-later
"""In-flight tool-call limits for the bridge: a global cap plus a per-session
cap. Calls beyond a cap queue (FIFO via asyncio.Semaphore) and fail with a
'busy' error if they wait longer than the queue timeout (0 = fail at once).
A cap of 0 disables that cap."""
import asyncio
import contextlib
import os
import time

DEFAULT_MAX_INFLIGHT = 16
DEFAULT_MAX_PER_SESSION = 4
DEFAULT_QUEUE_TIMEOUT = 120.0


class BusyError(RuntimeError):
    """Raised when a call waited longer than the queue timeout for a slot."""


def _num(value, default, cast):
    try:
        out = cast(value)
    except (TypeError, ValueError):
        return default
    return out if out >= 0 else default


def load_limits(config=None, environ=None):
    """Resolve limits. Env (ZS_MAX_INFLIGHT, ZS_MAX_INFLIGHT_PER_SESSION,
    ZS_QUEUE_TIMEOUT) wins over config keys (max_inflight,
    max_inflight_per_session, queue_timeout), which win over defaults."""
    config = config if isinstance(config, dict) else {}
    environ = os.environ if environ is None else environ
    out = {}
    for key, env_key, default, cast in (
        ("max_inflight", "ZS_MAX_INFLIGHT", DEFAULT_MAX_INFLIGHT, int),
        ("max_inflight_per_session", "ZS_MAX_INFLIGHT_PER_SESSION", DEFAULT_MAX_PER_SESSION, int),
        ("queue_timeout", "ZS_QUEUE_TIMEOUT", DEFAULT_QUEUE_TIMEOUT, float),
    ):
        value = _num(config.get(key), default, cast) if key in config else default
        env_value = environ.get(env_key)
        if env_value not in (None, ""):
            value = _num(env_value, value, cast)
        out[key] = value
    return out


class InflightLimiter:
    def __init__(self, max_inflight=DEFAULT_MAX_INFLIGHT, max_inflight_per_session=DEFAULT_MAX_PER_SESSION,
                 queue_timeout=DEFAULT_QUEUE_TIMEOUT):
        self.max_inflight = int(max_inflight)
        self.max_per_session = int(max_inflight_per_session)
        self.queue_timeout = float(queue_timeout)
        self._global = None
        self._sessions = {}   # session_id -> [semaphore, running, queued]
        self._running = 0
        self._queued = 0

    def configure(self, max_inflight, max_per_session, queue_timeout):
        """Apply new limits (only safe while idle; used at startup/tests)."""
        self.max_inflight = int(max_inflight)
        self.max_per_session = int(max_per_session)
        self.queue_timeout = float(queue_timeout)
        self._global = None
        self._sessions.clear()
        self._running = self._queued = 0

    def active(self, session_id):
        """Calls running or queued for a session."""
        entry = self._sessions.get(session_id)
        return (entry[1] + entry[2]) if entry else 0

    def snapshot(self):
        return {
            "max_inflight": self.max_inflight,
            "max_inflight_per_session": self.max_per_session,
            "queue_timeout": self.queue_timeout,
            "inflight": self._running,
            "queued": self._queued,
            "sessions": {sid: {"inflight": e[1], "queued": e[2]} for sid, e in self._sessions.items()},
        }

    def _busy(self, session_id, which):
        return BusyError(
            f"Too many tool calls in flight ({which}; global {self._running}/{self.max_inflight or 'unlimited'}, "
            f"session {self.active(session_id)}/{self.max_per_session or 'unlimited'}); try again.")

    @contextlib.asynccontextmanager
    async def slot(self, session_id):
        """Hold one per-session and one global slot for the duration of a call."""
        if self._global is None and self.max_inflight > 0:
            self._global = asyncio.Semaphore(self.max_inflight)
        entry = self._sessions.get(session_id)
        if entry is None:
            entry = self._sessions[session_id] = [
                asyncio.Semaphore(self.max_per_session) if self.max_per_session > 0 else None, 0, 0]
        sem_session, sem_global = entry[0], self._global
        deadline = time.monotonic() + self.queue_timeout
        held = []
        entry[2] += 1
        self._queued += 1
        try:
            for sem, label in ((sem_session, "per-session limit"), (sem_global, "global limit")):
                if sem is None:
                    continue
                if not sem.locked():
                    await sem.acquire()
                else:
                    try:
                        await asyncio.wait_for(sem.acquire(), max(0.0, deadline - time.monotonic()))
                    except asyncio.TimeoutError:
                        raise self._busy(session_id, label) from None
                held.append(sem)
        except BaseException:
            for sem in held:
                sem.release()
            raise
        finally:
            entry[2] -= 1
            self._queued -= 1
        entry[1] += 1
        self._running += 1
        try:
            yield
        finally:
            entry[1] -= 1
            self._running -= 1
            for sem in held:
                sem.release()
            if entry[1] == 0 and entry[2] == 0 and self._sessions.get(session_id) is entry:
                del self._sessions[session_id]
