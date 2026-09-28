"""V2 candidate signal families built from the new datasets. Each is a HYPOTHESIS to be measured (app.core.signal_registry),
never assumed bullish or bearish; events are low priority (no Telegram alert) until a family is validated.

US_FTD_SPIKE        a symbol's largest daily fails-to-deliver value in a half-month file >= $1M and >= 5x the median of its
                    previous 6 half-months (event at the file's publication).
US_SHORT_PRESSURE   5-day mean FINRA short-volume ratio >= 0.60 and z >= 2.5 vs the prior 60 sessions, volume >= 200k/day
                    (event at the file's publication; one per symbol per 30 days).
US_13F_NEW_POSITION a focused manager (10-150 positions) opens a position >= 3 % of its reported portfolio.
US_13F_ACCUMULATION the 3rd distinct focused manager to OPEN a new position in the same CUSIP for one quarter (event at
                    the filing that crosses the threshold - point in time).
Identity: symbol + date (FINRA / FTD) or CUSIP (13F) must resolve to exactly one CIK; otherwise no event."""
from __future__ import annotations

import statistics
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db.store import create_event
from app.markets.identity import cusip_to_cik, symbol_to_cik

FTD_MIN_VALUE = 1_000_000
FTD_MULT = 5.0
SV_RATIO, SV_Z, SV_MIN_VOL = 0.60, 2.5, 200_000
NEWPOS_WEIGHT = 0.03
ACCUM_MANAGERS = 3


def _ticker(conn, cik):
    from app.entities.registry import resolver
    return resolver.ticker_for_cik(cik)


def ftd_spikes(conn: Connection, source_file: str, *, is_backfill: bool) -> list[int]:
    rows = conn.execute(text("""
        WITH cur AS (SELECT symbol, max(quantity * price) v, max(available_at) avail, max(settlement_date) sd, max(cusip) cusip
                     FROM fails_to_deliver WHERE source_file = :f AND price IS NOT NULL AND symbol IS NOT NULL GROUP BY symbol),
        hist AS (SELECT symbol, source_file, max(quantity * price) v FROM fails_to_deliver
                 WHERE source_file < :f AND source_file >= :lo AND price IS NOT NULL GROUP BY 1, 2)
        SELECT c.symbol, c.v, c.avail, c.sd, c.cusip, array_agg(h.v ORDER BY h.source_file DESC) hv
        FROM cur c LEFT JOIN hist h USING (symbol) WHERE c.v >= :minv GROUP BY 1, 2, 3, 4, 5"""),
                        {"f": source_file, "lo": _file_minus(source_file, 6), "minv": FTD_MIN_VALUE}).mappings().all()
    out = []
    for r in rows:
        hv = [x for x in (r["hv"] or []) if x is not None][:6]
        if len(hv) < 4:
            continue  # not enough history to call anything a spike
        base = statistics.median(hv)
        if base <= 0 or r["v"] < FTD_MULT * base:
            continue
        cik = symbol_to_cik(conn, r["symbol"], r["sd"])
        if not cik:
            continue
        pub = r["avail"].date()
        eid = create_event(conn, event_type="FTD_SPIKE", dedupe_key=f"FTD:{source_file}:{r['symbol']}", public_date=pub,
                           ticker=r["symbol"], issuer_cik=cik, title=f"${r['symbol']}: fails-to-deliver ${r['v']:,.0f} ({r['v'] / base:.1f}x median)",
                           payload={"max_daily_value": r["v"], "median_prev": base, "multiple": r["v"] / base, "cusip": r["cusip"],
                                    "source_file": source_file, "last_settlement": str(r["sd"])},
                           source_url="https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data", is_backfill=is_backfill,
                           event_family="US_FTD_SPIKE", identity_confidence="MEDIUM", available_at_override=r["avail"],
                           date_quality="exact")
        if eid:
            out.append(eid)
    return out


def _file_minus(name: str, halves: int) -> str:
    y, m, h = int(name[8:12]), int(name[12:14]), name[14]
    idx = (y * 12 + m - 1) * 2 + (h == "b") - halves
    y2, rem = divmod(idx, 24)
    m2, h2 = divmod(rem, 2)
    return f"cnsfails{y2}{m2 + 1:02d}{'ab'[h2]}"


def short_pressure(conn: Connection, trade_date: date, *, is_backfill: bool) -> list[int]:
    rows = conn.execute(text("""
        WITH w AS (SELECT symbol, trade_date, short_volume::float / NULLIF(total_volume, 0) r, total_volume, available_at,
                          row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) rn
                   FROM short_volume_daily WHERE trade_date <= :d AND trade_date > :d - 110 AND total_volume > 0)
        SELECT symbol, avg(r) FILTER (WHERE rn <= 5) r5, avg(r) FILTER (WHERE rn > 5 AND rn <= 65) m60,
               stddev_samp(r) FILTER (WHERE rn > 5 AND rn <= 65) s60, avg(total_volume) FILTER (WHERE rn <= 5) v5,
               count(*) FILTER (WHERE rn > 5 AND rn <= 65) n60, max(available_at) FILTER (WHERE rn = 1) avail,
               bool_or(rn = 1 AND trade_date = :d) today
        FROM w GROUP BY symbol"""), {"d": trade_date}).mappings().all()
    out = []
    for r in rows:
        if not r["today"] or (r["n60"] or 0) < 40 or not r["s60"] or r["v5"] < SV_MIN_VOL or r["r5"] < SV_RATIO:
            continue
        z = (r["r5"] - r["m60"]) / r["s60"]
        if z < SV_Z:
            continue
        recent = conn.execute(text("""SELECT 1 FROM events WHERE event_type = 'SHORT_PRESSURE' AND ticker = :t
                                      AND public_date > :d - 30"""), {"t": r["symbol"], "d": trade_date}).first()
        if recent:
            continue
        cik = symbol_to_cik(conn, r["symbol"], trade_date)
        if not cik:
            continue
        eid = create_event(conn, event_type="SHORT_PRESSURE", dedupe_key=f"SHV:{r['symbol']}:{trade_date}", public_date=trade_date,
                           ticker=r["symbol"], issuer_cik=cik, title=f"${r['symbol']}: short-volume ratio {r['r5']:.0%} (z {z:.1f})",
                           payload={"ratio_5d": r["r5"], "mean_60d": r["m60"], "z": z, "avg_volume_5d": r["v5"],
                                    "note": "FINRA short-sale VOLUME, not short interest"},
                           source_url="https://www.finra.org/finra-data/browse-catalog/short-sale-volume-data", is_backfill=is_backfill,
                           event_family="US_SHORT_PRESSURE", identity_confidence="MEDIUM", available_at_override=r["avail"],
                           date_quality="exact")
        if eid:
            out.append(eid)
    return out


def thirteen_f(conn: Connection, accession: str, *, is_backfill: bool) -> list[int]:
    from app.sources.us.form13f import position_changes
    f = conn.execute(text("""SELECT manager_cik, manager_name, period, filed_date, available_at, entries, submission_type
                             FROM inst_filings_13f WHERE accession = :a"""), {"a": accession}).mappings().first()
    if not f or f["submission_type"] != "13F-HR" or not f["entries"] or not (10 <= f["entries"] <= 150):
        return []
    out = []
    for ch in position_changes(conn, f["manager_cik"], f["period"]):
        if ch["change"] not in ("NEW", "INCREASE"):
            continue
        cik, conf, _why = cusip_to_cik(conn, ch["cusip"])
        if not cik:
            continue
        ticker = _ticker(conn, cik)
        if ch["change"] == "NEW" and ch["weight"] >= NEWPOS_WEIGHT:
            eid = create_event(conn, event_type="INST_13F_NEW_POSITION", dedupe_key=f"13FN:{accession}:{ch['cusip']}", public_date=f["filed_date"], ticker=ticker,
                               issuer_cik=cik, company_name=ch["issuer_name"],
                               title=f"{f['manager_name']} opened {ch['weight']:.1%} position in {ch['issuer_name']} (Q {f['period']})",
                               payload={"manager_cik": f["manager_cik"], "manager": f["manager_name"], "period": str(f["period"]),
                                        "weight": ch["weight"], "shares": ch["shares"], "cusip": ch["cusip"], "entries": f["entries"]},
                               source_url=f"https://www.sec.gov/Archives/edgar/data/{int(f['manager_cik'])}/{accession.replace('-', '')}/",
                               is_backfill=is_backfill, event_family="US_13F_NEW_POSITION", identity_confidence=conf,
                               source_accession=accession, available_at_override=f["available_at"], date_quality="date")
            if eid:
                out.append(eid)
        # focused managers that hold the CUSIP this quarter but did not hold it the quarter before (new holders),
        # counting only filings already public at this filing's available_at
        n = conn.execute(text("""
            SELECT count(DISTINCT f.manager_cik) FROM inst_filings_13f f JOIN inst_holdings_13f h ON h.accession = f.accession
            WHERE h.cusip = :c AND f.period = :p AND f.submission_type = '13F-HR' AND f.entries BETWEEN 10 AND 150
              AND f.available_at <= :t
              AND EXISTS (SELECT 1 FROM inst_filings_13f f0 WHERE f0.manager_cik = f.manager_cik
                          AND f0.period = CAST(date_trunc('quarter', CAST(:p AS date)) AS date) - 1 AND f0.submission_type = '13F-HR')
              AND NOT EXISTS (SELECT 1 FROM inst_filings_13f f0 JOIN inst_holdings_13f h0 ON h0.accession = f0.accession
                              WHERE f0.manager_cik = f.manager_cik AND h0.cusip = :c
                                AND f0.period = CAST(date_trunc('quarter', CAST(:p AS date)) AS date) - 1)"""),
                         {"c": ch["cusip"], "p": f["period"], "t": f["available_at"]}).scalar()
        if n >= ACCUM_MANAGERS:
            eid = create_event(conn, event_type="INST_13F_ACCUMULATION", dedupe_key=f"13FA:{ch['cusip']}:{f['period']}",
                               public_date=f["filed_date"], ticker=ticker, issuer_cik=cik, company_name=ch["issuer_name"],
                               title=f"{ch['issuer_name']}: {n} focused funds opened new positions (Q {f['period']})",
                               payload={"period": str(f["period"]), "new_focused_holders": n, "cusip": ch["cusip"],
                                        "trigger_manager": f["manager_name"]},
                               source_url="https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets",
                               is_backfill=is_backfill, event_family="US_13F_ACCUMULATION", identity_confidence=conf,
                               available_at_override=f["available_at"], date_quality="date")
            if eid:
                out.append(eid)
    return out
