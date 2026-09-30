# Congress forward-looking identity - production deploy plan (NOT deployed)

Status 2026-09-30: the data repair is APPLIED in production (run_id congress-identity-2026-09-30-d36227; backup
pe_pre-migration_20260930T1624Z_congress_repair.dump). The forward code (new PTRs written with the trade-date
identity, clusters by issuer) is on branch congress-identity-repair @ d118126 and NOT deployed: new House PTRs are still
written by the old path (CIK from today's ticker map, clusters without CIK).

## What changes in production
| file | effect | services that run it |
|---|---|---|
| app/sources/congress/identity.py (new) | trade-date symbol -> issuer; merged-record / name checks; REVIEW otherwise | worker (house source) |
| app/sources/congress/house.py | process_ptr writes identity + needs_review; clusters count only same-issuer trades, carry CIK + doc_id/line_no | worker |
| app/jobs/congress_repair.py (new) | the repair job (already applied); re-runnable dry-run / apply / undo | one-off |
No migration, no schema change. No paper / scoring / gate code changes (HOUSE_BUY is context only).

## Steps
1. Branch: merge production 72ba073 (exit-order fix) into congress-identity-repair (git merge-tree: no conflict -
   disjoint files), full suite, Codex review of the merge.
2. Verify production == 72ba073 (hash check), back up congress_transactions + events(CONGRESS_*) (table dump),
   tag the current image rollback-72ba073.
3. Build the candidate on the server, run the full suite inside it (pyarrow present), tag it public-edge:1.0.0.
4. Copy the changed files (git diff 72ba073..<merge>), recreate ONLY worker (the house source runs there); api /
   intelligence / backfill do not execute this code.
5. Smoke: pe_smoke 18/18; dry-run of app.jobs.congress_repair -> expect ~0 FILLED / CHANGED (only PTRs ingested
   since the apply); force one live run of the house source is NOT needed - wait for its 3-hourly run and check that
   new CONGRESS_BUY rows carry identity_confidence and, for unsafe lines, needs_review + identity_review.
6. If the dry-run shows new FILLED / CHANGED / REVIEW rows from the gap between the apply and this deploy: apply the
   repair job once more (same audited path).
Rollback: image rollback-72ba073 + recreate worker; the data repair has its own undo(run_id).
