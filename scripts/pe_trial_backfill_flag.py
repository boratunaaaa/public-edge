"""One-off repair: ClinicalTrials events that were already older than LIVE_MAX_AGE when first seen are history, not
live signals. The 2000+ historical load (v2_steps.backfill_health_gov -> run_live(since=2000-01-01)) stored them with
is_backfill = false, so their returns sat in the slow live lane. DRY-RUN unless --apply.

    docker compose run --rm --no-deps -T backfill python scripts/pe_trial_backfill_flag.py [--apply]

Only is_backfill changes (false -> true), one audited row per event in data_cleanup_audit (before / after), in one
transaction. Undo: UPDATE events SET is_backfill = false WHERE id IN (SELECT row_id FROM data_cleanup_audit
WHERE run_id = '<run_id>')."""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date

from sqlalchemy import text

from app.db.session import get_engine
from app.sources.us.clinical import LIVE_MAX_AGE

WHERE = """event_family = 'US_TRIAL_CATALYST' AND NOT is_backfill
           AND available_at < coalesce(first_seen_at, created_at) - make_interval(secs => :age)"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    run_id = f"{'apply' if a.apply else 'dry'}-{date.today()}-trialbf-{uuid.uuid4().hex[:6]}"
    p = {"age": LIVE_MAX_AGE.total_seconds()}
    with get_engine().begin() as c:
        if not a.apply:
            c.execute(text("SET TRANSACTION READ ONLY"))
        by = dict(c.execute(text(f"SELECT returns_status, count(*) FROM events WHERE {WHERE} GROUP BY 1"), p).all())
        live_kept = c.execute(text("""SELECT count(*) FROM events WHERE event_family = 'US_TRIAL_CATALYST' AND NOT is_backfill
                                      AND NOT (available_at < coalesce(first_seen_at, created_at) - make_interval(secs => :age))"""),
                              p).scalar()
        out = {"run_id": run_id, "mode": "APPLY" if a.apply else "DRY_RUN", "rule_days": LIVE_MAX_AGE.days,
               "to_backfill": sum(by.values()), "by_returns_status": by, "live_kept": live_kept}
        if a.apply:
            c.execute(text(f"""INSERT INTO data_cleanup_audit (run_id, action, table_name, row_id, before, after, reason, dry_run)
                               SELECT :r, 'RECLASSIFY', 'events', id, jsonb_build_object('is_backfill', false),
                                      jsonb_build_object('is_backfill', true),
                                      'TRIAL_HISTORICAL_LOAD_MARKED_LIVE: available_at older than first seen - {LIVE_MAX_AGE.days}d', false
                               FROM events WHERE {WHERE}"""), {**p, "r": run_id})
            out["updated"] = c.execute(text(f"UPDATE events SET is_backfill = true, updated_at = now() WHERE {WHERE}"), p).rowcount
            out["audited"] = c.execute(text("SELECT count(*) FROM data_cleanup_audit WHERE run_id = :r"), {"r": run_id}).scalar()
            if out["updated"] != out["audited"] or out["updated"] != out["to_backfill"]:
                raise SystemExit(f"count mismatch, rolled back: {out}")
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
