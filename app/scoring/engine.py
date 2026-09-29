"""Investment scoring V1 (RESEARCH-ONLY): an evidence-weighted estimate, kept apart from the hard gate.

    score = edge of the most specific VALIDATED evidence cell the event falls into
          + VALIDATED risk adjustments that apply at the scoring moment
    unit  = probability points of beating SPY over 90 days vs a matched control of the same stock
    ci_low = score - 1.96 * sqrt(sum of the components' squared standard errors)

The hard gate (app.paper.proposals.evaluate_gate: identity, duplicate, liquidity, severe financing risk, direction,
family policy) is NOT part of the score and is never changed by it: the gate's failed rules are stored next to the
score as vetoes. Nothing here sizes, approves or places an order; MAIN books and real money are untouched.

Every input a component reads is point in time: only events with available_at <= the scoring moment. Components
without validated evidence (13D, 13F, Congress, contracts, news, discovery, insider history profiles, every V2 family)
are listed with weight 0 and the reason - no chosen weights. When several validated cells match, the SMALLEST effect
is used (the conservative choice)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.scoring.evidence import NOT_MEASURED, NOT_SIGNIFICANT, registry

MODEL_VERSION = "score.v1-evidence"
Z95 = 1.96
TENK_WINDOW_DAYS = 90  # the 10-K effect was measured over the 90 days after the change

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
    why = reg["unmeasured"].get(fam.split(" ")[0], "no validated evidence") if fam else "V2 family: not measured yet"
    comps.append({"component": fam or et, "applies": False, "status": NOT_SIGNIFICANT if fam else NOT_MEASURED,
                  "effect_pp": 0.0, "companies": None, "p": None, "why": why if isinstance(why, str) else f"measured: {why}"})
    return None, "NO_VALIDATED_CELL", comps


def score_event(conn: Connection, ev: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    reg = registry()
    public = ev.get("available_at") or ev.get("public_at")
    if public is None or public > now:  # the event itself must be public at the scoring moment
        return {"model_version": MODEL_VERSION, "evidence_version": reg["version"], "scored_at": now, "score_pp": 0.0,
                "ci_low_pp": 0.0, "cell": "NOT_PUBLIC_YET" if public else "NO_PUBLIC_TIME",
                "components": [{"component": "EVENT", "applies": False, "status": NOT_MEASURED, "effect_pp": 0.0,
                                "companies": None, "p": None,
                                "why": "event not public at the scoring moment" if public else "event has no public time"}]}
    cell, cell_name, comps = _edge_cell(ev, reg)
    parts = [cell] if cell else []
    tk = reg["TENK_CHANGE_RECENT"]
    recent = conn.execute(text("""SELECT id FROM events WHERE issuer_cik = :c AND event_type = 'TENK_CHANGE' AND NOT superseded
                                  AND coalesce(available_at, public_at) <= :now AND coalesce(available_at, public_at) > :lo
                                  ORDER BY coalesce(available_at, public_at) DESC LIMIT 1"""),
                          {"c": ev.get("issuer_cik"), "now": now, "lo": now - timedelta(days=TENK_WINDOW_DAYS)}).scalar() \
        if ev.get("issuer_cik") else None
    comps.append({**_comp("TENK_CHANGE_RECENT", tk, bool(recent) and tk.usable,
                          "" if recent else f"no 10-K material change in the {TENK_WINDOW_DAYS} days before"),
                  "evidence_event": recent})
    if recent and tk.usable:
        parts.append(tk)
    score = sum(e.effect for e in parts)
    se = math.sqrt(sum(e.se ** 2 for e in parts)) if parts else 0.0
    return {"model_version": MODEL_VERSION, "evidence_version": reg["version"], "scored_at": now,
            "score_pp": round(100 * score, 3), "ci_low_pp": round(100 * (score - Z95 * se), 3) if parts else 0.0,
            "cell": cell_name, "components": comps}


def record(engine: Engine, event_id: int, vetoes: list[str] | None = None, now: datetime | None = None) -> dict | None:
    """Score one event and store it (research record; idempotent per event + model version)."""
    import json

    from app.db.models import events
    from sqlalchemy import select
    with engine.begin() as conn:
        ev = conn.execute(select(events).where(events.c.id == event_id)).mappings().first()
        if ev is None:
            return None
        s = score_event(conn, dict(ev), now)
        conn.execute(text("""INSERT INTO signal_scores (event_id, model_version, evidence_version, scored_at, score_pp, ci_low_pp,
                                                        cell, components, vetoes, research_only)
                             VALUES (:e, :mv, :ev, :at, :s, :lo, :cell, CAST(:comp AS jsonb), CAST(:veto AS jsonb), true)
                             ON CONFLICT (event_id, model_version) DO NOTHING"""),
                     {"e": event_id, "mv": s["model_version"], "ev": s["evidence_version"], "at": s["scored_at"],
                      "s": s["score_pp"], "lo": s["ci_low_pp"], "cell": s["cell"], "comp": json.dumps(s["components"], default=str),
                      "veto": json.dumps(sorted(vetoes or []))})
    return s


def family_status() -> list[dict]:
    """Every declared family (V1 alpha + V2 registry) with its scoring status - the audit view of what is wired."""
    from app.core.signal_registry import FAMILIES as V2
    from app.paper.alpha import FAMILIES as V1
    reg = registry()
    rows = []
    for fam in V1:
        if fam == "INSIDER_BUY":
            e = reg["INSIDER_BUY"]
            rows.append({"family": fam, "status": e.status, "weight_pp": round(100 * e.effect, 2),
                         "buckets_validated": sorted(k for k, v in reg["INSIDER_BUY_BUCKETS"].items() if v.usable)})
        elif fam == "INSIDER_CLUSTER":
            e = reg["INSIDER_CLUSTER"]
            rows.append({"family": fam, "status": e.status, "weight_pp": round(100 * e.effect, 2), "why": e.why})
        elif fam == "10K_CHANGE":
            rows.append({"family": fam, "status": reg["TENK_CHANGE_RECENT"].status, "weight_pp": 0.0,
                         "why": "measured NEGATIVE: used only as the TENK_CHANGE_RECENT risk adjustment, never as a buy cell"})
        else:
            rows.append({"family": fam, "status": NOT_SIGNIFICANT if fam in reg["unmeasured"] else NOT_MEASURED,
                         "weight_pp": 0.0, "why": str(reg["unmeasured"].get(fam, "no validated evidence"))})
    tk = reg["TENK_CHANGE_RECENT"]
    rows.append({"family": "TENK_CHANGE_RECENT (risk adjustment)", "status": tk.status, "weight_pp": round(100 * tk.effect, 2)})
    seen = {r["family"] for r in rows}
    for f in V2:
        if f.signal_id not in seen:
            rows.append({"family": f.signal_id, "status": NOT_MEASURED, "weight_pp": 0.0,
                         "why": "V2 family: no matched-control result yet" + (" (context only)" if f.context_only else "")})
    return rows
