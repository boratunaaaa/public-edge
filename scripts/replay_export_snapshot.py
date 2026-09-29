"""READ-ONLY export of a small replay input snapshot from a PUBLIC EDGE database (one READ ONLY transaction).

    python scripts/replay_export_snapshot.py --ciks 0000723646,0001505732 --start 2021-03-01 --end 2021-06-30 \
        --event-types INSIDER_OPEN_MARKET_BUY > snapshot.json

Events carry only publication-time fields (app.replay.source.EVENT_FIELDS); superseded legacy duplicates are left out
(a data-cleaning artefact, not a historical signal). Bars cover start-120d .. end+200d (the tail is for post-run
evaluation only; the replay's provider never shows it before its session)."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

from sqlalchemy import text

from app.db.session import get_engine
from app.replay.source import EVENT_FIELDS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ciks", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--event-types", default="INSIDER_OPEN_MARKET_BUY")
    ap.add_argument("--label", default="snapshot")
    a = ap.parse_args()
    ciks = [c.strip().zfill(10) for c in a.ciks.split(",") if c.strip()]
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    types = [t for t in a.event_types.split(",") if t]
    out: dict = {"label": a.label, "params": {"ciks": ciks, "start": a.start, "end": a.end, "event_types": types}}
    with get_engine().connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        cols = ", ".join(EVENT_FIELDS)
        out["events"] = [dict(r) for r in c.execute(text(f"""
            SELECT {cols} FROM events WHERE issuer_cik = ANY(:c) AND event_type = ANY(:t) AND NOT superseded
              AND available_at >= :s AND available_at <= :e ORDER BY available_at, id"""),
            {"c": ciks, "t": types, "s": start - timedelta(days=5), "e": end + timedelta(days=1)}).mappings()]
        syms = c.execute(text("""SELECT cik, symbol, first_seen, last_seen, source FROM security_symbols
                                 WHERE source <> 'sec_registry' AND (cik = ANY(:c) OR symbol IN
                                   (SELECT symbol FROM security_symbols WHERE cik = ANY(:c)))"""), {"c": ciks}).mappings().all()
        out["symbols"] = [dict(r) for r in syms]
        symbols = sorted({r["symbol"] for r in syms if r["cik"] in ciks} | {"SPY"})
        bars: dict[str, list] = {}
        for s, d, o, h, lo, cl, ac, v in c.execute(text("""SELECT symbol, date, open, high, low, close, adj_close, volume
                                                           FROM market_prices WHERE symbol = ANY(:s) AND date BETWEEN :a AND :b
                                                           ORDER BY symbol, date"""),
                                                    {"s": symbols, "a": start - timedelta(days=120), "b": end + timedelta(days=200)}):
            bars.setdefault(s, []).append([d, o, h, lo, cl, ac, v])
        out["bars"] = bars
        out["share_facts"] = {}
        for cik, acc, e, f, form, val in c.execute(text("""SELECT cik, accession, end_date, filed, form, val
                                                          FROM shares_outstanding_facts WHERE cik = ANY(:c)
                                                          ORDER BY cik, filed, end_date"""), {"c": ciks}):
            out["share_facts"].setdefault(cik, []).append([acc, e, f, form, val])
        out["security_master"] = [dict(r) for r in c.execute(text("""SELECT cik, name, sector, delisting_date, delisting_reason
                                                                      FROM security_master WHERE cik = ANY(:c)"""), {"c": ciks}).mappings()]
        out["corporate_actions"] = [dict(r) for r in c.execute(text("""SELECT cik, action, action_date, accession, source
                                                                        FROM corporate_actions WHERE cik = ANY(:c)"""),
                                                               {"c": ciks}).mappings()]
        all_ciks = sorted({r["cik"] for r in syms} | set(ciks))
        out["companies"] = dict(c.execute(text("SELECT cik, name FROM companies WHERE cik = ANY(:c)"), {"c": all_ciks}).all())
        c.rollback()
    json.dump(out, sys.stdout, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
