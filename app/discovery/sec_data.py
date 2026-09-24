"""SEC data for discovery: full-text search, universe snapshot, XBRL frames fundamentals."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from app import http
from app.db.models import companies, fundamentals_quarterly, tickers, universe_snapshots
from app.news.classify import mcap_bucket

log = logging.getLogger(__name__)
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
FRAMES_URL = "https://data.sec.gov/api/xbrl/frames/{tax}/{concept}/{unit}/{period}.json"
SPARK_URL = "https://query1.finance.yahoo.com/v7/finance/spark"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{s}"
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"}


# ------------------------------------------------------------------ full-text search
def efts_search(phrase: str, forms: list[str], start: date, end: date, max_hits: int = 500) -> list[dict]:
    """EDGAR full-text search for an exact phrase. Returns [{adsh, cik, form, file_date, filename, name}]."""
    out: list[dict] = []
    frm = 0
    while frm < max_hits:
        r = http.get(EFTS_URL, params={"q": f'"{phrase}"', "forms": ",".join(forms), "dateRange": "custom",
                                       "startdt": start.isoformat(), "enddt": end.isoformat(), "from": frm}, retries=2).json()
        hits = r.get("hits", {}).get("hits", [])
        if not hits:
            break
        for h in hits:
            s = h.get("_source", {})
            adsh, _, fn = h.get("_id", "").partition(":")
            for cik in s.get("ciks") or []:
                out.append({"adsh": adsh or s.get("adsh"), "cik": cik.zfill(10), "form": s.get("form") or s.get("file_type"),
                            "file_date": s.get("file_date"), "filename": fn, "name": (s.get("display_names") or [""])[0],
                            "items": s.get("items") or []})
        frm += len(hits)
        total = r.get("hits", {}).get("total", {}).get("value", 0)
        if frm >= total:
            break
    return out


def doc_url(cik: str, adsh: str, filename: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}/{filename}"


# ------------------------------------------------------------------ universe
def _quarters_back(n: int, today: date | None = None) -> list[str]:
    today = today or date.today()
    y, q = today.year, (today.month - 1) // 3 + 1
    out = []
    for _ in range(n):
        q -= 1
        if q == 0:
            y, q = y - 1, 4
        out.append(f"CY{y}Q{q}")
    return out


def shares_frames(n_quarters: int = 3) -> dict[str, tuple[int, date]]:
    """Latest cover-page shares per CIK from the last n calendar-quarter instant frames."""
    best: dict[str, tuple[int, date]] = {}
    for p in _quarters_back(n_quarters):
        try:
            d = http.get(FRAMES_URL.format(tax="dei", concept="EntityCommonStockSharesOutstanding", unit="shares", period=p + "I")).json()
        except http.HttpError as e:
            if e.status == 404:
                continue
            raise
        for row in d.get("data", []):
            cik = str(row["cik"]).zfill(10)
            end = date.fromisoformat(row["end"])
            if cik not in best or end > best[cik][1]:
                best[cik] = (int(row["val"]), end)
    return best


def spark_closes(symbols: list[str]) -> dict[str, tuple[float, int | None]]:
    """{symbol: (last close, last day volume)} in batches of 20 (provider limit)."""
    out: dict[str, tuple[float, int | None]] = {}
    for i in range(0, len(symbols), 20):
        batch = [s.replace(".", "-") for s in symbols[i:i + 20]]
        try:
            d = http.get(SPARK_URL, params={"symbols": ",".join(batch), "range": "5d", "interval": "1d"}, headers=BROWSER_UA, retries=2).json()
        except Exception as e:
            log.warning("spark batch failed: %s", e)
            continue
        for r in (d.get("spark") or {}).get("result") or []:
            try:
                resp = r["response"][0]
                closes = [c for c in resp["indicators"]["quote"][0]["close"] if c]
                if closes:
                    out[r["symbol"].replace("-", ".")] = (float(closes[-1]), resp["meta"].get("regularMarketVolume"))
            except (KeyError, IndexError, TypeError):
                continue
    return out


def adv_dollar_30d(symbol: str) -> float | None:
    try:
        d = http.get(CHART_URL.format(s=symbol.replace(".", "-")), params={"range": "3mo", "interval": "1d"},
                     headers=BROWSER_UA, retries=1).json()
        res = d["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        pairs = [(c, v) for c, v in zip(q["close"], q["volume"]) if c and v is not None][-30:]
        return sum(c * v for c, v in pairs) / len(pairs) if pairs else None
    except Exception:
        return None


def build_universe(engine: Engine, as_of: date | None = None, with_adv: bool = True) -> dict:
    as_of = as_of or date.today()
    shares = shares_frames()
    with engine.connect() as conn:
        rows = conn.execute(select(companies.c.cik, companies.c.name, tickers.c.symbol, tickers.c.id)
                            .join(tickers, tickers.c.company_id == companies.c.id).order_by(tickers.c.id)).all()
    primary: dict[str, tuple[str, str]] = {}
    for cik, name, sym, _ in rows:
        primary.setdefault(cik, (sym, name))  # first listed symbol = primary class
    cands = {cik: v for cik, v in primary.items() if cik in shares}
    prices = spark_closes([v[0] for v in cands.values()])
    recs = []
    for cik, (sym, name) in cands.items():
        p = prices.get(sym)
        if not p:
            continue
        sh, end = shares[cik]
        if (as_of - end).days > 400:
            continue  # stale cover page -> unknown market cap
        mcap = p[0] * sh
        recs.append({"as_of": as_of, "cik": cik, "ticker": sym, "name": name, "price": p[0], "shares": sh, "shares_end": end,
                     "market_cap": mcap, "bucket": mcap_bucket(mcap), "adv_dollar_30d": None})
    if with_adv:
        for r in recs:
            if r["bucket"] in ("MICRO", "EARLY_SMALL"):
                r["adv_dollar_30d"] = adv_dollar_30d(r["ticker"])
    with engine.begin() as conn:
        for i in range(0, len(recs), 1000):
            st = pg_insert(universe_snapshots).values(recs[i:i + 1000])
            conn.execute(st.on_conflict_do_update(index_elements=["as_of", "cik"], set_={
                c: st.excluded[c] for c in ("ticker", "name", "price", "shares", "shares_end", "market_cap", "bucket", "adv_dollar_30d")}))
    counts: dict[str, int] = {}
    for r in recs:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1
    return {"as_of": as_of, "companies": len(recs), "buckets": counts, "with_shares": len(shares)}


# ------------------------------------------------------------------ fundamentals (XBRL frames)
REVENUE_CONCEPTS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                    "RevenueFromContractWithCustomerIncludingAssessedTax"]
QUARTERLY = {"GrossProfit": "GrossProfit", "ResearchAndDevelopmentExpense": "RD", "OperatingIncomeLoss": "OperatingIncome",
             "NetIncomeLoss": "NetIncome"}
INSTANT = {"CashAndCashEquivalentsAtCarryingValue": "Cash", "RevenueRemainingPerformanceObligation": "RPO",
           "LiabilitiesCurrent": "CurrentLiabilities", "AssetsCurrent": "CurrentAssets"}
ANNUAL = {"NetCashProvidedByUsedInOperatingActivities": "OCF_annual", "PaymentsToAcquirePropertyPlantAndEquipment": "Capex_annual",
          "ShareBasedCompensation": "SBC_annual"}


def _frame(concept: str, period: str, unit: str = "USD") -> list[dict]:
    try:
        return http.get(FRAMES_URL.format(tax="us-gaap", concept=concept, unit=unit, period=period)).json().get("data", [])
    except http.HttpError as e:
        if e.status == 404:
            return []
        raise


def load_fundamentals(engine: Engine, n_quarters: int = 10, only_ciks: set[str] | None = None) -> dict:
    periods = _quarters_back(n_quarters)
    rows: list[dict] = []

    def add(cik, name, r):
        cik = str(cik).zfill(10)
        if only_ciks is not None and cik not in only_ciks:
            return
        rows.append({"cik": cik, "concept": name, "period_end": date.fromisoformat(r["end"]),
                     "period_start": date.fromisoformat(r["start"]) if r.get("start") else None,
                     "val": float(r["val"]), "accession": r.get("accn")})

    for p in periods:
        seen_rev: set[str] = set()
        for concept in REVENUE_CONCEPTS:  # preference order; first concept wins per company-period
            for r in _frame(concept, p):
                cik = str(r["cik"]).zfill(10)
                if cik in seen_rev:
                    continue
                seen_rev.add(cik)
                add(cik, "Revenue", r)
        for concept, name in QUARTERLY.items():
            for r in _frame(concept, p):
                add(r["cik"], name, r)
        for concept, name in INSTANT.items():
            for r in _frame(concept, p + "I"):
                add(r["cik"], name, r)
        try:  # cover-page share count per quarter -> dilution
            for r in http.get(FRAMES_URL.format(tax="dei", concept="EntityCommonStockSharesOutstanding", unit="shares",
                                                period=p + "I")).json().get("data", []):
                add(r["cik"], "Shares", r)
        except http.HttpError as e:
            if e.status != 404:
                raise
    for y in {int(p[2:6]) for p in periods[:6]}:
        for concept, name in ANNUAL.items():
            for r in _frame(concept, f"CY{y}"):
                add(r["cik"], name, r)
    with engine.begin() as conn:
        for i in range(0, len(rows), 2000):
            st = pg_insert(fundamentals_quarterly).values(rows[i:i + 2000])
            conn.execute(st.on_conflict_do_update(index_elements=["cik", "concept", "period_end"],
                                                  set_={"val": st.excluded.val, "period_start": st.excluded.period_start,
                                                        "accession": st.excluded.accession}))
    return {"rows": len(rows), "periods": periods}


def series(conn, cik: str, concept: str) -> list[tuple[date, float]]:
    rows = conn.execute(select(fundamentals_quarterly.c.period_end, fundamentals_quarterly.c.val).where(
        fundamentals_quarterly.c.cik == cik, fundamentals_quarterly.c.concept == concept)
        .order_by(fundamentals_quarterly.c.period_end)).all()
    return [(r[0], float(r[1])) for r in rows]


def since(days: int) -> date:
    return date.today() - timedelta(days=days)
