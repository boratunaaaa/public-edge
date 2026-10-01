"""Investment scoring (RESEARCH-ONLY): an evidence-weighted estimate, kept apart from the hard gate.

    score  = edge of the VALIDATED evidence cell the event falls into (evidence v3: only ten_pct_any is active)
    vetoes = the gate's failed rules + the validated 10-K vetoes (app.core.tenk_policy - the SAME policy the gate
             enforces as a hard veto: NEGATIVE change / NEW_GOING_CONCERN public in the 90 days before)
    unit   = probability points of beating SPY over 90 days vs a matched control of the same stock
A veto never changes the score and the score never lifts a veto: any veto means "no action" (the same convention as
the gate's rules). Cells marked EVIDENCE_ONLY or HOLDOUT_REQUIRED are listed with weight 0 and are never stacked.

The hard gate (app.paper.proposals.evaluate_gate: identity, duplicate, liquidity, severe financing risk, direction,
validated 10-K risk, family policy) is NOT part of the score and is never changed by it: the gate's failed rules are stored next to the
score as vetoes. Nothing here sizes, approves or places an order; MAIN books and real money are untouched.

Every input a component reads is point in time: only events with available_at <= the scoring moment. Components
without validated evidence (13D, 13F, Congress, contracts, news, discovery, insider history profiles, every V2 family)
are listed with weight 0 and the reason - no chosen weights. When several validated cells match, the SMALLEST effect
is used (the conservative choice); weights are never added across cells of the same event."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.core.tenk_policy import TENK_NEGATIVE_RECENT as TENK_VETO  # the SAME policy the paper gate enforces
from app.core.tenk_policy import TENK_WINDOW_DAYS, tenk_risk
from app.scoring.evidence import EVIDENCE_ONLY, HOLDOUT_REQUIRED, NOT_MEASURED, NOT_SIGNIFICANT, registry

MODEL_VERSION = "score.v2-evidence"
# night-run subgroups derived from owner / issuer history: not evaluated at scoring time (weight 0 until a holdout)
DERIVED_HOLDOUT = ("serial_3plus_issuers_2y", "owner_history_negative", "indirect_only", "first_buy_same_issuer_5y")

# research_v2.INSIDER_BUCKETS as Python predicates with SQL three-valued logic: `(payload->>'x')::boolean` is NULL when
# the key is missing or null, NOT NULL is NULL, and a WHERE clause keeps a row only when the whole condition is TRUE.
def _b(p, k):
    """SQL boolean of payload->>k: True / False / None (unknown)."""
    v = p.get(k)
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    t = str(v).strip().lower()
    return True if t in ("true", "t", "1", "yes", "y", "on") else False if t in ("false", "f", "0", "no", "n", "off") else None


def _not(v):
    return None if v is None else not v


def _and(*vs):
    if any(v is False for v in vs):
        return False
    return None if any(v is None for v in vs) else True


def _f(p, k):
    try:
        return float(p.get(k)) if p.get(k) is not None else None
    except (TypeError, ValueError):
        return None


def _cmp(x, test):
    return None if x is None else test(x)


BUCKETS = {
    "role_ceo": lambda p: _b(p, "is_ceo"),
    "role_cfo": lambda p: _and(_b(p, "is_cfo"), _not(_b(p, "is_ceo"))),
    "role_director_only": lambda p: _and(_b(p, "is_director"), _not(_b(p, "is_ceo")), _not(_b(p, "is_cfo"))),
    "role_10pct_only": lambda p: _and(_b(p, "is_ten_pct"), _not(_b(p, "is_director")), _not(_b(p, "is_ceo")),
                                      _not(_b(p, "is_cfo"))),
    "role_other_officer": lambda p: _and(p.get("officer_title") is not None, _not(_b(p, "is_ceo")), _not(_b(p, "is_cfo"))),
    "value_lt_100k": lambda p: _cmp(_f(p, "value"), lambda x: x < 1e5),
    "value_100k_1m": lambda p: _cmp(_f(p, "value"), lambda x: 1e5 <= x < 1e6),
    "value_ge_1m": lambda p: _cmp(_f(p, "value"), lambda x: x >= 1e6),
    "incr_lt_10pct": lambda p: _cmp(_f(p, "holdings_increase"), lambda x: x < 0.10),
    "incr_10_50pct": lambda p: _cmp(_f(p, "holdings_increase"), lambda x: 0.10 <= x < 0.50),
    "incr_ge_50pct": lambda p: _cmp(_f(p, "holdings_increase"), lambda x: x >= 0.50),
    "ten_pct_any": lambda p: _b(p, "is_ten_pct"),  # research_scoring.EXTRA_BUCKETS: (payload->>'is_ten_pct')::boolean
}


def _discretionary(p) -> bool:
    """research_v2.DISCRETIONARY = NOT COALESCE((payload->>'is_10b5_1')::boolean, false)."""
    return not (_b(p, "is_10b5_1") or False)

# families without validated evidence (weight 0) - the reason is taken from the evidence file
UNVALIDATED_FAMILY = {"ACTIVIST_13D": "13D", "CONGRESS_BUY": "HOUSE_BUY", "MULTI_SIGNAL_EVENT": "MULTI_SIGNAL",
                      "MATERIAL_NEWS": "NEWS", "GOV_CONTRACT": "GOV_CONTRACT", "EARLY_DISCOVERY": "DISCOVERY",
                      "ACTIVIST_CROWDING": "ACTIVIST_CROWDING", "TENK_CHANGE": "10K_CHANGE (risk feature, not a buy cell)"}


def _comp(name: str, eff, applies: bool, why: str = "") -> dict:
    return {"component": name, "applies": applies, "status": eff.status if eff else NOT_MEASURED,
            "effect_pp": round(100 * eff.effect, 3) if (eff and applies) else 0.0,
            "companies": eff.companies if eff else None, "p": eff.p if eff else None,
            "why": why or (eff.why if eff else "")}


def _edge_cell(ev: dict, reg: dict) -> tuple[object | None, str, list[dict]]:
    et = ev.get("event_type")
    p = ev.get("payload") or {}
    comps: list[dict] = []
    if et == "INSIDER_OPEN_MARKET_BUY":
        if not _discretionary(p):
            return None, "OUTSIDE_EVIDENCE:10b5-1", [{"component": "INSIDER_BUY", "applies": False, "status": NOT_MEASURED,
                                                     "effect_pp": 0.0, "companies": None, "p": None,
                                                     "why": "pre-planned (10b5-1) purchase: the evidence covers discretionary buys only"}]
        matched = []
        for name, pred in BUCKETS.items():
            eff = reg["INSIDER_BUY_BUCKETS"][name]
            hit = pred(p) is True  # a WHERE keeps the row only when the condition is TRUE (not NULL)
            comps.append(_comp(f"INSIDER_BUY:{name}", eff, hit and eff.usable,
                               "" if (hit or not eff.usable) else "bucket does not match this filing"))
            if hit and eff.usable:
                matched.append(eff)
        for name in DERIVED_HOLDOUT:
            eff = reg["INSIDER_BUY_BUCKETS"].get(name)
            comps.append(_comp(f"INSIDER_BUY:{name}", eff, False, "history-derived subgroup, not evaluated at scoring: "
                                                                   + (eff.why if eff else "no result")))
        base = reg["INSIDER_BUY"]
        if matched:
            cell = min(matched, key=lambda e: e.effect)
            comps.append(_comp("INSIDER_BUY", base, False, f"superseded by the more specific cell {cell.name}"))
            return cell, cell.name, comps
        comps.append(_comp("INSIDER_BUY", base, base.usable))
        return (base if base.usable else None), ("INSIDER_BUY" if base.usable else "NO_VALIDATED_CELL"), comps
    if et == "CLUSTER_INSIDER_BUY":
        eff = reg["INSIDER_CLUSTER"]
        comps.append(_comp("INSIDER_CLUSTER", eff, eff.usable))
        return (eff if eff.usable else None), ("INSIDER_CLUSTER" if eff.usable else "NO_VALIDATED_CELL"), comps
    fam = UNVALIDATED_FAMILY.get(et)
    status, why = _evidence_status(fam.split(" ")[0], reg) if fam else _v2_status(et, reg)
    if et == "TENK_CHANGE":
        status, why = NOT_MEASURED, "a 10-K change is never a buy cell; a NEGATIVE one is a veto on the issuer's signals"
    comps.append({"component": fam or et, "applies": False, "status": status, "effect_pp": 0.0, "companies": None,
                  "p": None, "why": why})
    return None, "NO_VALIDATED_CELL", comps


def _evidence_status(family: str, reg: dict) -> tuple[str, str]:
    """NOT_SIGNIFICANT only for a family that WAS tested (a measured result in the evidence file); a family without any
    matched-control test is NOT_MEASURED - the two are never conflated."""
    e = reg["unmeasured"].get(family)
    if isinstance(e, dict) and e.get("sign_test_p") is not None:
        return NOT_SIGNIFICANT, (f"tested: {100 * e['difference']:+.2f} pp, p={e['sign_test_p']:.3g}, "
                                 f"{e.get('companies')} companies - not significant")
    return NOT_MEASURED, (e if isinstance(e, str) else "no matched-control result")


def _v2_status(event_type: str, reg: dict) -> tuple[str, str]:
    """A V2 event type from the night family run: tested (with its numbers) or not enough 90-day coverage."""
    r = reg["v2_families"].get(event_type)
    if not r:
        return NOT_MEASURED, "V2 family: not measured yet"
    if r.get("status") == "TESTED":
        return NOT_SIGNIFICANT, (f"tested: {100 * r['effect']:+.2f} pp, BH p={r['bh_p']:.3g}, {r['companies']} companies"
                                 " - not significant")
    return NOT_MEASURED, f"insufficient 90-day coverage ({r.get('companies_90d', 0)} companies < 100)"


def score_event(conn: Connection, ev: dict, now: datetime | None = None, context: bool = True) -> dict:
    """context=False (a retry at an earlier decision moment): the 10-K context is not rebuilt - supersession has no
    timestamp, so today's state could differ from the decision's; the component says so instead of guessing."""
    now = now or datetime.now(timezone.utc)
    reg = registry()
    public = ev.get("available_at") or ev.get("public_at")
    if public is None or public > now:  # the event itself must be public at the scoring moment
        return {"model_version": MODEL_VERSION, "evidence_version": reg["version"], "scored_at": now, "score_pp": 0.0,
                "cell": "NOT_PUBLIC_YET" if public else "NO_PUBLIC_TIME",
                "components": [{"component": "EVENT", "applies": False, "status": NOT_MEASURED, "effect_pp": 0.0,
                                "companies": None, "p": None,
                                "why": "event not public at the scoring moment" if public else "event has no public time"}],
                "vetoes": []}
    cell, cell_name, comps = _edge_cell(ev, reg)
    score = cell.effect if cell else 0.0  # one cell per event: weights are never stacked
    tk = reg["TENK_NEGATIVE_RECENT"]
    out = {"model_version": MODEL_VERSION, "evidence_version": reg["version"], "scored_at": now,
           "score_pp": round(100 * score, 3), "cell": cell_name, "components": comps, "vetoes": []}
    # app.core.tenk_policy: strictly before the decision moment; a retry (context=False) cannot rebuild that moment's
    # supersession state and counts superseded changes too (the conservative side of a veto)
    risk = tenk_risk(conn, ev.get("issuer_cik"), now, include_superseded=not context)
    note = "" if context else " (retry: superseded changes also count - the decision's state is not reconstructable)"
    comps.append({**_comp(TENK_VETO, tk, bool(risk.vetoes), (tk.why if risk.vetoes else
                          f"no NEGATIVE / going-concern 10-K change public in the {TENK_WINDOW_DAYS} days before") + note),
                  "evidence_event": risk.event_ids[0] if risk.event_ids else None})
    out["vetoes"].extend(risk.vetoes)
    return out


def record(engine: Engine, event_id: int, vetoes: list[str] | None = None, now: datetime | None = None,
           context: bool = True) -> dict | None:
    """Score one event and store it (research record; idempotent per event + model version)."""
    import json

    from app.db.models import events
    from sqlalchemy import select
    with engine.begin() as conn:
        ev = conn.execute(select(events).where(events.c.id == event_id)).mappings().first()
        if ev is None:
            return None
        s = score_event(conn, dict(ev), now, context=context)
        conn.execute(text("""INSERT INTO signal_scores (event_id, model_version, evidence_version, scored_at, score_pp,
                                                        cell, components, vetoes, research_only)
                             VALUES (:e, :mv, :ev, :at, :s, :cell, CAST(:comp AS jsonb), CAST(:veto AS jsonb), true)
                             ON CONFLICT (event_id, model_version) DO NOTHING"""),
                     {"e": event_id, "mv": s["model_version"], "ev": s["evidence_version"], "at": s["scored_at"],
                      "s": s["score_pp"], "cell": s["cell"], "comp": json.dumps(s["components"], default=str),
                      "veto": json.dumps(sorted(set(vetoes or []) | set(s["vetoes"])))})
    return s


def retry_missing(engine: Engine, event_types: list[str], since: datetime) -> int:
    """Score events whose proposal exists but whose score write failed (or the process died in between). They are no
    longer rescanned (a proposal exists), so they are scored here at the ORIGINAL decision moment (the proposal's
    creation time) with the gate outcome of that decision: a created proposal passed the gate - no vetoes."""
    from app.db.models import proposed_trades, signal_scores
    from sqlalchemy import and_, select
    with engine.connect() as conn:
        rows = conn.execute(select(proposed_trades.c.event_id, proposed_trades.c.proposal_created_at).where(and_(
            proposed_trades.c.event_type.in_(event_types), proposed_trades.c.proposal_created_at >= since,
            proposed_trades.c.event_id.isnot(None),
            ~select(signal_scores.c.event_id).where(and_(signal_scores.c.event_id == proposed_trades.c.event_id,
                                                         signal_scores.c.model_version == MODEL_VERSION)).exists()))
            .order_by(proposed_trades.c.proposal_created_at)).all()
    done = 0
    for eid, at in rows:  # one failing event never blocks the others: each is retried on its own
        try:
            record(engine, eid, [], now=at, context=False)
            done += 1
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("research score retry of event %s failed", eid)
    return done


def family_status() -> list[dict]:
    """Every declared family (V1 alpha + V2 registry) with its scoring status - the audit view of what is wired."""
    from app.core.signal_registry import FAMILIES as V2
    from app.paper.alpha import FAMILIES as V1
    reg = registry()
    rows = []
    for fam in V1:
        if fam == "INSIDER_BUY":
            e = reg["INSIDER_BUY"]
            b = reg["INSIDER_BUY_BUCKETS"]
            rows.append({"family": fam, "status": e.status, "weight_pp": round(100 * e.effect, 2), "why": e.why,
                         "buckets_validated": {k: round(100 * v.effect, 2) for k, v in b.items() if v.usable},
                         "evidence_only": sorted(k for k, v in b.items() if v.status == EVIDENCE_ONLY),
                         "holdout_required": sorted(k for k, v in b.items() if v.status == HOLDOUT_REQUIRED)})
        elif fam == "INSIDER_CLUSTER":
            e = reg["INSIDER_CLUSTER"]
            rows.append({"family": fam, "status": e.status, "weight_pp": round(100 * e.effect, 2), "why": e.why})
        elif fam == "10K_CHANGE":
            tk = reg["TENK_NEGATIVE_RECENT"]
            rows.append({"family": fam, "status": NOT_MEASURED, "weight_pp": 0.0,
                         "why": "never a buy cell; a NEGATIVE change is a scoring veto (TENK_NEGATIVE_RECENT): " + tk.why})
        else:
            st, why = _evidence_status(fam, reg)
            rows.append({"family": fam, "status": st, "weight_pp": 0.0, "why": why})
    tk = reg["TENK_NEGATIVE_RECENT"]
    rows.append({"family": "TENK_NEGATIVE_RECENT (veto)", "status": tk.status, "weight_pp": 0.0, "why": tk.why})
    seen = {r["family"] for r in rows}
    for f in V2:
        if f.signal_id not in seen:
            tested = [(et, _v2_status(et, reg)) for et in f.source_event_types if et in reg["v2_families"]]
            st, why = ((NOT_SIGNIFICANT if any(s == NOT_SIGNIFICANT for _e, (s, _w) in tested) else NOT_MEASURED),
                       "; ".join(f"{et}: {w}" for et, (_s, w) in tested)) if tested else \
                (NOT_MEASURED, "V2 family: no matched-control result yet")
            rows.append({"family": f.signal_id, "status": st, "weight_pp": 0.0,
                         "why": why + (" (context only)" if f.context_only else "")})
    return rows
