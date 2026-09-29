"""Central clock. Production uses SystemClock (behaviour unchanged: wall-clock UTC). A historical replay installs a
ReplayClock for the duration of the run (use_clock), so the reused production code paths (proposal gate, paper
ledger, price cache, security master) believe it is the simulated moment.

Only the paths a replay executes read the clock; the rest of the code base still calls datetime.now() directly and is
never run inside a replay (tests/test_replay.py pins the migrated call sites)."""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
from datetime import date, datetime, timezone


class SystemClock:
    name = "system"

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today(self) -> date:
        return date.today()


@dataclass
class ReplayClock:
    """Deterministic simulated time. It only moves forward (set / advance), never by itself."""
    current: datetime
    name: str = "replay"
    history: list[datetime] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.current.tzinfo is None:
            raise ValueError("ReplayClock needs a timezone-aware datetime")
        self.current = self.current.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self.current

    def today(self) -> date:
        # the date a production process would see: UTC servers (date.today() on the production hosts is UTC)
        return self.current.date()

    def set(self, t: datetime) -> None:
        t = t.astimezone(timezone.utc)
        if t < self.current:
            raise ValueError(f"replay clock cannot move backwards ({t} < {self.current})")
        self.history.append(self.current)
        self.current = t


_SYSTEM = SystemClock()
_current: contextvars.ContextVar = contextvars.ContextVar("pe_clock", default=_SYSTEM)


def get_clock():
    return _current.get()


def now() -> datetime:
    return _current.get().now()


def today() -> date:
    return _current.get().today()


def is_replay() -> bool:
    return _current.get() is not _SYSTEM


@contextlib.contextmanager
def use_clock(clock):
    token = _current.set(clock)
    try:
        yield clock
    finally:
        _current.reset(token)
