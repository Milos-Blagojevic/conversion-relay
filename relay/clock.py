"""Time source. Everything reads time through a clock, so demo seeding can replay two weeks in seconds."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class ManualClock:
    """A clock that moves only when told; `sleep` advances it instantly."""

    def __init__(self, start: float) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def set(self, moment: float) -> None:
        self._now = moment

    def sleep(self, seconds: float) -> None:
        self._now += max(0.0, seconds)
