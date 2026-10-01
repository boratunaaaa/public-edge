"""One-off repair of the ClinicalTrials historical-load lane flag (app/jobs/trial_lane_repair.py). DRY-RUN unless --apply.

    docker compose run --rm --no-deps -T backfill python scripts/pe_trial_backfill_flag.py [--apply] [--undo-run RUN_ID]

Cohort = events created inside the logged window of the v2_health_gov historical step (provenance, not event age).
--undo-run reverts rows of an earlier run that lie outside that window. Undo of a run: see its audit rows."""
from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import text

from app.db.session import get_engine
from app.jobs.trial_lane_repair import run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo-run")
    a = ap.parse_args()
    with get_engine().begin() as c:
        if not a.apply:
            c.execute(text("SET TRANSACTION READ ONLY"))
        out = run(c, apply=a.apply, undo_run=a.undo_run)
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
