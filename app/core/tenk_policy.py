"""Validated 10-K risk: ONE policy shared by the paper gate (hard veto) and the research score vetoes.

Evidence (app/scoring/evidence_2026-09-30.json, research_v2.tenk_negative, measured from the 10-K date):
  TENK_NEGATIVE_RECENT    a 10-K change with direction NEGATIVE: -6.71 pp vs a matched control of the same stock
                          (2,863 companies, sign p 5e-9)
  TENK_NEW_GOING_CONCERN  a 10-K change listing NEW_GOING_CONCERN, whatever its overall direction (measured by change
                          type): 28 % beat SPY, 27 % delisted within 180 days, 12 % bankrupt within 365 days

Point in time: only changes of the SAME issuer that were public STRICTLY BEFORE the decision moment count
(coalesce(available_at, public_at) < at), and only within the TENK_WINDOW_DAYS before it (the window the effect was
measured over). A change becoming public at or after the decision can never veto it.

Locking (no race between a decision and a 10-K change): every writer of TENK_CHANGE state - a new event
(app.db.store.create_event) or a supersession (app.tenk.engine) - takes the issuer's EXCLUSIVE transaction advisory
lock; a paper decision that must honour the veto (approval,
BUY fill) reads with lock=True, i.e. under the issuer's SHARED lock held until its transaction commits. A 10-K
committed before the decision's read is therefore always seen; one being written waits until the decision has
committed (it then happened before the 10-K was known, and the next stage re-checks). A transaction holding several
issuer locks takes them all up front in CIK order (lock_issuers): no deadlock cycle.

Supersession: a live decision uses today's state (superseded changes are ignored). A retry at an earlier decision
moment cannot know that moment's supersession state (no timestamp), so it counts superseded changes too - the
conservative side of a veto."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

TENK_WINDOW_DAYS = 90
TENK_NEGATIVE_RECENT = "TENK_NEGATIVE_RECENT"
TENK_NEW_GOING_CONCERN = "TENK_NEW_GOING_CONCERN"


@dataclass(frozen=True)
class TenkRisk:
    vetoes: tuple[str, ...] = ()
    event_ids: tuple[int, ...] = ()  # the 10-K change events behind the vetoes (most recent first)

    @property
    def note(self) -> str:
        return "validated 10-K risk: " + ", ".join(self.vetoes)


def lock_issuer(conn: Connection, issuer_cik: str, *, exclusive: bool) -> None:
    """Transaction-scoped advisory lock on the issuer's 10-K risk (see module doc)."""
    fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    conn.execute(text(f"SELECT {fn}(hashtextextended(:k, 0))"), {"k": f"pe:tenk:{issuer_cik}"})


def lock_issuers(conn: Connection, issuer_ciks, *, exclusive: bool) -> None:
    """Several issuers at once, always in CIK order: every transaction that holds more than one issuer lock (the
    multi-issuer reclassifier, a fill run over many orders) acquires them in the same order - no deadlock cycle."""
    for cik in sorted({c for c in issuer_ciks if c}):
        lock_issuer(conn, cik, exclusive=exclusive)


def tenk_risk(conn: Connection, issuer_cik: str | None, at: datetime, *, include_superseded: bool = False,
              lock: bool = False) -> TenkRisk:
    """The validated 10-K vetoes that apply to a decision on `issuer_cik` at `at` (see module doc). lock=True takes
    the issuer's shared lock first and keeps it until the caller's transaction ends."""
    if not issuer_cik:
        return TenkRisk()
    if lock:
        lock_issuer(conn, issuer_cik, exclusive=False)
    rows = conn.execute(text(f"""
        SELECT id, payload->>'direction' = 'NEGATIVE' AS negative,
               coalesce(payload->'change_types' ? 'NEW_GOING_CONCERN', false) AS going_concern
        FROM events
        WHERE issuer_cik = :c AND event_type = 'TENK_CHANGE' {"" if include_superseded else "AND NOT superseded"}
          AND (payload->>'direction' = 'NEGATIVE' OR coalesce(payload->'change_types' ? 'NEW_GOING_CONCERN', false))
          AND coalesce(available_at, public_at) < :at AND coalesce(available_at, public_at) > :lo
        ORDER BY coalesce(available_at, public_at) DESC, id DESC"""),
        {"c": issuer_cik, "at": at, "lo": at - timedelta(days=TENK_WINDOW_DAYS)}).all()
    vetoes = [v for v, hit in ((TENK_NEGATIVE_RECENT, any(r.negative for r in rows)),
                               (TENK_NEW_GOING_CONCERN, any(r.going_concern for r in rows))) if hit]
    return TenkRisk(tuple(vetoes), tuple(r.id for r in rows))
