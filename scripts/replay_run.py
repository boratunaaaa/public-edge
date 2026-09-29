"""Run one historical replay from a snapshot file into an isolated replay database (never production).

    REPLAY_DATABASE_URL=postgresql+psycopg://... python scripts/replay_run.py --snapshot snap.json --id smoke1 \
        --start 2021-03-01 --end 2021-06-30 [--event-types INSIDER_OPEN_MARKET_BUY] [--cash 10000] [--role development]"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from app.replay.config import ReplayConfig
from app.replay.engine import ReplayRun
from app.replay.source import load_snapshot


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--id", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--event-types", default="INSIDER_OPEN_MARKET_BUY")
    ap.add_argument("--cash", type=float, default=10_000.0)
    ap.add_argument("--role", default="development")
    ap.add_argument("--risk-run", help="load point-in-time risk labels + coverage of this reconstruction run")
    a = ap.parse_args()
    url = os.environ.get("REPLAY_DATABASE_URL")
    if not url:
        print("REPLAY_DATABASE_URL is required (a database that is not production)", file=sys.stderr)
        return 2
    cfg = ReplayConfig(replay_id=a.id, start=datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc),
                       end=datetime.fromisoformat(a.end).replace(hour=23, tzinfo=timezone.utc),
                       event_types=tuple(t for t in a.event_types.split(",") if t), initial_cash=a.cash, period_role=a.role)
    t0 = time.time()
    src = load_snapshot(a.snapshot)
    if a.risk_run:
        from app.replay.risk_history import RiskHistoryStore
        # the issuer scope the reconstruction ran with = the snapshot's export parameters (coverage is per scope)
        ciks = sorted(json.load(open(a.snapshot)).get("params", {}).get("ciks") or
                      {e["issuer_cik"] for e in src.events if e.get("issuer_cik")})
        src.risk_labels, src.risk_cov = RiskHistoryStore(url).load(ciks, cfg.end, a.risk_run)
    run = ReplayRun(cfg, src, url)
    res = run.run()
    out = run.summary() | {"wall_seconds": round(time.time() - t0, 1), "price_requests": run.provider.requests,
                           "reviews": res.reviews[:20], "leakage": res.leakage}
    print(json.dumps(out, indent=1, default=str))
    return 0 if res.status == "COMPLETED" else 1


if __name__ == "__main__":
    sys.exit(main())
