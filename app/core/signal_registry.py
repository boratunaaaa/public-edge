"""Signal family contracts (V2 #44). A family that is not declared here does not exist: no undocumented heuristic.

Every family is measured independently (app.backtest.validation_v2); paper_enabled is decided by data (TRUSTED + an
OUTPERFORMING verdict), never set here. research_only families may feed only the RESEARCH paper book."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class SignalFamily:
    signal_id: str
    country: str
    version: str
    source_event_types: tuple[str, ...]
    source_datasets: tuple[str, ...]
    feature_definition: str
    entry_rule: str = "first session OPEN strictly after available_at (never transaction / document date)"
    exit_rule: str = "fixed horizon 30/90/180 calendar days; delisting / verified merger consideration resolved by the corporate-action exit resolver"
    benchmark: str = "SPY (US), sector ETF secondary; XU100 (TR), XIU/TSX composite (CA) when priced"
    data_quality_requirement: str = "identity HIGH/MEDIUM, date_quality exact|date, unresolved share <= 20 % for TRUSTED"
    tradability_gate: str = "listed + priced at available_at, not PRE_LISTING / NOT_TRADEABLE; liquidity bucket recorded"
    minimum_sample: int = 100  # company-equal episodes for a verdict
    confidence_interval: str = "Wilson 95 % on company-equal SPY-beat rate; Benjamini-Hochberg across the hypothesis ledger"
    context_only: bool = False
    research_only: bool = True
    notes: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


_E = "first session OPEN strictly after available_at"
FAMILIES: tuple[SignalFamily, ...] = (
    # ---- existing US families (revalidated with the same contract)
    SignalFamily("INSIDER_BUY", "US", "1", ("INSIDER_OPEN_MARKET_BUY",), ("sec_form4",), "open-market purchase by an officer/director (Form 4 code P)", research_only=False),
    SignalFamily("INSIDER_CLUSTER", "US", "1", ("CLUSTER_INSIDER_BUY",), ("sec_form4",), ">= 2 independent insiders buying within the window", research_only=False),
    SignalFamily("13D", "US", "2", ("ACTIVIST_13D",), ("sec_13d",), "Schedule 13D; classification ACTIVIST/INSIDER/CONTROL/PASSIVE (13d-class-v3.2)", context_only=True,
                 notes="decision 2026-09-27: 13D is context/confirmation, never a standalone trigger"),
    SignalFamily("ACTIVIST_CROWDING", "US", "2", ("ACTIVIST_CROWDING",), ("sec_13d",), ">= 2 independent ACTIVIST_13D (HIGH/MEDIUM) filers within 90 days", context_only=True),
    SignalFamily("HOUSE_BUY", "US", "1", ("CONGRESS_BUY",), ("congress_house",), "House PTR purchase (disclosure date)", context_only=True),
    SignalFamily("GOV_CONTRACT", "US", "1", ("GOV_CONTRACT",), ("usaspending",), "federal award >= threshold of market cap (first-seen)"),
    SignalFamily("MULTI_SIGNAL", "US", "1", ("MULTI_SIGNAL_EVENT",), ("sec_form4", "sec_13d", "usaspending", "congress_house"), "independent source groups within the window (no double counting of one filing)"),
    SignalFamily("10K_CHANGE", "US", "1", ("TENK_CHANGE",), ("sec_10k",), "material 10-K text change vs prior year"),
    SignalFamily("NEWS", "US", "1", ("MATERIAL_NEWS",), ("news_sec_8k", "news_prnewswire", "news_businesswire"), "material official news, clustered, official source canonical"),
    SignalFamily("DISCOVERY", "US", "1", ("EARLY_DISCOVERY",), ("discovery_engine",), "early-discovery stage transition (never a recommendation)", context_only=True),
    # ---- V2 US candidates
    SignalFamily("US_13F_NEW_POSITION", "US", "1", ("INST_13F_NEW_POSITION",), ("sec_13f",), "focused manager (10-150 positions) opens a >= 3 % position; known at the 13F filing date, 45+ days after the quarter"),
    SignalFamily("US_13F_ACCUMULATION", "US", "1", ("INST_13F_ACCUMULATION",), ("sec_13f",), "3rd focused manager opening a new position in the same CUSIP for one quarter (point in time)"),
    SignalFamily("US_13F_CONTEXT", "US", "1", ("INST_13F_EXIT", "INST_13F_DECREASE"), ("sec_13f",), "focused manager exits / cuts (>10 %) a position (context, never a candidate)", context_only=True),
    SignalFamily("10K_NEGATIVE_CHANGE", "US", "1", (), ("sec_10k",), "TENK_CHANGE with direction NEGATIVE (risk research only; no short orders)", context_only=True,
                 notes="measured by app.backtest.research_v2.tenk_negative (drawdown, delisting, bankruptcy, dilution, sector-adjusted)"),
    SignalFamily("US_SHORT_PRESSURE", "US", "1", ("SHORT_PRESSURE",), ("finra_short_volume",), "5d FINRA short-VOLUME ratio >= 60 % and z >= 2.5 vs 60 sessions (volume, not short interest)"),
    SignalFamily("US_FTD_SPIKE", "US", "1", ("FTD_SPIKE",), ("sec_ftd",), "half-month max daily FTD value >= $1M and >= 5x the prior 6 half-months' median"),
    SignalFamily("US_TRIAL_CATALYST", "US", "1", ("RESULTS_POSTED", "TRIAL_TERMINATED", "TRIAL_PHASE_CHANGE", "TRIAL_START", "TRIAL_COMPLETION", "TRIAL_SUSPENDED"),
                 ("us_clinical_trials",), "lead-sponsor (industry) trial events; status changes forward-only"),
    SignalFamily("US_FDA_EVENT", "US", "1", ("FDA_APPROVAL", "FDA_EFFICACY_SUPPLEMENT"), ("us_openfda",), "Drugs@FDA original / efficacy approvals by sponsor"),
    SignalFamily("US_GOV_GRANT", "US", "0", ("GOV_GRANT",), ("usaspending",), "federal grant to a listed recipient (USAspending assistance awards)", notes="adapter extension pending"),
    SignalFamily("US_GOV_LOBBY_CONVERGENCE", "US", "0", (), ("senate_lda",), "lobbying client with new federal contract", context_only=True,
                 notes="BLOCKED: lda.gov answers 403 (Akamai) from the tested network"),
    SignalFamily("US_PATENT_ACCELERATION", "US", "0", (), ("uspto_odp",), "assignee patent grant acceleration", context_only=True,
                 notes="AUTH_REQUIRED: PatentsView/USPTO ODP needs an API key"),
    # ---- TR candidates (adapter parsers exist; live access blocked, see DATA_SOURCE_COST_REPORT)
    SignalFamily("TR_INSIDER_BUY", "TR", "0", ("TR_INSIDER_BUY",), ("kap",), "pay alım satım bildirimi: insider / major holder purchase", notes="amounts only in PDF attachments; KAP list API behind WAF"),
    SignalFamily("TR_BUYBACK", "TR", "0", ("TR_BUYBACK",), ("kap",), "pay geri alım execution table"),
    SignalFamily("TR_CONTRACT", "TR", "0", ("TR_CONTRACT",), ("kap",), "iş ilişkisi / sözleşme disclosures"),
    SignalFamily("TR_CAPACITY_INVESTMENT", "TR", "0", ("TR_CAPACITY_INVESTMENT",), ("kap",), "kapasite / fabrika / yatırım disclosures"),
    SignalFamily("TR_DILUTION_RISK", "TR", "1", ("TR_RIGHTS_ISSUE_APPROVAL",), ("tr_spk_bulletin",), "SPK-approved bedelli share issue (dilution %)", context_only=True),
    SignalFamily("TR_BONUS_ISSUE", "TR", "1", ("TR_BONUS_ISSUE_APPROVAL",), ("tr_spk_bulletin",), "SPK-approved bedelsiz (bonus) share issue, % of current capital"),
    SignalFamily("TR_SPK_SANCTION", "TR", "1", ("TR_SPK_ENFORCEMENT",), ("tr_spk_bulletin",), "SPK criminal complaint / trading ban / fine concerning trading in the stock", context_only=True),
    # ---- CA candidates
    SignalFamily("CA_GOV_CONTRACT", "CA", "1", ("CA_GOV_CONTRACT_WIN",), ("ca_canadabuys",), "federal contract award to a listed supplier (publication date)"),
    SignalFamily("CA_GRANT", "CA", "0", (), ("ca_grants",), "federal grant / contribution (forward only: no publication date in the file)", context_only=True),
    SignalFamily("CA_INSIDER_BUY", "CA", "0", (), ("sedi",), "SEDI insider purchase", notes="BLOCKED: SEDI behind a bot manager; needs an authorised feed"),
    SignalFamily("CA_SHORT_POSITION_CHANGE", "CA", "0", (), ("ciro_short",), "CIRO consolidated short position change", notes="BLOCKED: ciro.ca answers 403 to automated clients from both tested networks (office + Hetzner server)"),
    SignalFamily("CA_MINING_STAGE_ADVANCE", "CA", "0", ("CA_RESOURCE_ESTIMATE", "CA_FEASIBILITY"), ("newswire_ca",), "PEA->PFS->DFS / resource upgrades from issuer releases (normalized, never graded 'good')"),
)
BY_ID = {f.signal_id: f for f in FAMILIES}


def as_rows() -> list[dict]:
    return [{**asdict(f), "source_event_types": "|".join(f.source_event_types), "source_datasets": "|".join(f.source_datasets),
             "tags": "|".join(f.tags)} for f in FAMILIES]
