"""FINSABER V2 open dataset (S&P 500 incl. delisted members, 2000-2025) - daily prices WITH a CIK column.

Evaluated 2026-09-25: huggingface.co/datasets/finsaber-team/FINSABER-V2-Data, price_daily parquet per year
(~5 MB/year). License tag "other"; the card makes users responsible for the underlying data providers'
terms -> used internally for research only, never redistributed. Coverage for our unresolved events is
small (S&P 500 members only) but identity is strong: the dataset's own CIK column.
Only the needed years are downloaded (no full archive), each file is hashed; rows are filtered to the
CIKs that have unresolved events.
"""
from __future__ import annotations

import hashlib
import io
import logging
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app import http
from app.marketdata.provider import Bar
from app.pricesources.store import save_source

log = logging.getLogger(__name__)
FILE_URL = "https://huggingface.co/datasets/finsaber-team/FINSABER-V2-Data/resolve/main/price_daily/year={y}/part-000.parquet"


def unresolved_ciks(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text("""SELECT DISTINCT issuer_cik FROM events WHERE returns_status = 'no_data'
                                                   AND NOT superseded AND issuer_cik IS NOT NULL""")).all()}


def load_year(y: int, ciks: set[str], fetch=None) -> tuple[dict[tuple[str, str], list[Bar]], str]:
    import pyarrow.parquet as pq
    body = (fetch or (lambda url: http.get(url, retries=2).content))(FILE_URL.format(y=y))
    sha = hashlib.sha256(body).hexdigest()
    t = pq.read_table(io.BytesIO(body), columns=["date", "symbol", "cik", "open", "high", "low", "close",
                                                  "adjusted_close", "volume"]).to_pylist()
    out: dict[tuple[str, str], list[Bar]] = {}
    for r in t:
        cik = str(r["cik"] or "").split(".")[0].zfill(10)
        if cik not in ciks:
            continue
        d = r["date"] if isinstance(r["date"], date) else date.fromisoformat(str(r["date"])[:10])
        out.setdefault((cik, str(r["symbol"]).upper()), []).append(
            Bar(d, r["open"], r["high"], r["low"], r["close"], r["adjusted_close"],
                int(r["volume"]) if r["volume"] is not None else None))
    return out, sha


SOURCE_END = date(2025, 12, 31)  # last date of the V2 snapshot


def splice_ticker_changes(parts: dict[str, list[Bar]]) -> list[tuple[str, list[Bar], list[str]]]:
    """Same CIK, consecutive symbols (COG -> CTRA, VIAC -> PARA): one series when the next symbol starts
    within 9 calendar days of the previous one's end and the price is continuous (|jump| < 50%).
    A long gap (e.g. old equity cancelled in bankruptcy, new equity issued later) is never spliced."""
    segs = sorted(((s, sorted(b, key=lambda x: x.date)) for s, b in parts.items() if b), key=lambda t: t[1][0].date)
    out: list[tuple[str, list[Bar], list[str]]] = []
    for sym, bars in segs:
        if out:
            psym, pbars, chain = out[-1]
            gap = (bars[0].date - pbars[-1].date).days
            pa, pb = (pbars[-1].adj_close or pbars[-1].close), (bars[0].adj_close or bars[0].close)
            if 0 < gap <= 9 and pa and pb and abs(pb / pa - 1) < 0.5:
                out[-1] = (sym, pbars + bars, chain + [sym])
                continue
        out.append((sym, bars, [sym]))
    return out


def run(engine: Engine, years: tuple[int, ...] = (2020, 2021, 2022, 2023, 2024, 2025), fetch=None) -> dict:
    ciks = unresolved_ciks(engine)
    series: dict[tuple[str, str], list[Bar]] = {}
    shas = {}
    for y in years:
        part, shas[y] = load_year(y, ciks, fetch)
        for k, v in part.items():
            series.setdefault(k, []).extend(v)
    ref = "hf://finsaber-team/FINSABER-V2-Data/price_daily " + " ".join(f"{y}:{shas[y][:16]}" for y in years)
    by_cik: dict[str, dict[str, list[Bar]]] = {}
    for (cik, sym), bars in series.items():
        by_cik.setdefault(cik, {})[sym] = bars
    out = {"ciks_with_rows": len(by_cik), "series": 0, "spliced": 0, "truncated": 0, "verdicts": {}}
    from app.securitymaster.provider import get_security_master
    for cik, parts in by_cik.items():
        with engine.connect() as conn:
            dl = get_security_master(conn).delisting(cik)
        for sym, bars, chain in splice_ticker_changes(parts):
            truncated = False
            if dl and dl.date and bars[-1].date > dl.date + timedelta(days=30):
                pre = [b for b in bars if b.date <= dl.date]
                if len(pre) >= 20 and pre[0].date < dl.date - timedelta(days=30):
                    bars, truncated = pre, True  # keep only the exchange-listed part; OTC/successor quotes dropped
            with engine.begin() as conn:
                _, v = save_source(conn, cik=cik, symbol=sym, provider="finsaber", bars=bars, identity="cik", source_ref=ref,
                                   source_end=SOURCE_END, truncated_at_delisting=truncated,
                                   extra_checks={"symbol_chain": chain})
            out["series"] += 1
            out["spliced"] += len(chain) > 1
            out["truncated"] += truncated
            out["verdicts"][v.status] = out["verdicts"].get(v.status, 0) + 1
    return out
