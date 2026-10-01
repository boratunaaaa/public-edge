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
    """Display / paper ticker only (never the identity: prices resolve by CIK in the security master). The in-memory
    CIK->ticker map is empty in a fresh process (v2 historical runner, worker) until loaded - before 0013 every 13F
    event was written with ticker NULL for that reason. load() is a no-op while the map is fresh (1 h)."""
    from app.entities.registry import resolver
    if cik:
        resolver.load(conn)
    return resolver.ticker_for_cik(cik)


def _review(conf: str, why: str, payload: dict) -> bool:
    """LOW identity (prefix-only CUSIP / name contradiction): kept for audit, never validated or traded."""
    if conf == "LOW":
        payload["identity_review"] = why
        return True
    return False


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
    changes = position_changes(conn, f["manager_cik"], f["period"])
    for ch in changes:
        if ch["change"] in ("EXIT", "DECREASE"):
            # context only (not a candidate family): a focused manager leaving / cutting a position it held
            cik, conf, why = cusip_to_cik(conn, ch["cusip"], ch["issuer_name"])
            if cik and ch["prev_shares"]:
                et = "INST_13F_EXIT" if ch["change"] == "EXIT" else "INST_13F_DECREASE"
                payload = {"manager_cik": f["manager_cik"], "manager": f["manager_name"], "period": str(f["period"]),
                           "shares": ch["shares"], "prev_shares": ch["prev_shares"], "cusip": ch["cusip"]}
                review = _review(conf, why, payload)
                eid = create_event(conn, event_type=et, dedupe_key=f"13F{et[-4:]}:{accession}:{ch['cusip']}", public_date=f["filed_date"],
                                   ticker=_ticker(conn, cik), issuer_cik=cik, company_name=ch["issuer_name"],
                                   title=f"{f['manager_name']} {'exited' if et.endswith('EXIT') else 'cut'} {ch['issuer_name']} (Q {f['period']})",
                                   payload=payload,
                                   source_url="https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets", is_backfill=is_backfill,
                                   event_family="US_13F_CONTEXT", identity_confidence=conf, source_accession=accession,
                                   available_at_override=f["available_at"], date_quality="date", needs_review=review)
                if eid:
                    out.append(eid)
            continue
        if ch["change"] not in ("NEW", "INCREASE"):
            continue
        cik, conf, why = cusip_to_cik(conn, ch["cusip"], ch["issuer_name"])
        if not cik:
            continue
        ticker = _ticker(conn, cik)
        if ch["change"] == "NEW" and ch["weight"] >= NEWPOS_WEIGHT:
            payload = {"manager_cik": f["manager_cik"], "manager": f["manager_name"], "period": str(f["period"]),
                       "weight": ch["weight"], "shares": ch["shares"], "cusip": ch["cusip"], "entries": f["entries"]}
            review = _review(conf, why, payload)
            eid = create_event(conn, event_type="INST_13F_NEW_POSITION", dedupe_key=f"13FN:{accession}:{ch['cusip']}", public_date=f["filed_date"], ticker=ticker,
                               issuer_cik=cik, company_name=ch["issuer_name"],
                               title=f"{f['manager_name']} opened {ch['weight']:.1%} position in {ch['issuer_name']} (Q {f['period']})",
                               payload=payload,
                               source_url=f"https://www.sec.gov/Archives/edgar/data/{int(f['manager_cik'])}/{accession.replace('-', '')}/",
                               is_backfill=is_backfill, event_family="US_13F_NEW_POSITION", identity_confidence=conf,
                               source_accession=accession, available_at_override=f["available_at"], date_quality="date",
                               needs_review=review)
            if eid:
                out.append(eid)
    return out


def accumulation_events(conn: Connection, period, *, is_backfill: bool, loaded: set[str] | None = None) -> list[int]:
    """US_13F_ACCUMULATION, set-based for one report period: per CUSIP, focused managers (10-150 positions, with a
    13F-HR for the previous quarter) that hold it now but did not hold it the quarter before, ordered by the time their
    filing became public; the event is the 3rd such manager's filing (point in time: only filings public by then)."""
    rows = conn.execute(text("""
        WITH foc AS (SELECT accession, manager_cik, manager_name, filed_date, available_at FROM inst_filings_13f
                     WHERE period = :p AND submission_type = '13F-HR' AND entries BETWEEN 10 AND 150
                       AND (CAST(:all_ok AS boolean) OR source = ANY(:loaded))),
        prevf AS (SELECT manager_cik, accession FROM inst_filings_13f
                  WHERE period = CAST(date_trunc('quarter', CAST(:p AS date)) AS date) - 1 AND submission_type = '13F-HR'
                    AND (CAST(:all_ok AS boolean) OR source = ANY(:loaded))),
        newh AS (SELECT DISTINCT ON (f.manager_cik, h.cusip) h.cusip, f.manager_cik, f.manager_name, f.accession, f.filed_date,
                        f.available_at, max(h.issuer_name) OVER (PARTITION BY h.cusip) issuer_name
                 FROM foc f JOIN inst_holdings_13f h ON h.accession = f.accession
                 WHERE EXISTS (SELECT 1 FROM prevf pf WHERE pf.manager_cik = f.manager_cik)
                   AND NOT EXISTS (SELECT 1 FROM prevf pf JOIN inst_holdings_13f h0 ON h0.accession = pf.accession
                                   WHERE pf.manager_cik = f.manager_cik AND h0.cusip = h.cusip)
                 ORDER BY f.manager_cik, h.cusip, f.available_at),
        ranked AS (SELECT *, row_number() OVER (PARTITION BY cusip ORDER BY available_at, accession) rn FROM newh)
        SELECT cusip, manager_name, accession, filed_date, available_at, issuer_name FROM ranked WHERE rn = 3"""),
                        {"p": period, "all_ok": loaded is None, "loaded": sorted(loaded or [])}).mappings().all()
    out = []
    for r in rows:
        cik, conf, why = cusip_to_cik(conn, r["cusip"], r["issuer_name"])
        if not cik:
            continue
        payload = {"period": str(period), "new_focused_holders": 3, "cusip": r["cusip"], "trigger_manager": r["manager_name"]}
        review = _review(conf, why, payload)
        eid = create_event(conn, event_type="INST_13F_ACCUMULATION", dedupe_key=f"13FA:{r['cusip']}:{period}",
                           public_date=r["filed_date"], ticker=_ticker(conn, cik), issuer_cik=cik, company_name=r["issuer_name"],
                           title=f"{r['issuer_name']}: 3 focused funds opened new positions (Q {period})",
                           payload=payload,
                           source_url="https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets", is_backfill=is_backfill,
                           event_family="US_13F_ACCUMULATION", identity_confidence=conf, source_accession=r["accession"],
                           available_at_override=r["available_at"], date_quality="date", needs_review=review)
        if eid:
            out.append(eid)
    return out


def thirteen_f_period(conn: Connection, period, originals: list[str], *, is_backfill: bool, dry_run: bool = False) -> list[dict]:
    """Set-based equivalent of thirteen_f() for many original filings of ONE report period: two queries for the whole
    period instead of one position_changes() per filing. Classification is the same Python code path (NEW / EXIT /
    DECREASE / INCREASE / HOLD on the same effective reports), so the events are identical - verified by
    test_thirteen_f_period_equals_per_filing and by a dry-run comparison on production before use.
    Returns event specs (dedupe_key, event_type, fields); dry_run=True writes nothing. When writing, spec["event_id"] is the
    NEW event id or None when the event already existed (callers count only real inserts)."""
    if not originals:
        return []
    fil = {r["accession"]: dict(r) for r in conn.execute(text("""
        SELECT accession, manager_cik, manager_name, period, filed_date, available_at, entries, submission_type
        FROM inst_filings_13f WHERE accession = ANY(:a)"""), {"a": originals}).mappings()}
    managers = sorted({f["manager_cik"] for f in fil.values() if f["period"] == period and f["submission_type"] == "13F-HR"
                       and f["entries"] and 10 <= f["entries"] <= 150})
    rows = conn.execute(text("""
        WITH f AS (SELECT manager_cik, accession, period, filed_date, submission_type, amendment_type FROM inst_filings_13f
                   WHERE manager_cik = ANY(:m) AND period <= :p AND period >= CAST(:p AS date) - 120),
        eff AS (SELECT DISTINCT ON (manager_cik, period) manager_cik, period, accession, filed_date FROM f
                WHERE submission_type = '13F-HR' OR amendment_type = 'RESTATEMENT'
                ORDER BY manager_cik, period, filed_date DESC, accession DESC),
        two AS (SELECT * FROM (SELECT eff.*, row_number() OVER (PARTITION BY manager_cik ORDER BY period DESC) rn FROM eff) x
                WHERE rn <= 2)
        SELECT e.manager_cik, e.period, e.filed_date, h.cusip, max(h.issuer_name) issuer_name, sum(h.shares) shares, sum(h.value) value
        FROM two e JOIN inst_holdings_13f h ON h.accession = e.accession
        GROUP BY 1, 2, 3, 4"""), {"m": managers, "p": period}).mappings().all()
    by_m: dict[str, list] = {}
    for r in rows:
        by_m.setdefault(r["manager_cik"], []).append(r)
    cik_cache: dict[str, tuple] = {}
    tick_cache: dict[str, str | None] = {}
    specs = []
    for acc in originals:
        f = fil.get(acc)
        if not f or f["submission_type"] != "13F-HR" or not f["entries"] or not (10 <= f["entries"] <= 150) or f["period"] != period:
            continue
        mrows = by_m.get(f["manager_cik"], [])
        periods = sorted({r["period"] for r in mrows})
        if not periods or periods[-1] != period:
            continue
        cur = {r["cusip"]: r for r in mrows if r["period"] == period}
        prev = {r["cusip"]: r for r in mrows if len(periods) > 1 and r["period"] == periods[0]}
        if not prev:
            continue  # FIRST_REPORT: no event
        tot = sum(r["value"] or 0 for r in cur.values()) or None
        for cu in set(cur) | set(prev):
            a, b = cur.get(cu), prev.get(cu)
            if a and not b:
                kind = "NEW"
            elif b and not a:
                kind = "EXIT"
            elif a["shares"] > b["shares"] * 1.1:
                kind = "INCREASE"
            elif a["shares"] < b["shares"] * 0.9:
                kind = "DECREASE"
            else:
                kind = "HOLD"
            shares, prev_shares = (a["shares"] if a else 0), (b["shares"] if b else 0)
            weight = (a["value"] / tot) if (a and tot) else 0.0
            name = (a or b)["issuer_name"]
            if kind not in ("NEW", "EXIT", "DECREASE") or (kind == "NEW" and weight < NEWPOS_WEIGHT) or (kind != "NEW" and not prev_shares):
                continue
            if cu not in cik_cache:
                cik_cache[cu] = cusip_to_cik(conn, cu, name)
            cik, conf, why = cik_cache[cu]
            if not cik:
                continue
            if cik not in tick_cache:
                tick_cache[cik] = _ticker(conn, cik)
            if kind == "NEW":
                et, key = "INST_13F_NEW_POSITION", f"13FN:{acc}:{cu}"
                title = f"{f['manager_name']} opened {weight:.1%} position in {name} (Q {f['period']})"
                payload = {"manager_cik": f["manager_cik"], "manager": f["manager_name"], "period": str(f["period"]),
                           "weight": weight, "shares": shares, "cusip": cu, "entries": f["entries"]}
                url = f"https://www.sec.gov/Archives/edgar/data/{int(f['manager_cik'])}/{acc.replace('-', '')}/"
                fam = "US_13F_NEW_POSITION"
            else:
                et = "INST_13F_EXIT" if kind == "EXIT" else "INST_13F_DECREASE"
                key = f"13F{et[-4:]}:{acc}:{cu}"
                title = f"{f['manager_name']} {'exited' if kind == 'EXIT' else 'cut'} {name} (Q {f['period']})"
                payload = {"manager_cik": f["manager_cik"], "manager": f["manager_name"], "period": str(f["period"]),
                           "shares": shares, "prev_shares": prev_shares, "cusip": cu}
                url = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
                fam = "US_13F_CONTEXT"
            review = _review(conf, why, payload)
            spec = {"event_type": et, "dedupe_key": key, "public_date": f["filed_date"], "issuer_cik": cik, "ticker": tick_cache[cik],
                    "company_name": name, "title": title, "payload": payload, "source_url": url, "event_family": fam,
                    "identity_confidence": conf, "source_accession": acc, "available_at": f["available_at"], "needs_review": review}
            specs.append(spec)
            if not dry_run:
                spec["event_id"] = create_event(conn, event_type=et, dedupe_key=key, public_date=f["filed_date"], ticker=spec["ticker"], issuer_cik=cik,
                             company_name=name, title=title, payload=payload, source_url=url, is_backfill=is_backfill, event_family=fam,
                             identity_confidence=conf, source_accession=acc, available_at_override=f["available_at"], date_quality="date",
                             needs_review=review)
    return specs
