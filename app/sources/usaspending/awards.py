"""Federal contract awards from the official USAspending.gov API."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy.engine import Connection, Engine

from app import http
from app.db.models import government_awards
from app.db.store import create_event, insert_rows, job_state, save_job_state
from app.entities.registry import resolver
from app.marketdata.provider import get_market_cap
from app.parsers.sec_common import to_date
from app.signals import priority as P
from app.signals.detect import _money, multi_signal_check
from app.sources.base import RunResult, Source

log = logging.getLogger(__name__)
SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
AWARD_URL = "https://api.usaspending.gov/api/v2/awards/{gid}/"
PUBLIC_URL = "https://www.usaspending.gov/award/{gid}"
MIN_AMOUNT = 5_000_000
PARENT_LOOKUP_MIN = 25_000_000
EVENT_MIN_RATIO = 0.005
FIELDS = ["Award ID", "Recipient Name", "Recipient UEI", "Award Amount", "Base Obligation Date", "Start Date", "End Date",
          "Awarding Agency", "Awarding Sub Agency", "Description", "generated_internal_id"]
# DoD contract actions are published with a 90-day delay (FPDS policy); civilian ~3 business days.
DOD_PUBLICATION_LAG = 90
CIVILIAN_PUBLICATION_LAG = 5


def conservative_public_date(award_date: date, agency: str | None) -> date:
    """Earliest date an outside investor could have known about a HISTORICAL award."""
    lag = DOD_PUBLICATION_LAG if agency and "defense" in agency.lower() else CIVILIAN_PUBLICATION_LAG
    return award_date + timedelta(days=lag)


def search_new_awards(start: date, end: date, page: int = 1, min_amount: float = MIN_AMOUNT,
                      date_type: str = "new_awards_only") -> dict:
    body = {
        "filters": {"award_type_codes": ["A", "B", "C", "D"],
                    "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat(), "date_type": date_type}],
                    "award_amounts": [{"lower_bound": min_amount}]},
        "fields": FIELDS, "sort": "Award Amount", "order": "desc", "limit": 100, "page": page,
    }
    return http.post(SEARCH_URL, json=body, retries=3).json()


def parent_name(gid: str) -> str | None:
    try:
        d = http.get(AWARD_URL.format(gid=gid), retries=1).json()
    except Exception:
        return None
    rec = d.get("recipient") or {}
    return rec.get("parent_recipient_name")


MAPPING_RANK = {"VERIFIED": 4, "HIGH_CONFIDENCE": 3, "MEDIUM": 2, "LOW": 1}


def map_recipient(name: str, parent: str | None, *, manual: dict[str, str] | None = None) -> tuple[str | None, str | None, str]:
    """(ticker, confidence, source) for an award recipient -> listed parent.

    VERIFIED        curated alias (entity_aliases) or recipient AND USAspending parent both resolve
                    to the same SEC registrant
    HIGH_CONFIDENCE the recipient's own name is exactly (normalized) one SEC registrant
    MEDIUM          only USAspending's parent-recipient name resolves (subsidiary -> parent)
    LOW             anything weaker - stored for research, never a high-priority alert"""
    manual = manual or {}
    norm_name = (name or "").strip().upper()
    if norm_name in manual:
        return manual[norm_name], "VERIFIED", "entity_aliases (curated)"
    t_name = resolver.ticker_for_name(name)
    t_parent = resolver.ticker_for_name(parent) if parent and parent.strip().upper() != norm_name else None
    if t_name and t_parent and t_name == t_parent:
        return t_name, "VERIFIED", "recipient name + USAspending parent -> same SEC registrant"
    if t_name:
        return t_name, "HIGH_CONFIDENCE", "recipient name = SEC registrant name (exact, unique)"
    if t_parent:
        return t_parent, "MEDIUM", "USAspending parent_recipient_name = SEC registrant name"
    if parent:
        return None, "LOW", "parent name did not resolve uniquely"
    return None, None, "unmapped"


def _manual_aliases(conn: Connection) -> dict[str, str]:
    from sqlalchemy import select
    from app.db.models import entity_aliases
    rows = conn.execute(select(entity_aliases.c.alias_norm, entity_aliases.c.ticker).where(
        entity_aliases.c.category == "GOV_RECIPIENT", entity_aliases.c.ticker.isnot(None))).all()
    return {a.upper(): t for a, t in rows}


def process_award(conn: Connection, a: dict, *, is_backfill: bool, first_seen: date) -> list[int]:
    gid = a.get("generated_internal_id")
    amount = float(a.get("Award Amount") or 0)
    if not gid or amount <= 0:
        return []
    award_date = to_date(a.get("Base Obligation Date")) or to_date(a.get("Start Date"))
    name = a.get("Recipient Name") or ""
    manual = _manual_aliases(conn)
    ticker, mconf, msrc = map_recipient(name, None, manual=manual)
    parent = None
    if amount >= PARENT_LOOKUP_MIN and mconf != "VERIFIED":
        parent = parent_name(gid)  # also upgrades HIGH_CONFIDENCE to VERIFIED when both agree
        if parent:
            ticker, mconf, msrc = map_recipient(name, parent, manual=manual)
    # Live: the documented date is the day this system first saw the record, with the amount
    # shown on that day. Backfill: USAspending exposes neither the original publication date nor
    # the amount known back then -> the date is ESTIMATED and kept out of verified stats/multi-signal.
    if is_backfill:
        if not award_date:
            return []
        public_date, date_quality = conservative_public_date(award_date, a.get("Awarding Agency")), "estimated"
        basis, amount_basis = "estimated", "current_total_at_backfill"
    else:
        public_date, date_quality = first_seen, "first_seen"
        basis, amount_basis = "first_seen", "amount_at_first_seen"
    mcap = ratio = None
    if ticker:
        try:
            # point-in-time: shares known on the public date x price on that date (None = unknown)
            mcap = get_market_cap(conn, ticker, public_date)
        except Exception as e:
            log.warning("market cap %s failed: %s", ticker, e)
        ratio = (amount / mcap) if mcap else None
    level = P.contract_level(ratio)
    url = PUBLIC_URL.format(gid=gid)
    cid = resolver.ticker_to_cik.get(ticker) if ticker else None
    prio = P.contract_priority(ratio) if ratio is not None else 0
    if mconf in ("MEDIUM", "LOW"):
        # subsidiary -> parent attribution is not verified: research only, below the alert threshold
        prio = min(prio, 3)
    insert_rows(conn, government_awards, [{
        "award_key": gid, "award_id": a.get("Award ID"), "recipient_name": name, "recipient_uei": a.get("Recipient UEI"),
        "parent_name": parent, "amount": amount, "award_date": award_date, "start_date": to_date(a.get("Start Date")),
        "end_date": to_date(a.get("End Date")), "agency": a.get("Awarding Agency"), "subagency": a.get("Awarding Sub Agency"),
        "description": (a.get("Description") or "")[:4000], "ticker": ticker, "market_cap": mcap, "value_to_mcap": ratio,
        "mcap_level": level, "first_seen_date": first_seen, "public_date_basis": basis, "amount_basis": amount_basis,
        "recipient_cik": cid, "mapping_confidence": mconf, "mapping_source": msrc, "source_url": url,
    }], ["award_key"])  # first observation wins: later amount changes never rewrite history
    created = []
    if ticker and ratio is not None and ratio >= EVENT_MIN_RATIO:
        eid = create_event(
            conn, event_type="GOV_CONTRACT", dedupe_key=f"AWARD:{gid}", public_date=public_date, transaction_date=award_date,
            ticker=ticker, company_name=resolver.cik_to_name.get(cid or "", name), issuer_cik=cid,
            priority=prio,
            title=f"{_money(amount)} federal contract to {name} (${ticker}) = {ratio * 100:.1f}% of market cap",
            payload={"recipient": name, "parent": parent, "amount": amount, "award_date": award_date,
                     "agency": a.get("Awarding Agency"), "subagency": a.get("Awarding Sub Agency"),
                     "description": (a.get("Description") or "")[:500], "period": [a.get("Start Date"), a.get("End Date")],
                     "market_cap": mcap, "value_to_mcap": ratio, "level": level, "award_id": a.get("Award ID"),
                     "public_date_basis": basis, "amount_basis": amount_basis,
                     "mapping_confidence": mconf, "mapping_source": msrc},
            source_url=url, is_backfill=is_backfill, date_quality=date_quality,
        )
        if eid:
            created.append(eid)
            m = multi_signal_check(conn, ticker, public_date, is_backfill=is_backfill)
            if m:
                created.append(m)
    return created


def ingest_range(engine: Engine, start: date, end: date, *, is_backfill: bool, max_pages: int = 50,
                 date_type: str = "new_awards_only", min_base_date: date | None = None) -> RunResult:
    res = RunResult()
    with engine.connect() as conn:
        resolver.load(conn)
    for page in range(1, max_pages + 1):
        d = search_new_awards(start, end, page, date_type=date_type)
        rows = d.get("results") or []
        for a in rows:
            if min_base_date is not None:
                bd = to_date(a.get("Base Obligation Date"))
                if bd is None or bd < min_base_date:
                    continue  # a modification of an old award, not a new contract
            try:
                with engine.begin() as conn:
                    res.events += process_award(conn, a, is_backfill=is_backfill, first_seen=date.today())
                res.items += 1
            except Exception as e:
                log.warning("award %s failed: %s", a.get("generated_internal_id"), e)
        if not (d.get("page_metadata") or {}).get("hasNext"):
            break
    return res


class UsaSpendingSource(Source):
    name = "usaspending"
    interval_seconds = 6 * 3600
    slow = True

    def run_live(self, engine: Engine) -> RunResult:
        # Records published/updated recently whose award was signed in the last 150 days.
        # (date_signed alone would miss DoD awards, which appear ~90 days after signing.)
        end = date.today()
        return ingest_range(engine, end - timedelta(days=3), end, is_backfill=False, date_type="last_modified_date",
                            min_base_date=end - timedelta(days=150))

    def run_backfill(self, engine: Engine, years: int = 5) -> RunResult:
        name = "backfill_usaspending"
        with engine.begin() as conn:
            st = job_state(conn, name)
        done = set(st.get("done", []))
        total = RunResult()
        today = date.today()
        m = date(today.year - years, today.month, 1)
        while m < today - timedelta(days=12):
            key = m.strftime("%Y-%m")
            nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
            if key not in done:
                r = ingest_range(engine, m, nxt - timedelta(days=1), is_backfill=True)
                total.items += r.items
                done.add(key)
                with engine.begin() as conn:
                    save_job_state(conn, name, {"done": sorted(done), "last": key, "awards": r.items}, status="running")
            m = nxt
        with engine.begin() as conn:
            save_job_state(conn, name, {"done": sorted(done)}, status="done")
        return total
