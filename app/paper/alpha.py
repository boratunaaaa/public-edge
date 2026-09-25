"""ALPHA VALIDATION: one honest verdict per signal family.

Status (data quality, from app.backtest.quality): TRUSTED | PARTIAL | BLOCKED.
Verdict (performance, 90-day company-equal SPY-beat rate):
  TRUSTED  -> OUTPERFORMING if the 95% interval is entirely above 50%, UNDERPERFORMING if entirely
              below, else INCONCLUSIVE
  PARTIAL  -> the worst/best-case bounds decide the same way (UNDERPERFORMING if even the best case
              is below 50%), else INCONCLUSIVE
  BLOCKED  -> BLOCKED (cannot be measured yet; no rate is shown)
An UNDERPERFORMING family is shown openly and never feeds automatic trade proposals.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.backtest import quality as Q
from app.backtest.probability import wilson_interval

# family -> (event type, SQL condition, walk-forward study or None)
FAMILIES: dict[str, tuple[str, str, str | None]] = {
    "INSIDER_BUY": ("INSIDER_OPEN_MARKET_BUY", "TRUE", "insider_buy_value"),
    "INSIDER_CLUSTER": ("CLUSTER_INSIDER_BUY", "TRUE", "cluster_size"),
    "13D": ("ACTIVIST_13D", "TRUE", "activist_purpose"),
    "ACTIVIST_CROWDING": ("ACTIVIST_CROWDING", "TRUE", None),
    "HOUSE_BUY": ("CONGRESS_BUY", "TRUE", None),
    "GOV_CONTRACT": ("GOV_CONTRACT", "TRUE", "contract_ratio"),
    "MULTI_SIGNAL": ("MULTI_SIGNAL_EVENT", "TRUE", None),
    "10K_CHANGE": ("TENK_CHANGE", "TRUE", "tenk_change"),
    "NEWS": ("MATERIAL_NEWS", "TRUE", "news_materiality"),
    "DISCOVERY": ("EARLY_DISCOVERY", "TRUE", None),
}
EVENT_FAMILY = {v[0]: k for k, v in FAMILIES.items()}
HORIZONS = (30, 90, 180)
VERDICT_HORIZON = 90


def verdict(st: dict) -> tuple[str, float | None, float | None]:
    """(verdict, ci_low, ci_high) for one horizon's quality stats."""
    status = st.get("status")
    if status == "TRUSTED" and st.get("company_equal_rate") is not None and st.get("company_n"):
        lo, hi = wilson_interval(st["company_equal_rate"] * st["company_n"], st["company_n"])
        if hi is not None and hi < 0.5:
            return "UNDERPERFORMING", lo, hi
        if lo is not None and lo > 0.5:
            return "OUTPERFORMING", lo, hi
        return "INCONCLUSIVE", lo, hi
    if status == "PARTIAL":
        wc, bc = st.get("worst_case"), st.get("best_case")
        if bc is not None and bc < 0.5:
            return "UNDERPERFORMING", wc, bc
        if wc is not None and wc > 0.5:
            return "OUTPERFORMING", wc, bc
        return "INCONCLUSIVE", wc, bc
    return "BLOCKED", None, None


def _walkforward(conn: Connection, study: str | None) -> dict | None:
    if not study:
        return {"status": "NOT_CONFIGURED"}
    rows = conn.execute(text("""SELECT fold, metrics->>'status' status, metrics->>'chosen' chosen
                                FROM walkforward_results WHERE study = :s AND run_id = (
                                  SELECT run_id FROM walkforward_results WHERE study = :s ORDER BY created_at DESC LIMIT 1)
                                ORDER BY fold"""), {"s": study}).mappings().all()
    if not rows:
        return {"status": "NOT_RUN_YET"}
    ok = [r for r in rows if r["status"] == "OK"]
    return {"status": "OK" if ok else "INSUFFICIENT_OUT_OF_SAMPLE_DATA", "folds": len(rows), "usable_folds": len(ok)}


def family_report(conn: Connection, family: str, use_cache: bool = True) -> dict:
    et, cond, study = FAMILIES[family]
    per_h = {}
    for h in HORIZONS:
        st = Q.compute(conn, et, "alpha", cond, {}, h, use_cache=use_cache, persist=True)
        v, lo, hi = verdict(st)
        trusted = st["status"] == "TRUSTED"
        per_h[h] = {"status": st["status"], "verdict": v, "episodes": st["unique_episodes"], "companies": st["company_n"],
                    "unresolved_share": st["unresolved_share"],
                    "median_excess": st["median_excess"] if trusted else None,
                    "spy_beat_rate": st["company_equal_rate"] if trusted else None,
                    "sector_beat_rate": st.get("sector_benchmark_rate") if trusted else None,
                    "worst_case": st["worst_case"] if st["status"] == "PARTIAL" else None,
                    "best_case": st["best_case"] if st["status"] == "PARTIAL" else None,
                    "ci": [lo, hi] if trusted else None,
                    "reason": None if trusted else Q.reason_text_tr(st)}
    main = per_h[VERDICT_HORIZON]
    return {"family": family, "event_type": et, "status": main["status"], "verdict": main["verdict"], "horizons": per_h,
            "walk_forward": _walkforward(conn, study)}


def all_families(conn: Connection, use_cache: bool = True) -> list[dict]:
    return [family_report(conn, f, use_cache) for f in FAMILIES]


def proposal_allowed(conn: Connection, event_type: str, research_mode: bool = False) -> tuple[bool, str]:
    """Is this family allowed to feed automatic paper-trade proposals?"""
    fam = EVENT_FAMILY.get(event_type)
    if fam is None:
        return False, "UNKNOWN_FAMILY"
    rep = family_report(conn, fam)
    if rep["verdict"] == "UNDERPERFORMING":
        return (research_mode, "FAMILY_UNDERPERFORMING")
    if rep["status"] == "BLOCKED":
        return (research_mode, "FAMILY_BLOCKED")
    if rep["verdict"] != "OUTPERFORMING":
        return (research_mode, "FAMILY_NOT_PROVEN")
    return True, "FAMILY_OUTPERFORMING"
