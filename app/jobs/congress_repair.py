"""Congress (House PTR) identity repair - dry run by default (one READ ONLY transaction), audited apply.

Root cause (audit 2026-09-29): CONGRESS_BUY issuer_cik came from TODAY's SEC ticker map, CONGRESS_CLUSTER_BUY events
had no CIK at all, and the PDF parser sometimes merged several disclosure lines into one record (the ticker of one
line attached to another line's trade). This job re-derives every event's identity at its trade date
(app.sources.congress.identity.resolve) and classifies it:

  SAME      identity unchanged and safe                -> confidence recorded; returns kept
  FILLED    no CIK before, now resolved (HIGH/MEDIUM)  -> CIK set; returns priced without an issuer removed, re-queued
  CHANGED   a different issuer than before             -> CIK corrected; the old issuer's returns removed, re-queued
  REVIEW    ambiguous / merged / unresolved / mismatch -> needs_review + IDENTITY_REVIEW, returns removed (parked)

Cluster events get the issuer of their member trades: every member trade must resolve to the same safe issuer
(else REVIEW). Every write is recorded in data_cleanup_audit (before / after) under one run_id; undo(run_id) restores
the event columns (removed returns are recomputed by the return queue).

    python -m app.jobs.congress_repair            # dry run: counts + examples, nothing written
    python -m app.jobs.congress_repair --apply    # never run without an explicit instruction"""
from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.sources.congress.identity import Identity, resolve

REVIEW_PARK_DAYS = 3650


def _tx_key(dedupe_key: str) -> tuple[str, int] | None:
    parts = (dedupe_key or "").split(":")  # CONG:house:<doc_id>:<line_no>
    if len(parts) == 4 and parts[0] == "CONG":
        try:
            return parts[2], int(parts[3])
        except ValueError:
            return None
    return None


def classify(old_cik: str | None, ident: Identity) -> str:
    if ident.review:
        return "REVIEW"
    if old_cik is None:
        return "FILLED"
    return "SAME" if ident.cik == old_cik else "CHANGED"


def _buy_rows(conn: Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(text("""
        SELECT e.id, e.dedupe_key, e.issuer_cik, e.ticker, e.transaction_date, e.public_date, e.needs_review,
               e.identity_confidence, t.asset_name, t.ticker tx_ticker, t.transaction_date tx_date,
               EXISTS (SELECT 1 FROM event_returns r WHERE r.event_id = e.id) has_ret
        FROM events e
        LEFT JOIN congress_transactions t ON t.chamber = 'house' AND t.doc_id = split_part(e.dedupe_key, ':', 3)
             AND t.line_no = CASE WHEN split_part(e.dedupe_key, ':', 4) ~ '^[0-9]+$' THEN split_part(e.dedupe_key, ':', 4)::int END
        WHERE e.event_type = 'CONGRESS_BUY' AND NOT e.superseded ORDER BY e.id""")).mappings()]


def _cluster_rows(conn: Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(text("""
        SELECT e.id, e.issuer_cik, e.ticker, e.transaction_date, e.public_date, e.payload, e.needs_review,
               EXISTS (SELECT 1 FROM event_returns r WHERE r.event_id = e.id) has_ret
        FROM events e WHERE e.event_type = 'CONGRESS_CLUSTER_BUY' AND NOT e.superseded ORDER BY e.id""")).mappings()]


def plan(conn: Connection, sample: int = 8) -> dict:
    """Identity per event (no writes). Returns the decisions and the counts."""
    from app.entities.registry import resolver
    resolver.load(conn, force=True)  # today's map is the fallback for symbols without filing history
    cache: dict[tuple, Identity] = {}

    def ident(ticker, asset, on) -> Identity:
        k = (ticker, asset, on)
        if k not in cache:
            cache[k] = resolve(conn, ticker, asset, on)
        return cache[k]
    decisions: list[dict] = []
    buys = _buy_rows(conn)
    by_tx: dict[tuple, Identity] = {}
    for r in buys:
        on = r["tx_date"] or r["transaction_date"] or r["public_date"]
        asset = r["asset_name"] if r["asset_name"] is not None else None
        i = ident(r["tx_ticker"] or r["ticker"], asset, on)
        k = _tx_key(r["dedupe_key"])
        if k:
            by_tx[k] = i
        decisions.append({"id": r["id"], "type": "CONGRESS_BUY", "old_cik": r["issuer_cik"], "cik": i.cik,
                          "confidence": i.confidence, "reason": i.reason, "kind": classify(r["issuer_cik"], i),
                          "has_ret": r["has_ret"], "ticker": r["tx_ticker"] or r["ticker"]})
    # clusters: the member trades (same ticker, purchases, inside the window, members named in the payload)
    for c in _cluster_rows(conn):
        p = c["payload"] or {}
        trades = p.get("trades") or []
        idents = []
        for t in trades:
            if t.get("doc_id") is not None and t.get("line_no") is not None:  # clusters written since this repair
                keys = [(str(t["doc_id"]), int(t["line_no"]))]
            else:  # older clusters: every matching disclosure line; they must all agree (never an arbitrary one)
                keys = [tuple(r) for r in conn.execute(text("""
                    SELECT doc_id, line_no FROM congress_transactions WHERE chamber = 'house' AND upper(ticker) = upper(:t)
                      AND politician = :p AND transaction_date = :d AND transaction_type = 'PURCHASE'
                    ORDER BY doc_id, line_no"""), {"t": c["ticker"], "p": t.get("politician"), "d": t.get("date")})]
            found = [by_tx.get(k) for k in keys]
            if not found or any(i is None or i.review for i in found) or len({i.cik for i in found}) > 1:
                idents.append(None)  # missing, unsafe or ambiguous member line
            else:
                idents.append(found[0])
        if not idents or any(i is None or i.review for i in idents):
            i = Identity(None, "LOW", "REVIEW_REQUIRED:CLUSTER_MEMBER_UNSAFE")
        elif len({i.cik for i in idents}) > 1:
            i = Identity(None, "LOW", "REVIEW_REQUIRED:CLUSTER_MEMBERS_DISAGREE")
        else:
            conf = "HIGH" if all(i.confidence == "HIGH" for i in idents) else "MEDIUM"
            i = Identity(idents[0].cik, conf, "CLUSTER_MEMBERS")
        decisions.append({"id": c["id"], "type": "CONGRESS_CLUSTER_BUY", "old_cik": c["issuer_cik"], "cik": i.cik,
                          "confidence": i.confidence, "reason": i.reason, "kind": classify(c["issuer_cik"], i),
                          "has_ret": c["has_ret"], "ticker": c["ticker"]})
    cnt: Counter = Counter()
    with_ret: Counter = Counter()
    reasons: Counter = Counter()
    examples: dict[str, list] = {}
    for d in decisions:
        key = f"{d['type']}:{d['kind']}"
        cnt[key] += 1
        if d["has_ret"]:
            with_ret[key] += 1
        if d["kind"] == "REVIEW":
            reasons[f"{d['type']}:{d['reason'].split(':')[1] if ':' in d['reason'] else d['reason']}"] += 1
        ex = examples.setdefault(key, [])
        if d["kind"] != "SAME" and len(ex) < sample:
            ex.append({k: d[k] for k in ("id", "ticker", "old_cik", "cik", "confidence", "reason")})
    stale = sum(v for k, v in with_ret.items() if not k.endswith(":SAME"))
    return {"decisions": decisions, "counts": dict(cnt), "with_returns": dict(with_ret), "review_reasons": dict(reasons),
            "returns_to_remove": stale, "returns_kept": sum(v for k, v in with_ret.items() if k.endswith(":SAME")),
            "examples": examples}


def _snapshot(conn: Connection, eid: int, lock: bool = True) -> dict:
    """The complete event row and its return rows, as JSON (restored exactly by undo)."""
    row = conn.execute(text(f"SELECT to_jsonb(e) FROM events e WHERE id = :i {'FOR UPDATE' if lock else ''}"), {"i": eid}).scalar()
    rets = conn.execute(text("SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.horizon_days), '[]'::jsonb) "
                             "FROM event_returns r WHERE r.event_id = :i"), {"i": eid}).scalar()
    return {"event": row, "returns": rets}


def _issuer_name(conn: Connection, cik: str) -> str | None:
    from app.entities.registry import resolver
    return resolver.cik_to_name.get(cik) or conn.execute(text("SELECT name FROM security_master WHERE cik = :c"),
                                                         {"c": cik}).scalar()


def apply(conn: Connection, decisions: list[dict], run_id: str, now: datetime) -> dict:
    from app.entities.registry import resolver
    resolver.load(conn, force=True)
    park = now + timedelta(days=REVIEW_PARK_DAYS)
    n: Counter = Counter()
    for d in sorted(decisions, key=lambda x: x["id"]):  # event rows locked in id order (return worker's lock order)
        eid, kind = d["id"], d["kind"]
        before = _snapshot(conn, eid)
        removed = conn.execute(text("DELETE FROM event_returns WHERE event_id = :i"), {"i": eid}).rowcount \
            if kind != "SAME" else 0
        # an earlier IDENTITY review is lifted once the identity is safe; a review for any other reason stays
        # the review is lifted only when its CURRENT reason is the identity (a stale payload marker next to another
        # reason - e.g. DATA_QUALITY - never lifts that review); the stale identity marker itself is always dropped
        was_id_review = "(missing_reason = 'IDENTITY_REVIEW' OR (missing_reason IS NULL AND payload ? 'identity_review'))"
        if kind == "SAME":
            conn.execute(text(f"""UPDATE events SET identity_confidence = :cf,
                                  needs_review = CASE WHEN {was_id_review} THEN false ELSE needs_review END,
                                  returns_status = CASE WHEN missing_reason = 'IDENTITY_REVIEW' THEN 'pending' ELSE returns_status END,
                                  returns_next_at = CASE WHEN missing_reason = 'IDENTITY_REVIEW' THEN NULL ELSE returns_next_at END,
                                  missing_reason = CASE WHEN missing_reason = 'IDENTITY_REVIEW' THEN NULL ELSE missing_reason END,
                                  payload = payload - 'identity_review' WHERE id = :i"""), {"cf": d["confidence"], "i": eid})
        elif kind in ("FILLED", "CHANGED"):
            conn.execute(text(f"""UPDATE events SET issuer_cik = :c, issuer_id = :c, identity_confidence = :cf,
                                  company_name = coalesce(:nm, company_name), ticker = coalesce(:t, ticker),
                                  needs_review = CASE WHEN {was_id_review} THEN false ELSE needs_review END,
                                  returns_status = 'pending', returns_checked_at = NULL, returns_next_at = NULL,
                                  returns_error = NULL, price_symbol = NULL, price_symbol_basis = NULL,
                                  missing_reason = CASE WHEN missing_reason = 'IDENTITY_REVIEW' THEN NULL ELSE missing_reason END,
                                  payload = (payload - 'identity_review') || CAST(:p AS jsonb) WHERE id = :i"""),
                         {"c": d["cik"], "cf": d["confidence"], "t": d["ticker"], "i": eid, "nm": _issuer_name(conn, d["cik"]),
                          "p": json.dumps({"identity_repair": d["reason"]})})
        else:
            conn.execute(text("""UPDATE events SET needs_review = true, identity_confidence = 'LOW', issuer_cik = NULL,
                                 issuer_id = NULL, returns_status = 'no_data', missing_reason = 'IDENTITY_REVIEW',
                                 returns_next_at = :n, returns_checked_at = :now, price_symbol = NULL,
                                 price_symbol_basis = NULL, payload = payload || CAST(:p AS jsonb) WHERE id = :i"""),
                         {"i": eid, "n": park, "now": now, "p": json.dumps({"identity_review": d["reason"]})})
        conn.execute(text("""INSERT INTO data_cleanup_audit (run_id, action, table_name, row_id, before, after, reason, dry_run)
                             VALUES (:r, :a, 'events', :i, CAST(:b AS jsonb), CAST(:af AS jsonb), :why, false)"""),
                     {"r": run_id, "a": f"congress_{kind.lower()}", "i": eid, "b": json.dumps(before, default=str),
                      "af": json.dumps({**_snapshot(conn, eid, lock=False), "cik": d["cik"], "confidence": d["confidence"],
                                        "returns_removed": removed}, default=str),
                      "why": d["reason"][:500]})
        n[kind] += 1
        n["returns_removed"] += removed
    return dict(n)


def undo(conn: Connection, run_id: str) -> int:
    """Restore every event row of a run exactly as it was (all columns) and its original return rows; returns
    computed after the run for the repaired identity are removed first."""
    cols = [c for c in conn.execute(text("""SELECT column_name FROM information_schema.columns WHERE table_name = 'events'
                                            AND table_schema = current_schema() ORDER BY ordinal_position""")).scalars()
            if c != "id"]
    rows = conn.execute(text("SELECT row_id, before FROM data_cleanup_audit WHERE run_id = :r AND table_name = 'events' "
                             "ORDER BY row_id"), {"r": run_id}).all()
    col_list = ", ".join(cols)
    for eid, b in rows:
        conn.execute(text("SELECT 1 FROM events WHERE id = :i FOR UPDATE"), {"i": eid})
        conn.execute(text(f"""UPDATE events SET ({col_list}) = (SELECT {col_list} FROM jsonb_populate_record(NULL::events,
                              CAST(:j AS jsonb))) WHERE id = :i"""), {"j": json.dumps(b["event"]), "i": eid})
        conn.execute(text("DELETE FROM event_returns WHERE event_id = :i"), {"i": eid})
        conn.execute(text("""INSERT INTO event_returns SELECT * FROM jsonb_populate_recordset(NULL::event_returns,
                             CAST(:j AS jsonb))"""), {"j": json.dumps(b["returns"])})
    return len(rows)


def run(engine: Engine, do_apply: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if not do_apply:
        with engine.connect() as c:
            c.execute(text("SET TRANSACTION READ ONLY"))
            p = plan(c)
            c.rollback()
        p.pop("decisions")
        return {"mode": "DRY_RUN", **p}
    run_id = f"congress-identity-{now.date().isoformat()}-{uuid.uuid4().hex[:6]}"
    with engine.begin() as c:
        p = plan(c)
        out = apply(c, p.pop("decisions"), run_id, now)
    return {"mode": "APPLY", "run_id": run_id, **p, "applied": out}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    from app.db.session import get_engine
    print(json.dumps(run(get_engine(), a.apply), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
