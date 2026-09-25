"""DISCOVERY INTEREST SCORE - research priority, NOT a buy score.

Heuristic starting weights; every raw feature is stored so the weights can later be fitted
against the backtest. Price momentum is deliberately NOT a feature.
"""
from __future__ import annotations

MAJOR_CATEGORIES = {"HYPERSCALER", "TOP_SEMICONDUCTOR", "AUTOMOTIVE_OEM", "DEFENSE_PRIME", "US_GOVERNMENT", "LARGE_PHARMA",
                    "LEADING_AI", "SPACE_PRIME", "BIG_TECH"}
FORTUNE500_CLASS = {"HYPERSCALER", "TOP_SEMICONDUCTOR", "AUTOMOTIVE_OEM", "DEFENSE_PRIME", "LARGE_PHARMA", "BIG_TECH"}
PROGRESS_STAGES = {"CUSTOMER_QUALIFICATION", "EARLY_COMMERCIAL", "PRODUCTION_RAMP"}
FLAG_TR = {"REVENUE_ACCELERATION": "satış büyümesi art arda hızlanıyor", "BACKLOG_BREAKOUT": "bekleyen siparişler hızla arttı",
           "MARGIN_INFLECTION": "kârlılığı iyileşiyor", "CAPACITY_EXPANSION": "üretim kapasitesine yatırım yapıyor"}
STAGE_TR_SHORT = {"RESEARCH": "araştırma", "PRE-COMMERCIAL": "satış öncesi", "PILOT": "deneme üretimi",
                  "CUSTOMER_QUALIFICATION": "müşteri testi", "EARLY_COMMERCIAL": "ilk satışlar", "PRODUCTION_RAMP": "üretim artışı",
                  "MASS_PRODUCTION": "seri üretim", "SCALED": "büyük ölçek"}
BOTTLENECK_TR = {"SOLE_SUPPLIER": "tek/az sayıdaki tedarikçiden biri",
                 "CAPACITY_CONSTRAINED": "talep kapasitesini aşıyor", "LONG_TERM_SUPPLY": "uzun vadeli tedarik anlaşması",
                 "FIRST_OF_KIND": "ilk/benzersiz teknoloji"}
RISK_PENALTY = {("GOING_CONCERN", "HIGH"): 3.0, ("DILUTION", "HIGH"): 2.0, ("DILUTION", "MEDIUM"): 1.0,
                ("REVERSE_SPLIT", "MEDIUM"): 1.5, ("MIN_BID_DEFICIENCY", "HIGH"): 1.5, ("NASDAQ_MIN_BID_RISK", "HIGH"): 1.0,
                ("LOW_LIQUIDITY", "HIGH"): 1.5, ("LOW_LIQUIDITY", "MEDIUM"): 0.5, ("INSIDER_SELLING", "MEDIUM"): 1.0,
                ("LATE_FILING", "MEDIUM"): 1.5, ("MATERIAL_WEAKNESS", "MEDIUM"): 0.5, ("FREQUENT_CAPITAL_RAISES", "HIGH"): 1.0,
                ("CASH_RUNWAY", "HIGH"): 1.5, ("CASH_RUNWAY", "MEDIUM"): 0.5, ("EXTREME_VALUATION", "MEDIUM"): 1.0,
                ("PROMOTIONAL_PR", "MEDIUM"): 1.0, ("HIGH_SBC", "MEDIUM"): 0.5, ("NEGATIVE_WORKING_CAPITAL", "MEDIUM"): 0.5}


def features(*, market_cap: float | None, metrics: dict, stage: str | None, stage_prev: str | None,
             relations: list[dict], signal_labels: list[str], insider: dict, activist_13d: bool,
             gov_contract_ratio: float | None, risks: list[dict], themes: list[str] | None = None) -> dict:
    strong_customers = [r for r in relations if r["category"] in MAJOR_CATEGORIES and r["strength"] >= 0.6 and not r["hedged"]
                        and r.get("relation") not in ("competitor", "supplier")]
    return {
        "market_cap": market_cap,
        "in_priority_band": bool(market_cap and 100e6 <= market_cap <= 2e9),
        "revenue_yoy": metrics.get("yoy_latest"),
        "revenue_yoy_prev": metrics.get("yoy_prev"),
        "ttm_revenue": metrics.get("ttm_revenue"),
        "inflection_flags": metrics.get("flags", []),
        "stage": stage, "stage_prev": stage_prev,
        "stage_advanced": bool(stage_prev and stage and _rank(stage) > _rank(stage_prev)),
        "major_customers": sorted({f"{r['counterparty']} ({r['category']})" for r in strong_customers}),
        "major_customer_count": len({r["counterparty"] for r in strong_customers}),
        "weak_mentions": len([r for r in relations if r["strength"] < 0.6]),
        "bottleneck_signals": sorted(set(signal_labels) & {"SOLE_SUPPLIER", "CAPACITY_CONSTRAINED",
                                                          "LONG_TERM_SUPPLY", "FIRST_OF_KIND"}),
        "government_backed": "GOVERNMENT_BACKED" in signal_labels,
        "gov_contract_to_mcap": gov_contract_ratio,
        "insider_buy_value_90d": insider.get("buy_value", 0.0),
        "insider_buyers_90d": insider.get("buyers", 0),
        "activist_13d": activist_13d,
        "runway_months": metrics.get("runway_months"),
        "positive_ocf": (metrics.get("annual_ocf") or 0) > 0,
        "risks": [f"{r['risk']}:{r['severity']}" for r in risks],
        "themes": themes or [],
    }


def _rank(stage: str | None) -> int:
    from app.discovery.language import STAGES
    return STAGES.index(stage) if stage in STAGES else -1


def score(f: dict) -> tuple[float, str, list[str]]:
    s = 0.0
    why: list[str] = []
    mc = f.get("market_cap") or 0
    if f.get("in_priority_band"):
        s += 1.5
    elif 50e6 <= mc < 100e6 or 2e9 < mc < 10e9:
        s += 0.5
    y = f.get("revenue_yoy")
    if y is not None and f.get("revenue_yoy_prev") is not None:
        if y >= 0.5 and f["revenue_yoy_prev"] >= 0.25:
            s += 2
            why.append(f"satışlar bir yılda {y:+.0%} büyüdü (bir önceki çeyrekte {f['revenue_yoy_prev']:+.0%})")
        elif y >= 0.25:
            s += 1
            why.append(f"satışlar bir yılda {y:+.0%} büyüdü")
    flags = f.get("inflection_flags") or []
    for fl, w in (("REVENUE_ACCELERATION", 1.5), ("BACKLOG_BREAKOUT", 1.5), ("MARGIN_INFLECTION", 1.0), ("CAPACITY_EXPANSION", 0.5)):
        if fl in flags:
            s += w
            why.append(FLAG_TR.get(fl, fl))
    if f.get("stage") in PROGRESS_STAGES:
        s += 1
    if f.get("stage_advanced"):
        s += 1.5
        why.append(f"yeni aşamaya geçti: {STAGE_TR_SHORT.get(f['stage_prev'], f['stage_prev'])} → {STAGE_TR_SHORT.get(f['stage'], f['stage'])}")
    if f.get("major_customer_count"):
        s += min(4.0, 2.0 * f["major_customer_count"])
        why.append("büyük müşteri/ortak: " + ", ".join(f["major_customers"][:3]))
    if f.get("bottleneck_signals"):
        s += min(2.0, float(len(f["bottleneck_signals"])))
        why.append("kritik tedarikçi konumu: " + ", ".join(BOTTLENECK_TR.get(b, b) for b in f["bottleneck_signals"]))
    if f.get("government_backed"):
        s += 1
        why.append("devlet desteği / stratejik tedarik zinciri")
    r = f.get("gov_contract_to_mcap")
    if r and r >= 0.05:
        s += 1.5
        why.append(f"devlet sözleşmesi şirket değerinin {r:.0%}'i kadar")
    if f.get("insider_buyers_90d", 0) >= 2:
        s += 2
        why.append(f"son 90 günde {f['insider_buyers_90d']} yönetici hisse aldı")
    elif f.get("insider_buy_value_90d", 0) >= 100_000:
        s += 1.5
        why.append(f"yöneticiler son 90 günde {f['insider_buy_value_90d'] / 1e3:,.0f} bin $ hisse aldı")
    if f.get("activist_13d"):
        s += 1
        why.append("aktivist yatırımcı %5+ pay topladı (13D)")
    if f.get("positive_ocf") or (f.get("runway_months") or 0) >= 24:
        s += 1
    if f.get("stage") in ("RESEARCH", "PRE-COMMERCIAL"):
        s -= 1  # unproven technology
    for rk in f.get("risks") or []:
        risk, sev = rk.split(":")
        s -= RISK_PENALTY.get((risk, sev), 0.0)
    level = "HIGH" if s >= 6 else "MEDIUM" if s >= 3.5 else "LOW"
    # HIGH interest needs business evidence of the kind this engine looks for (a technology theme, a
    # validated customer or a bottleneck position) - revenue growth alone (shipping, miners...) is not enough
    if level == "HIGH" and not has_business_evidence(f):
        level = "MEDIUM"
    if level == "HIGH" and ((f.get("market_cap") or 0) >= 10e9 or f.get("stage") == "SCALED"):
        level = "MEDIUM"  # already scaled operating businesses are not early discoveries
    high_risks = {rk.split(":", 1)[0] for rk in (f.get("risks") or []) if rk.endswith(":HIGH")}
    if level == "HIGH" and (len(high_risks) >= 2 or "GOING_CONCERN" in high_risks):
        level = "MEDIUM"  # severe financing/distress risk cannot carry a HIGH-interest label
    return round(s, 2), level, why


def has_business_evidence(f: dict) -> bool:
    return bool(f.get("themes") or f.get("major_customer_count") or f.get("bottleneck_signals")
                or f.get("government_backed") or (f.get("gov_contract_to_mcap") or 0) >= 0.05)


def has_real_progress(f: dict) -> bool:
    """A company only becomes a discovery candidate on business evidence - never on price action.
    Revenue-only progress (growth flags / early stage) additionally needs a technology theme."""
    if not has_business_evidence(f) and not f.get("insider_buyers_90d", 0) >= 2:
        return False
    growth = bool(set(f.get("inflection_flags") or []) - {"CASH_RUNWAY_RISK"}) or \
        (f.get("stage") in PROGRESS_STAGES and (f.get("revenue_yoy") or 0) >= 0.25) or bool(f.get("stage_advanced"))
    return bool(f.get("major_customer_count") or f.get("bottleneck_signals") or (f.get("gov_contract_to_mcap") or 0) >= 0.05
                or f.get("insider_buyers_90d", 0) >= 2 or growth)
