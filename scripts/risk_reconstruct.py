"""Offline point-in-time risk reconstruction into the REPLAY database (schema risk_hist_v3; production refused).

    REPLAY_DATABASE_URL=... python scripts/risk_reconstruct.py --source risk_src.json --run-id pilot1
Idempotent and restartable: re-running the same --run-id skips checkpointed issuers and never duplicates a label; a
run id is pinned to its source digest + detector configuration (a changed input needs a new run id). Coverage is
published only for an export whose completion trailer verifies (exit 3 otherwise)."""
from __future__ import annotations

import argparse
import json
import os
import sys

from app.replay.risk_history import RiskHistoryStore, RunInputChanged, SourceIncomplete, reconstruct


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()
    url = os.environ.get("REPLAY_DATABASE_URL")
    if not url:
        print("REPLAY_DATABASE_URL is required (never the production database)", file=sys.stderr)
        return 2
    try:
        res = reconstruct(RiskHistoryStore(url), a.run_id, a.source)
    except (RunInputChanged, SourceIncomplete) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 3
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
