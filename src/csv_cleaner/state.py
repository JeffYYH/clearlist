"""Small process-local store: one job and latest snapshot per browser session."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock

from .engine import Dataset, PreviewResult


class StoreFull(Exception):
    pass


@dataclass
class Job:
    dataset: Dataset
    touched: float
    result: PreviewResult | None = None


class SessionStore:
    def __init__(self, capacity: int = 4, ttl: int = 1800, clock: Callable = time.monotonic):
        self.capacity = capacity
        self.ttl = ttl
        self.clock = clock
        self._jobs: dict[str, Job] = {}
        self._lock = RLock()

    def prune(self) -> None:
        with self._lock:
            expired = [
                key for key, job in self._jobs.items() if self.clock() - job.touched >= self.ttl
            ]
            for key in expired:
                del self._jobs[key]

    def get(self, key: str | None) -> Job | None:
        with self._lock:
            self.prune()
            job = self._jobs.get(key)
            if job:
                job.touched = self.clock()
            return job

    def put(self, key: str, dataset: Dataset) -> None:
        with self._lock:
            self.prune()
            if key not in self._jobs and len(self._jobs) >= self.capacity:
                raise StoreFull
            self._jobs[key] = Job(dataset, self.clock())

    def set_result(self, key: str, expected: Job, result: PreviewResult) -> bool:
        with self._lock:
            # Protect against a reset/replacement racing preview creation in threaded tests.
            if self.get(key) is not expected:
                return False
            expected.result = result
            return True

    def discard(self, key: str | None) -> None:
        with self._lock:
            self._jobs.pop(key, None)
