"""Source plugin interface. A new public-data source = one subclass registered in SOURCES."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from sqlalchemy.engine import Engine


@dataclass
class RunResult:
    items: int = 0  # documents processed
    events: list[int] = field(default_factory=list)  # new event ids
    fingerprint: str | None = None  # structure hash of the parsed response (schema-change detection)
    watermark: dict | None = None  # incremental-sync position after this run


class Source(ABC):
    name: str = "abstract"
    interval_seconds: int = 3600
    critical: bool = True  # critical sources alert Telegram after N consecutive failures
    slow: bool = False  # long-running sources run on their own thread so SEC polling stays ~1 min
    enabled: bool = True
    disabled_reason: str | None = None
    # V2 registry metadata (app.core.source_state / source registry)
    country: str = "US"
    dataset: str | None = None
    license_status: str | None = None
    parser_version: str | None = None
    stale_after = None  # datetime.timedelta: no success within it -> STALE

    @abstractmethod
    def run_live(self, engine: Engine) -> RunResult:
        """Fetch what is new since the last run. Must be idempotent."""

    def run_backfill(self, engine: Engine) -> RunResult:  # optional
        return RunResult()


class PlannedSource(Source):
    """Phase-2 placeholder: registered so the architecture/health view knows about it."""
    enabled = False

    def __init__(self, name: str, reason: str):
        self.name = name
        self.disabled_reason = reason

    def run_live(self, engine: Engine) -> RunResult:
        return RunResult()
