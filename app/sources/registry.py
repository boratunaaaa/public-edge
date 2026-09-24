"""All data sources. Adding a source = implement app.sources.base.Source and list it here."""
from __future__ import annotations

from app.sources.base import PlannedSource, Source
from app.sources.congress.house import HousePtrSource, SenateSource
from app.sources.sec.live import Sec13DSource, SecForm4Source, SecForm144Source
from app.sources.trump.tracker import TrumpFamilySource
from app.sources.usaspending.awards import UsaSpendingSource

SOURCES: list[Source] = [
    SecForm4Source(),
    Sec13DSource(),
    SecForm144Source(),
    HousePtrSource(),
    SenateSource(),
    UsaSpendingSource(),
    TrumpFamilySource(),
    # Phase 2 - architecture slots, not active in V1
    PlannedSource("fda", "phase 2: FDA approvals / PDUFA calendar"),
    PlannedSource("clinicaltrials", "phase 2: ClinicalTrials.gov results/status changes"),
    PlannedSource("doj_antitrust", "phase 2: DOJ Antitrust actions"),
    PlannedSource("merger_arb", "phase 2: M&A merger arbitrage (8-K / DEFM14A)"),
    PlannedSource("buyback_execution", "phase 2: actual buyback execution (10-Q/10-K Item 2)"),
    PlannedSource("form13f", "phase 2: 13F institutional positions"),
]


def by_name(name: str) -> Source:
    for s in SOURCES:
        if s.name == name:
            return s
    raise KeyError(name)
