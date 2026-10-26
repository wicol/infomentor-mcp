"""Reentrant thread/process locking for the local guardian session."""

from __future__ import annotations

import asyncio
import fcntl
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import ClassVar


class SessionLockTimeout(Exception):
    """Another local operation held the shared session lock too long."""


class SessionLock:
    """Hold a permanent lock-file inode; the OS releases flock on process exit."""

    _shared: ClassVar[dict[Path, SessionLock]] = {}
    _shared_guard: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def shared(cls, path: Path) -> SessionLock:
        """Share one reentrant mutex across all stores for the same on-disk lock."""
        with cls._shared_guard:
            if path not in cls._shared:
                cls._shared[path] = cls(path)
            return cls._shared[path]

    def __init__(self, path: Path) -> None:
        self.path = path
        self._thread_lock = threading.RLock()
        self._depth = 0

    @contextmanager
    def acquire(self, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        message = "InfoMentor session is busy in another operation; retry after it finishes."
        if not self._thread_lock.acquire(timeout=max(0.0, timeout)):
            raise SessionLockTimeout(message)
        descriptor = None
        try:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return

            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
            )
            os.fchmod(descriptor, 0o600)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SessionLockTimeout(message) from None
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            self._depth = 1
            try:
                yield
            finally:
                self._depth = 0
        finally:
            if descriptor is not None:
                os.close(descriptor)
            self._thread_lock.release()

    @asynccontextmanager
    async def acquire_async(self, timeout: float = 30.0):
        """A separate flock descriptor, never a thread-owned RLock across awaits.

        This lock is intentionally not reentrant. Callers use the store's
        unlocked persistence primitives while holding it; sync operations in
        other tasks/threads/processes still contend on the same inode.
        """
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        try:
            os.fchmod(descriptor, 0o600)
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise SessionLockTimeout(
                            "Session is busy; retry after the current operation."
                        ) from None
                    await asyncio.sleep(0.05)
            yield
        finally:
            os.close(descriptor)

