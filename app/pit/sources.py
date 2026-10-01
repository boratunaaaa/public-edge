"""Point-in-time sources for historical replay (pure functions; app.jobs.pit_backfill and the replay export call them).

1. XBRL companyfacts -> facts with the filing that disclosed them (accession, filed date, form): cash, debt, operating
   cash flow and shares outstanding. A fact is visible from its filing's exact acceptance time when known, else from
   the day AFTER the filing date (same rule as replay share facts).
2. SEC submissions -> the COMPLETE filing history: the "recent" block plus every older page listed in filings.files
   (same columnar layout), and an accession -> acceptance-time map for (1).
3. CASH_RUNWAY at a moment: production app.discovery.metrics formula (latest cash / (-annual OCF / 12) when OCF < 0)
   over the facts visible at that moment only.

Nothing here reads the database or the network."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from app.marketdata.calendar import NY

CONCEPTS = {  # our family -> (taxonomy, XBRL concepts in preference order, unit)
    "CASH": ("us-gaap", ("CashAndCashEquivalentsAtCarryingValue",
                         "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "Cash"), "USD"),
    "DEBT": ("us-gaap", ("LongTermDebt", "LongTermDebtNoncurrent", "LongTermDebtCurrent", "DebtCurrent",
                         "ConvertibleNotesPayable"), "USD"),
    "OCF": ("us-gaap", ("NetCashProvidedByUsedInOperatingActivities",
                        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"), "USD"),
    "SHARES_DEI": ("dei", ("EntityCommonStockSharesOutstanding",), "shares"),
    "SHARES_GAAP": ("us-gaap", ("CommonStockSharesOutstanding",), "shares"),
}
# production (app.discovery.sec_data.load_fundamentals) reads Cash = CashAndCashEquivalentsAtCarryingValue (instant) and
# OCF_annual = NetCashProvidedByUsedInOperatingActivities from the SEC "CY" frames, i.e. a ~one-year duration fact
# (365 +/- 30 days) of any form. The runway uses exactly those two concepts; the other concepts are stored for
# research but never replace them.
RUNWAY_CASH, RUNWAY_OCF = "CashAndCashEquivalentsAtCarryingValue", "NetCashProvidedByUsedInOperatingActivities"
ANNUAL_DAYS = (335, 395)
_RANK = {(fam, name): i for fam, (_t, names, _u) in CONCEPTS.items() for i, name in enumerate(names)}


@dataclass(frozen=True)
class PitFact:
    cik: str
    family: str
    concept: str
    start: date | None
    end: date
    val: float
    filed: date
    accession: str
    form: str | None


def _d(s):
    return date.fromisoformat(s) if s else None


def parse_companyfacts(cik: str, data: dict) -> list[PitFact]:
    facts = data.get("facts") or {}
    out = []
    for fam, (tax, names, unit) in CONCEPTS.items():
        for name in names:
            for f in (((facts.get(tax) or {}).get(name) or {}).get("units") or {}).get(unit) or []:
                try:  # one malformed fact is skipped, never the whole issuer
                    end, filed, acc = _d(f["end"]), _d(f["filed"]), f["accn"]
                    start, val = _d(f.get("start")), float(f["val"])
                except (KeyError, ValueError, TypeError):
                    continue
                if not (end and filed and acc):
                    continue
                out.append(PitFact(cik.zfill(10), fam, name, start, end, val, filed, acc, f.get("form")))
    return sorted(out, key=lambda x: (x.family, x.filed, x.accession, x.concept, x.end))


def merge_submission_pages(recent: dict, pages: list[dict]) -> dict:
    """The recent block and every older page (filings.files -> CIK##########-submissions-NNN.json) in one columnar
    dict, duplicates (same accession) removed, newest first like the recent block."""
    cols = sorted(set(recent) | {k for p in pages for k in p})
    rows: dict[str, dict] = {}
    for block in [recent] + pages:
        n = len(block.get("accessionNumber") or [])
        for i in range(n):
            acc = block["accessionNumber"][i]
            row = {c: (block.get(c) or [])[i] if i < len(block.get(c) or []) else None for c in cols}
            if acc not in rows:
                rows[acc] = row
            else:  # the same filing on two pages: keep what is known, fill what is missing (e.g. its acceptance time)
                rows[acc] = {c: rows[acc].get(c) if rows[acc].get(c) not in (None, "") else row.get(c) for c in cols}
    ordered = sorted(rows.values(), key=lambda r: (r.get("filingDate") or "", r.get("acceptanceDateTime") or ""), reverse=True)
    return {c: [r.get(c) for r in ordered] for c in cols}


def acceptance_map(filings: dict) -> dict[str, datetime]:
    """accession -> aware acceptance time (EDGAR acceptanceDateTime is UTC, e.g. '2023-02-14T16:05:12.000Z')."""
    out = {}
    for acc, at in zip(filings.get("accessionNumber") or [], filings.get("acceptanceDateTime") or []):
        if acc and at:
            try:
                t = datetime.fromisoformat(at.replace("Z", "+00:00"))
            except ValueError:
                continue
            # EDGAR publishes UTC; a value without an offset is read as UTC explicitly, never in the host's zone
            out[acc] = (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    return out


def _aware(at: datetime) -> datetime:
    if at.tzinfo is None:
        raise ValueError("a replay moment must be timezone-aware")
    return at


def visible(f: PitFact, acceptance: dict, at: datetime) -> bool:
    at = _aware(at)
    t = acceptance.get(f.accession)
    if t is not None:
        return t <= at
    return f.filed < at.astimezone(NY).date()


def _accepted(f: PitFact, acceptance: dict) -> datetime:
    """When the filing was accepted: the exact time if known, else the end of its filing day in New York (the moment
    a date-only filing becomes visible) - so a same-day correction accepted later wins over the earlier filing,
    whatever the accession numbers. Deliberate, documented tie-break: of two same-day filings of which only one has a
    known time, the one WITHOUT a time is treated as the later one (it is only known from the end of that day)."""
    t = acceptance.get(f.accession)
    if t is not None:
        return t.astimezone(timezone.utc)
    return datetime.combine(f.filed, time(23, 59, 59), NY).astimezone(timezone.utc)


def runway_months(facts: list[PitFact], acceptance: dict, at: datetime) -> float | None:
    """Production formula on what was public at `at`: the latest cash instant and the latest annual operating cash
    flow (by period end, then by filing - a restatement replaces), runway only when that OCF is negative."""
    at = _aware(at)  # rejected even when there is no fact to look at
    seen = [f for f in facts if visible(f, acceptance, at)]

    def latest(fs):
        # latest period, then latest filing (a restatement replaces); within ONE filing the production-preferred
        # concept wins (CONCEPTS order); a remaining tie is broken by value - never by input order
        return max(fs, key=lambda x: (x.end, x.filed, _accepted(x, acceptance), x.accession, -_RANK[(x.family, x.concept)],
                                      x.val)) if fs else None
    cash = latest([f for f in seen if f.concept == RUNWAY_CASH])
    ocf = latest([f for f in seen if f.concept == RUNWAY_OCF and f.start
                  and ANNUAL_DAYS[0] <= (f.end - f.start).days <= ANNUAL_DAYS[1]])
    if cash is None or ocf is None or ocf.val >= 0:
        return None
    return cash.val / (-ocf.val / 12)


def acceptance_coverage(facts: list[PitFact], acceptance: dict) -> dict:
    """How many facts resolve to an exact acceptance time vs the next-day fallback (a drift monitor: companyfacts
    and submissions must use the same dashed accession format)."""
    exact = sum(1 for f in facts if f.accession in acceptance)
    return {"facts": len(facts), "exact": exact, "fallback": len(facts) - exact,
            "exact_share": round(exact / len(facts), 4) if facts else None}
