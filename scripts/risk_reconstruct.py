"""Offline point-in-time risk reconstruction into the REPLAY database (schema risk_hist; production refused).

    REPLAY_DATABASE_URL=... python scripts/risk_reconstruct.py --source risk_src.json --run-id pilot1
Idempotent and restartable: re-running the same --run-id skips checkpointed issuers and never duplicates a label."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

from app.replay.risk_history import RiskHistoryStore, load_risk_source, reconstruct


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()
    url = os.environ.get("REPLAY_DATABASE_URL")
    if not url:
        print("REPLAY_DATABASE_URL is required (never the production database)", file=sys.stderr)
        return 2
    src = load_risk_source(a.source)
    res = reconstruct(RiskHistoryStore(url), a.run_id, src, src["ciks"], date.fromisoformat(src["start"]),
                      date.fromisoformat(src["end"]))
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
