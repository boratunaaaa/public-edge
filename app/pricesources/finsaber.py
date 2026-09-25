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
from datetime import date

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


def run(engine: Engine, years: tuple[int, ...] = (2020, 2021, 2022, 2023, 2024, 2025), fetch=None) -> dict:
    ciks = unresolved_ciks(engine)
    series: dict[tuple[str, str], list[Bar]] = {}
    shas = {}
    for y in years:
        part, shas[y] = load_year(y, ciks, fetch)
        for k, v in part.items():
            series.setdefault(k, []).extend(v)
    ref = "hf://finsaber-team/FINSABER-V2-Data/price_daily " + " ".join(f"{y}:{shas[y][:16]}" for y in years)
    out = {"ciks_with_rows": len({c for c, _ in series}), "series": len(series), "verdicts": {}}
    for (cik, sym), bars in series.items():
        with engine.begin() as conn:
            _, v = save_source(conn, cik=cik, symbol=sym, provider="finsaber", bars=bars, identity="cik", source_ref=ref)
        out["verdicts"][v.status] = out["verdicts"].get(v.status, 0) + 1
    return out
