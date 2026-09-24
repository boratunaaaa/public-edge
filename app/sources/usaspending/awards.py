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


def process_award(conn: Connection, a: dict, *, is_backfill: bool, first_seen: date) -> list[int]:
    gid = a.get("generated_internal_id")
    amount = float(a.get("Award Amount") or 0)
    if not gid or amount <= 0:
        return []
    award_date = to_date(a.get("Base Obligation Date")) or to_date(a.get("Start Date"))
    name = a.get("Recipient Name") or ""
    ticker = resolver.ticker_for_name(name)
    parent = None
    if not ticker and amount >= PARENT_LOOKUP_MIN:
        parent = parent_name(gid)
        if parent and parent.strip().upper() != name.strip().upper():
            ticker = resolver.ticker_for_name(parent)
    public_date = conservative_public_date(award_date, a.get("Awarding Agency")) if (is_backfill and award_date) else first_seen
    mcap = ratio = None
    if ticker:
        try:
            mcap = get_market_cap(conn, ticker, award_date or public_date)
        except Exception as e:
            log.warning("market cap %s failed: %s", ticker, e)
        ratio = (amount / mcap) if mcap else None
    level = P.contract_level(ratio)
    url = PUBLIC_URL.format(gid=gid)
    cid = resolver.ticker_to_cik.get(ticker) if ticker else None
    insert_rows(conn, government_awards, [{
        "award_key": gid, "award_id": a.get("Award ID"), "recipient_name": name, "recipient_uei": a.get("Recipient UEI"),
        "parent_name": parent, "amount": amount, "award_date": award_date, "start_date": to_date(a.get("Start Date")),
        "end_date": to_date(a.get("End Date")), "agency": a.get("Awarding Agency"), "subagency": a.get("Awarding Sub Agency"),
        "description": (a.get("Description") or "")[:4000], "ticker": ticker, "market_cap": mcap, "value_to_mcap": ratio,
        "mcap_level": level, "source_url": url,
    }], ["award_key"])
    created = []
    if ticker and ratio is not None and ratio >= EVENT_MIN_RATIO:
        eid = create_event(
            conn, event_type="GOV_CONTRACT", dedupe_key=f"AWARD:{gid}", public_date=public_date, transaction_date=award_date,
            ticker=ticker, company_name=resolver.cik_to_name.get(cid or "", name), issuer_cik=cid,
            priority=P.contract_priority(ratio),
            title=f"{_money(amount)} federal contract to {name} (${ticker}) = {ratio * 100:.1f}% of market cap",
            payload={"recipient": name, "parent": parent, "amount": amount, "award_date": award_date,
                     "agency": a.get("Awarding Agency"), "subagency": a.get("Awarding Sub Agency"),
                     "description": (a.get("Description") or "")[:500], "period": [a.get("Start Date"), a.get("End Date")],
                     "market_cap": mcap, "value_to_mcap": ratio, "level": level, "award_id": a.get("Award ID"),
                     "public_date_basis": "first_seen" if not is_backfill else "award_date+publication_lag"},
            source_url=url, is_backfill=is_backfill,
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
