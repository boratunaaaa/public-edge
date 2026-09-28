"""All data sources. Adding a source = implement app.sources.base.Source and list it here."""
from __future__ import annotations

from app.news.sources import news_sources
from app.sources.base import PlannedSource, Source
from app.sources.congress.house import HousePtrSource, SenateSource
from app.sources.sec.live import Sec13DSource, SecForm4Source, SecForm144Source
from app.sources.trump.oge import TrumpOgeSource
from app.sources.trump.tracker import TrumpFamilySource
from app.sources.usaspending.awards import UsaSpendingSource
from app.sources.us.cftc import CftcCotSource
from app.sources.us.clinical import ClinicalTrialsSource
from app.sources.us.fda import OpenFdaSource
from app.sources.us.short_data import FinraShortVolumeSource, SecFtdSource
from app.sources.ca.official import BankOfCanadaValetSource, CanadaBuysSource
from app.sources.tr.spk import SpkBulletinSource

SOURCES: list[Source] = [
    SecForm4Source(),
    Sec13DSource(),
    SecForm144Source(),
    HousePtrSource(),
    SenateSource(),
    UsaSpendingSource(),
    TrumpFamilySource(),
    TrumpOgeSource(),
    # V2 news intelligence (Tier 1 official sources first)
    *news_sources(),
    PlannedSource("news_globenewswire", "GlobeNewswire RSS does not answer from the server (connection timeout)"),
    PlannedSource("news_dod_contracts", "war.gov contract announcement pages return 403 (Akamai) - DoD awards come from USAspending"),
    PlannedSource("news_fda_press", "fda.gov RSS returns 403 (Akamai) - FDA approvals come from the openFDA API"),
    PlannedSource("news_tier2_media", "Reuters/Bloomberg/WSJ/FT: no licensed feed configured; tier-2 only via links found in watchlist headlines"),
    # V2 datasets (DAILY_MEDIUM: slow thread; history runs in the backfill container)
    FinraShortVolumeSource(),
    SecFtdSource(),
    ClinicalTrialsSource(),
    OpenFdaSource(),
    CftcCotSource(),
    CanadaBuysSource(),
    BankOfCanadaValetSource(),
    SpkBulletinSource(),
    PlannedSource("senate_lda", "BLOCKED: lda.gov (Senate LDA API) answers 403 (Akamai) from the tested network; anonymous API otherwise allowed"),
    PlannedSource("uspto_patents", "AUTH_REQUIRED: PatentsView / USPTO ODP API needs a (free) API key"),
    PlannedSource("fred_alfred", "AUTH_REQUIRED: FRED/ALFRED vintages need a (free) API key"),
    PlannedSource("ciro_short", "BLOCKED: ciro.ca answers 403 to automated clients from both tested networks (office + Hetzner server)"),
    PlannedSource("ca_lobbying", "BLOCKED: lobbycanada.gc.ca answers 403 to automated clients from both tested networks (office + Hetzner server)"),
    PlannedSource("sedi", "BLOCKED: SEDI / SEDAR+ behind a bot manager; needs an authorised feed"),
    PlannedSource("kap_live", "BLOCKED: KAP list API behind WAF; official route = MKK API Portal (account + service approval)"),
    PlannedSource("ca_grants", "FORWARD_ONLY: file has no per-record publication date; stream job in the backfill container"),
    # Phase 2 - architecture slots
    PlannedSource("doj_antitrust", "phase 2: DOJ Antitrust actions"),
    PlannedSource("merger_arb", "phase 2: M&A merger arbitrage (8-K / DEFM14A)"),
    PlannedSource("buyback_execution", "phase 2: actual buyback execution (10-Q/10-K Item 2)"),
    PlannedSource("form13f", "V2: SEC 13F data sets are ingested by the backfill container (quarterly, HISTORICAL_LOW)"),
]


def by_name(name: str) -> Source:
    for s in SOURCES:
        if s.name == name:
            return s
    raise KeyError(name)
