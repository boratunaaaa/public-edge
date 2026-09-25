"""Legacy duplicate cleanup. Nothing is deleted: rows are marked superseded (with the canonical row
they fold into) and every change is written to data_cleanup_audit with before/after values.
Run with dry_run=True first; the dry run writes the same audit rows flagged dry_run=true.

  1. ACTIVIST_13D  - an amendment is a new episode only when ownership moved >= 1 percentage point
                     or the Item 4 purpose class changed; boilerplate amendments fold into the
                     investor/issuer episode's canonical event.
  2. MULTI_SIGNAL  - one canonical event per ticker + source-group combination + rolling episode
                     (same rule as the live detector: none within MULTI_WINDOW_DAYS before).
  3. MULTI-OWNER   - Form 4 buy events of joint filings re-derived with the owner-attribution rules;
                     the old per-primary-owner event folds into the re-derived one.
  4. CLUSTERS      - insider clusters re-counted by independent beneficial owners; clusters of a
                     single economic owner are superseded, over-counted ones corrected in place.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import and_, select, text
from sqlalchemy.engine import Connection, Engine

from app.db.models import activist_filings, data_cleanup_audit, events, insider_transactions
from app.signals.detect import FAMILY_GROUP, MULTI_WINDOW_DAYS, insider_buy_events, purchase_groups
from app.signals.owners import beneficial_roots, owner_key

log = logging.getLogger(__name__)
MATERIAL_PP = 1.0


def _audit(conn: Connection, run_id: str, action: str, row_id: int | None, before: dict | None, after: dict | None,
           reason: str, dry_run: bool, table: str = "events") -> None:
    conn.execute(data_cleanup_audit.insert().values(
        run_id=run_id, action=action, table_name=table, row_id=row_id,
        before=json.loads(json.dumps(before, default=str)) if before is not None else None,
        after=json.loads(json.dumps(after, default=str)) if after is not None else None,
        reason=reason[:2000], dry_run=dry_run))


def _supersede(conn: Connection, run_id: str, ev: dict, canonical_id: int | None, reason: str, dry_run: bool) -> None:
    before = {"superseded": ev.get("superseded", False), "canonical_id": ev.get("canonical_id"),
              "superseded_reason": ev.get("superseded_reason"), "event_type": ev.get("event_type"),
              "dedupe_key": ev.get("dedupe_key"), "ticker": ev.get("ticker"), "public_date": ev.get("public_date")}
    after = {"superseded": True, "canonical_id": canonical_id, "superseded_reason": reason}
    _audit(conn, run_id, "SUPERSEDE", ev["id"], before, after, reason, dry_run)
    if not dry_run:
        conn.execute(events.update().where(events.c.id == ev["id"]).values(
            superseded=True, canonical_id=canonical_id, superseded_reason=reason[:64]))


# ------------------------------------------------------------------ 1. 13D amendments

def is_material_amendment(change: float | None, cls: str | None, prev_cls: str | None, has_prev: bool) -> bool:
    if not has_prev:
        return True  # first filing of the pair inside our data: it opens the episode
    if change is not None and abs(change) >= MATERIAL_PP:
        return True
    return prev_cls is not None and cls is not None and cls != prev_cls


def cleanup_13d(conn: Connection, run_id: str, dry_run: bool) -> dict:
    rows = conn.execute(text("""
        SELECT e.id, e.event_type, e.dedupe_key, e.ticker, e.public_date, e.superseded, e.canonical_id,
               e.superseded_reason, e.payload, a.investor_cik, a.issuer_cik, a.filed_date, a.percent, a.item4_class,
               a.is_amendment
        FROM events e JOIN activist_filings a ON a.accession = e.payload->>'accession'
        WHERE e.event_type = 'ACTIVIST_13D' AND NOT e.superseded
        ORDER BY a.issuer_cik, a.investor_cik, a.filed_date, e.id""")).mappings().all()
    # full filing history per pair (amendments that never became events still carry the prior state)
    hist: dict[tuple, list] = defaultdict(list)
    for f in conn.execute(select(activist_filings.c.issuer_cik, activist_filings.c.investor_cik, activist_filings.c.filed_date,
                                 activist_filings.c.percent, activist_filings.c.item4_class, activist_filings.c.accession)
                          .where(activist_filings.c.is_13d.is_(True)).order_by(activist_filings.c.filed_date)).all():
        hist[(f.issuer_cik, f.investor_cik)].append(f)
    by_pair: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by_pair[(r["issuer_cik"], r["investor_cik"])].append(dict(r))
    n = kept = 0
    for pair, evs in by_pair.items():
        if not pair[0] or not pair[1]:
            continue
        canonical = None
        for ev in evs:
            prior = [f for f in hist[pair] if f.filed_date < ev["filed_date"] or
                     (f.filed_date == ev["filed_date"] and f.accession < (ev["payload"] or {}).get("accession", ""))]
            prev = prior[-1] if prior else None
            change = (ev["percent"] - prev.percent) if (prev is not None and ev["percent"] is not None and prev.percent is not None) else None
            material = (not ev["is_amendment"]) or is_material_amendment(change, ev["item4_class"],
                                                                          prev.item4_class if prev else None, prev is not None)
            if material or canonical is None:
                canonical = ev["id"]
                kept += 1
                continue
            _supersede(conn, run_id, ev, canonical, "13D_IMMATERIAL_AMENDMENT", dry_run)
            n += 1
    return {"superseded": n, "kept": kept}


# ------------------------------------------------------------------ 2. multi-signal episodes

def groups_of(payload: dict) -> tuple[str, ...]:
    g = payload.get("source_groups")
    if not g:
        g = sorted({FAMILY_GROUP[f] for f in (payload.get("families") or []) if f in FAMILY_GROUP})
    return tuple(sorted(g))


def canonical_multi(evs: list[dict], window_days: int = MULTI_WINDOW_DAYS) -> list[tuple[dict, int | None]]:
    """[(event, canonical_id_or_None)] for one ticker. Same rule as the live detector: an event is
    canonical when no canonical event with the same source groups exists in the preceding window."""
    out = []
    last: dict[tuple, dict] = {}
    for ev in sorted(evs, key=lambda e: (e["public_date"], e["id"])):
        g = groups_of(ev["payload"] or {})
        prev = last.get(g)
        if prev is not None and (ev["public_date"] - prev["public_date"]).days <= window_days:
            out.append((ev, prev["id"]))
        else:
            last[g] = ev
            out.append((ev, None))
    return out


def cleanup_multi_signal(conn: Connection, run_id: str, dry_run: bool) -> dict:
    rows = [dict(r) for r in conn.execute(select(events).where(and_(
        events.c.event_type == "MULTI_SIGNAL_EVENT", events.c.superseded.is_(False)))).mappings().all()]
    by_t: dict[str, list] = defaultdict(list)
    for r in rows:
        by_t[r["ticker"]].append(r)
    n = 0
    for _t, evs in by_t.items():
        for ev, canon in canonical_multi(evs):
            if canon is not None:
                _supersede(conn, run_id, ev, canon, "MULTI_SIGNAL_SAME_EPISODE", dry_run)
                n += 1
    return {"superseded": n, "total": len(rows)}


# ------------------------------------------------------------------ 3. multi-owner Form 4

def cleanup_multi_owner(conn: Connection, run_id: str, dry_run: bool) -> dict:
    accs = [a for (a,) in conn.execute(select(insider_transactions.c.accession).where(and_(
        insider_transactions.c.joint_owner_ciks.isnot(None), insider_transactions.c.category == "OPEN_MARKET_BUY",
        insider_transactions.c.is_derivative.is_(False))).distinct()).all()]
    n = created = 0
    for acc in accs:
        old = [dict(r) for r in conn.execute(select(events).where(and_(
            events.c.event_type == "INSIDER_OPEN_MARKET_BUY", events.c.superseded.is_(False),
            events.c.dedupe_key.like(f"IOMB:{acc}:%")))).mappings().all()]
        if not old:
            continue
        rows = conn.execute(select(insider_transactions).where(and_(
            insider_transactions.c.accession == acc, insider_transactions.c.category == "OPEN_MARKET_BUY",
            insider_transactions.c.is_derivative.is_(False)))).mappings().all()
        expected = {f"IOMB:{acc}:{k}"[:200] for k in purchase_groups(conn, rows)}
        stale = [e for e in old if e["dedupe_key"] not in expected]
        if not stale:
            continue
        if dry_run:
            for e in stale:
                _audit(conn, run_id, "SUPERSEDE", e["id"], {"dedupe_key": e["dedupe_key"]}, {"expected": sorted(expected)},
                       "FORM4_MULTI_OWNER_REATTRIBUTED", True)
                n += 1
            continue
        new_ids = insider_buy_events(conn, acc, is_backfill=old[0]["is_backfill"])
        created += len(new_ids)
        canon = conn.execute(select(events.c.id).where(events.c.dedupe_key.in_(list(expected))).order_by(events.c.id)).scalar()
        for e in stale:
            _supersede(conn, run_id, e, canon, "FORM4_MULTI_OWNER_REATTRIBUTED", dry_run)
            n += 1
        for nid in new_ids:
            _audit(conn, run_id, "CREATE", nid, None, {"accession": acc}, "FORM4_MULTI_OWNER_REATTRIBUTED", False)
    return {"superseded": n, "created": created, "joint_filings": len(accs)}


# ------------------------------------------------------------------ 4. clusters by beneficial owner

def cleanup_clusters(conn: Connection, run_id: str, dry_run: bool) -> dict:
    """Only issuers where the graph can change a count: joint rows or beneficial-owner links."""
    issuers = {c for (c,) in conn.execute(text("""
        SELECT DISTINCT issuer_cik FROM insider_transactions WHERE joint_owner_ciks IS NOT NULL
        UNION SELECT DISTINCT issuer_cik FROM beneficial_owner_links WHERE issuer_cik IS NOT NULL""")).all()}
    if not issuers:
        return {"superseded": 0, "corrected": 0, "checked": 0}
    evs = [dict(r) for r in conn.execute(select(events).where(and_(
        events.c.event_type == "CLUSTER_INSIDER_BUY", events.c.superseded.is_(False),
        events.c.issuer_cik.in_(list(issuers))))).mappings().all()]
    sup = corr = 0
    for ev in evs:
        p = ev["payload"] or {}
        w = int(p.get("window_days") or 30)
        lo = ev["public_date"] - timedelta(days=w - 1)
        rows = conn.execute(select(insider_transactions.c.owner_cik, insider_transactions.c.owner_name,
                                   insider_transactions.c.joint_owner_ciks).where(and_(
            insider_transactions.c.issuer_cik == ev["issuer_cik"], insider_transactions.c.category == "OPEN_MARKET_BUY",
            insider_transactions.c.is_derivative.is_(False), insider_transactions.c.is_10b5_1.is_(False),
            insider_transactions.c.filed_date >= lo, insider_transactions.c.filed_date <= ev["public_date"]))).all()
        keys = {owner_key(a, b, c) for a, b, c in rows}
        n_ind = len(set(beneficial_roots(conn, keys).values())) if keys else 0
        n_old = int(p.get("number_of_buyers") or 0)
        if n_ind >= n_old:
            continue
        if n_ind < 2:
            _supersede(conn, run_id, ev, None, "CLUSTER_SAME_BENEFICIAL_OWNER", dry_run)
            sup += 1
            continue
        after = {"number_of_buyers": n_ind, "independent_beneficial_owners": n_ind, "number_of_buyers_reported": n_old}
        _audit(conn, run_id, "CORRECT_PAYLOAD", ev["id"], {"number_of_buyers": n_old}, after,
               "CLUSTER_BENEFICIAL_OWNER_RECOUNT", dry_run)
        if not dry_run:
            conn.execute(text("UPDATE events SET payload = payload || CAST(:x AS jsonb) WHERE id = :id"),
                         {"id": ev["id"], "x": json.dumps(after)})
        corr += 1
    return {"superseded": sup, "corrected": corr, "checked": len(evs)}


# ------------------------------------------------------------------ runner

def run_cleanup(engine: Engine, dry_run: bool = True, steps: tuple[str, ...] = ("13d", "multi", "owners", "clusters")) -> dict:
    run_id = f"{'dry' if dry_run else 'apply'}-{date.today().isoformat()}-{uuid.uuid4().hex[:8]}"
    out: dict = {"run_id": run_id, "dry_run": dry_run}
    fns = {"13d": cleanup_13d, "multi": cleanup_multi_signal, "owners": cleanup_multi_owner, "clusters": cleanup_clusters}
    for step in steps:
        with engine.begin() as conn:
            out[step] = fns[step](conn, run_id, dry_run)
        log.info("cleanup %s %s: %s", run_id, step, out[step])
    return out


def run_cleanup_safe(engine: Engine, steps: tuple[str, ...] = ("13d", "multi", "owners", "clusters")) -> dict:
    """Dry run first (audited), then apply, then compare the counts; both runs stay in the audit log."""
    dry = run_cleanup(engine, dry_run=True, steps=steps)
    applied = run_cleanup(engine, dry_run=False, steps=steps)
    mismatch = {s: (dry[s].get("superseded"), applied[s].get("superseded")) for s in steps
                if dry[s].get("superseded") != applied[s].get("superseded")}
    if mismatch:
        log.warning("cleanup dry-run vs apply counts differ: %s", mismatch)
    return {"dry_run": dry, "applied": applied, "count_mismatch": mismatch}
