"""Runtime configuration. Every secret comes from the environment; nothing is hard-coded."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


def _float(name: str, default: float) -> float:
    v = _env(name)
    return float(v) if v else default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "postgresql+psycopg://public_edge:public_edge@localhost:5432/public_edge"))
    # SEC fair-access policy: identify yourself, stay below 10 req/s.
    sec_user_agent: str = field(default_factory=lambda: _env("SEC_USER_AGENT", "PublicEdge research contact@example.com"))
    sec_max_rps: float = field(default_factory=lambda: _float("SEC_MAX_RPS", 5.0))
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    telegram_enabled: bool = field(default_factory=lambda: _env("TELEGRAM_ENABLED", "1") == "1")
    # Minimum heuristic priority for a Telegram alert; lower events stay in the DB only.
    alert_min_priority: int = field(default_factory=lambda: _int("ALERT_MIN_PRIORITY", 4))
    max_alerts_per_hour: int = field(default_factory=lambda: _int("MAX_ALERTS_PER_HOUR", 8))
    backfill_years: int = field(default_factory=lambda: _int("BACKFILL_YEARS", 5))
    min_sample_size: int = field(default_factory=lambda: _int("MIN_SAMPLE_SIZE", 30))
    source_failure_alert_threshold: int = field(default_factory=lambda: _int("SOURCE_FAILURE_ALERT_THRESHOLD", 3))
    news_backfill_days: int = field(default_factory=lambda: _int("NEWS_BACKFILL_DAYS", 365))


settings = Settings()

# Public-record persons for the Trump-family tracker (SEC reporting-owner CIKs,
# verified via EDGAR company search on 2026-09-24).
TRUMP_FAMILY_CIKS: dict[str, str] = {
    "0000947033": "Donald J. Trump",
    "0002016181": "Donald J. Trump Jr.",
    "0002057754": "Eric Trump",
}
# Name fragments used to catch filings made through trusts/entities where the
# person appears only by name (e.g. "Donald J. Trump Revocable Trust").
TRUMP_FAMILY_NAME_PATTERNS: dict[str, str] = {
    r"\bTRUMP,?\s+DONALD\s+J\.?,?\s+JR\b|\bDONALD\s+J\.?\s+TRUMP,?\s+JR\b": "Donald J. Trump Jr.",
    r"\bTRUMP,?\s+ERIC\b|\bERIC\s+(F\.?\s+)?TRUMP\b": "Eric Trump",
    r"\bTRUMP,?\s+DONALD\s+J\b(?!\.?,?\s+JR)|\bDONALD\s+J\.?\s+TRUMP\b(?!,?\s+JR)": "Donald J. Trump",
}
