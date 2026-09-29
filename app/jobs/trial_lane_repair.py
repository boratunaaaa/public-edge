"""One-off repair: the ClinicalTrials historical load (v2_steps.backfill_health_gov -> run_live(since=2000-01-01)) stored
its events with is_backfill = false, so their returns sat in the slow live lane.

The cohort is identified by PROVENANCE, never by event age: ClinicalTrials events created inside the logged window of
that historical step (/opt/pe-crosscheck/v2_hist.log: "v2_health_gov start" 2026-09-28T12:05:48Z .. "done"
12:18:40Z; live runs of the source are 6 h apart and none fell inside it). Events of live runs - including old
registrations first seen by a live run, delayed polls, forward status / phase changes - are left live.

Every change is audited in data_cleanup_audit (before / after) in the same transaction. undo_run reverts the rows of an
earlier run that fall OUTSIDE the verified window (the first, age-based apply of 2026-09-29 also moved 129 events of a
05:26 live run)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

HIST_FROM = datetime(2026, 9, 28, 12, 5, 48, tzinfo=timezone.utc)
HIST_TO = datetime(2026, 9, 28, 12, 18, 41, tzinfo=timezone.utc)

COHORT = """event_family = 'US_TRIAL_CATALYST' AND NOT is_backfill AND created_at >= :f AND created_at <= :t"""
OUTSIDE = """id IN (SELECT row_id FROM data_cleanup_audit WHERE run_id = :u AND action = 'RECLASSIFY')
             AND is_backfill AND NOT (created_at >= :f AND created_at <= :t)"""


def run(conn: Connection, apply: bool = False, undo_run: str | None = None,
        window: tuple[datetime, datetime] = (HIST_FROM, HIST_TO)) -> dict:
    run_id = f"{'apply' if apply else 'dry'}-{date.today()}-triallane-{uuid.uuid4().hex[:6]}"
    p = {"f": window[0], "t": window[1], "u": undo_run}
    by = dict(conn.execute(text(f"SELECT returns_status, count(*) FROM events WHERE {COHORT} GROUP BY 1"), p).all())
    undo = conn.execute(text(f"SELECT count(*) FROM events WHERE {OUTSIDE}"), p).scalar() if undo_run else 0
    out = {"run_id": run_id, "mode": "APPLY" if apply else "DRY_RUN", "window": [str(w) for w in window],
           "to_backfill": sum(by.values()), "by_returns_status": by, "undo_to_live": undo}
    if not apply:
        return out
    moved = reverted = 0
    if out["to_backfill"]:
        conn.execute(text(f"""INSERT INTO data_cleanup_audit (run_id, action, table_name, row_id, before, after, reason, dry_run)
            SELECT :r, 'RECLASSIFY', 'events', id, '{{"is_backfill": false}}'::jsonb, '{{"is_backfill": true}}'::jsonb,
                   'TRIAL_HISTORICAL_LOAD_MARKED_LIVE: created by v2_health_gov historical step window', false
            FROM events WHERE {COHORT}"""), {**p, "r": run_id})
        moved = conn.execute(text(f"UPDATE events SET is_backfill = true, updated_at = now() WHERE {COHORT}"), p).rowcount
    if undo:
        conn.execute(text(f"""INSERT INTO data_cleanup_audit (run_id, action, table_name, row_id, before, after, reason, dry_run)
            SELECT :r, 'RECLASSIFY_UNDO', 'events', id, '{{"is_backfill": true}}'::jsonb, '{{"is_backfill": false}}'::jsonb,
                   'undo ' || :u || ': outside the verified historical window (live run)', false
            FROM events WHERE {OUTSIDE}"""), {**p, "r": run_id})
        reverted = conn.execute(text(f"UPDATE events SET is_backfill = false, updated_at = now() WHERE {OUTSIDE}"), p).rowcount
    audited = conn.execute(text("SELECT count(*) FROM data_cleanup_audit WHERE run_id = :r"), {"r": run_id}).scalar()
    out.update(moved=moved, reverted=reverted, audited=audited)
    if moved != out["to_backfill"] or reverted != undo or audited != moved + reverted:
        raise RuntimeError(f"count mismatch, rolled back: {out}")
    return out
