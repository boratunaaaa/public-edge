"""0013 return-queue / 13F identity repair. DRY-RUN unless --apply is given.
    docker compose run --rm --no-deps -T backfill python scripts/pe_return_repair.py [--apply] [--items a,b]
Items: partial_next_at, vendor_retry, identity_13f, ticker_13f, ca_grants_health (see app/jobs/return_repair.py).
--apply needs a fresh backup (scripts/pe-backup.sh) and migration 0013."""
from __future__ import annotations

import argparse
import json
import sys

from app.db.session import get_engine
from app.jobs.return_repair import ITEMS, run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes (default: read-only dry run)")
    ap.add_argument("--items", default=",".join(ITEMS))
    a = ap.parse_args()
    items = [x for x in a.items.split(",") if x]
    bad = [x for x in items if x not in ITEMS]
    if bad:
        print(f"unknown items: {bad}", file=sys.stderr)
        return 2
    print(json.dumps(run(get_engine(), items, apply=a.apply), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
