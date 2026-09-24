"""Living company thesis (every statement carries its evidence) + thesis change detector."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app.db.models import company_thesis, discovery_companies, discovery_evidence, news_story_clusters, thesis_changes, watchlist
from app.db.store import create_event

STRENGTHEN = {"MAJOR_CUSTOMER", "NEW_CONTRACT", "GOVERNMENT_CONTRACT", "COMMERCIALIZATION", "PRODUCTION_START",
              "CAPACITY_EXPANSION", "DESIGN_WIN", "FDA_APPROVAL", "GUIDANCE_RAISE", "GOVERNMENT_EQUITY", "GOVERNMENT_SUPPORT"}
WEAKEN = {"CUSTOMER_LOSS", "GUIDANCE_CUT", "FDA_REJECTION", "DILUTION", "CAPITAL_RAISE", "RECALL", "LAWSUIT", "CYBERSECURITY"}
INVALIDATE = {"BANKRUPTCY_RISK"}


def _tr(classes) -> str:
    from app.notifications.format import CLASS_TR
    return ", ".join(CLASS_TR.get(c, c) for c in sorted(classes))


def classify_change(classes: list[str], agreement: str | None, materiality: str) -> tuple[str, str]:
    """Returns (change, Turkish explanation)."""
    cs = set(classes)
    if cs & INVALIDATE:
        return "THESIS_INVALIDATION", "haberde iflas / borsadan çıkarılma riski var: " + _tr(cs & INVALIDATE)
    weak = cs & WEAKEN
    strong = cs & STRENGTHEN
    if strong and agreement in ("mou", "loi", "collaboration", "evaluation", "pilot"):
        strong = set()  # hype filter: non-binding news does not strengthen a thesis
    if weak and not strong:
        return "THESIS_WEAKENING", "olumsuz gelişme: " + _tr(weak)
    if strong and not weak and materiality in ("HIGH", "CRITICAL"):
        return "THESIS_STRENGTHENING", "önemli olumlu gelişme: " + _tr(strong)
    if strong and weak:
        return "NEUTRAL", f"karışık: olumlu ({_tr(strong)}) ve olumsuz ({_tr(weak)}) birlikte"
    return "NEUTRAL", "görüşü değiştiren bir gelişme yok"


def on_material_news(conn: Connection, cik: str | None, ticker: str | None, cluster_id: int, classes: list[str],
                     agreement: str | None, materiality: str, url: str, headline: str) -> str | None:
    """Only companies that are on the watchlist / in discovery get a thesis-change record."""
    if not cik:
        return None
    tracked = conn.execute(select(watchlist.c.category).where(watchlist.c.cik == cik)).first() or \
        conn.execute(select(discovery_companies.c.cik).where(discovery_companies.c.cik == cik)).first()
    if not tracked:
        return None
    change, why = classify_change(classes, agreement, materiality)
    res = conn.execute(pg_insert(thesis_changes).values(
        cik=cik, ticker=ticker, news_cluster_id=cluster_id, change=change, explanation=why,
        evidence=[{"url": url, "headline": headline[:300], "classes": classes, "agreement": agreement}],
    ).on_conflict_do_nothing(index_elements=["cik", "news_cluster_id"]).returning(thesis_changes.c.id)).first()
    if res and change != "NEUTRAL":
        create_event(conn, event_type=change, dedupe_key=f"THESIS:{cik}:{cluster_id}", public_date=datetime.now(timezone.utc).date(),
                     ticker=ticker, issuer_cik=cik, priority=3, title=f"${ticker}: {change.replace('_', ' ').lower()} - {why[:120]}",
                     payload={"change": change, "explanation": why, "cluster_id": cluster_id, "evidence_url": url,
                              "headline": headline[:300]}, source_url=url, date_quality="first_seen")
        if change in ("THESIS_WEAKENING", "THESIS_INVALIDATION"):
            row = conn.execute(select(watchlist).where(watchlist.c.cik == cik)).mappings().first()
            if row and not row["manual_override"] and row["category"] != "ARCHIVED":
                from app.discovery.watchlist import set_category
                set_category(conn, cik, ticker, "THESIS_WEAKENING", f"{change}: {why}", actor="auto")
    return change


def _ev(rows, n=3):
    return [{"text": r["sentence"][:400], "source": r["source_url"], "filed": str(r["filed_date"] or "")} for r in rows[:n]]


def build_thesis(conn: Connection, cik: str, ticker: str | None, *, dc: dict, risks: list[dict]) -> dict:
    """Assemble thesis sections strictly from stored evidence / numbers; empty when nothing is known."""
    ev = [dict(r) for r in conn.execute(select(discovery_evidence).where(discovery_evidence.c.cik == cik)
                                        .order_by(desc(discovery_evidence.c.filed_date))).mappings().all()]
    by_kind = lambda k, labels=None: [e for e in ev if e["kind"] == k and (labels is None or e["label"] in labels)]
    m = dc.get("metrics") or {}
    f = dc.get("features") or {}
    news = [dict(r) for r in conn.execute(select(news_story_clusters).where(
        news_story_clusters.c.cik == cik, news_story_clusters.c.materiality.in_(["HIGH", "CRITICAL"]))
        .order_by(desc(news_story_clusters.c.public_at)).limit(3)).mappings().all()]
    strong_cust = [e for e in by_kind("customer") if e["agreement"] in ("production_agreement", "paid_contract", "development_agreement")
                   and not e["hedged"] and e["relation"] not in ("competitor", "supplier")]
    sections = {
        "company": {"text": dc.get("name"), "evidence": []},
        "ticker": {"text": ticker, "evidence": []},
        "industry": {"text": ", ".join(dc.get("themes") or []) or None, "evidence": ["theme keywords in evidence sentences"]},
        "market_cap": {"text": f"${(dc.get('market_cap') or 0) / 1e6:,.0f}M ({dc.get('bucket')})" if dc.get("market_cap") else None,
                       "evidence": ["SEC cover-page shares x last close"]},
        "what_they_do": {"text": (by_kind("business") or [{}])[0].get("sentence"), "evidence": _ev(by_kind("business"), 1)},
        "why_it_matters": {"text": "; ".join(sorted({e["label"] for e in by_kind("bottleneck")}))[:300] or None,
                           "evidence": _ev(by_kind("bottleneck"))},
        "critical_bottleneck": {"text": "; ".join(sorted({e["label"] for e in by_kind("bottleneck", {"SOLE_SUPPLIER", "LIMITED_SUPPLY_BASE", "CAPACITY_CONSTRAINED"})})) or None,
                                "evidence": _ev(by_kind("bottleneck", {"SOLE_SUPPLIER", "LIMITED_SUPPLY_BASE", "CAPACITY_CONSTRAINED"}))},
        "commercialization_stage": {"text": dc.get("stage"), "evidence": (dc.get("features") or {}).get("stage_reasons", [])},
        "key_customers": {"text": ", ".join(sorted({f"{e['counterparty']} ({e['agreement']})" for e in strong_cust})) or None,
                          "evidence": _ev(strong_cust)},
        "growth_evidence": {"text": (f"TTM revenue ${(m.get('ttm_revenue') or 0) / 1e6:,.1f}M, YoY {m['yoy_latest']:+.0%}"
                                     if m.get("yoy_latest") is not None else None),
                            "evidence": ["SEC XBRL quarterly revenue"] + [fl for fl in m.get("flags", [])]},
        "government_support": {"text": "yes" if f.get("government_backed") or f.get("gov_contract_to_mcap") else None,
                               "evidence": _ev(by_kind("bottleneck", {"GOVERNMENT_BACKED"}))},
        "insider_activity": {"text": (f"{f.get('insider_buyers_90d')} buyer(s), ${f.get('insider_buy_value_90d', 0) / 1e3:,.0f}k in 90d"
                                      if f.get("insider_buy_value_90d") else None), "evidence": ["SEC Form 4"]},
        "major_risks": {"text": ", ".join(f"{r['risk']} ({r['severity']})" for r in risks) or None,
                        "evidence": [r["source_url"] for r in risks if r.get("source_url")]},
        "latest_material_news": {"text": news[0]["headline"] if news else None,
                                 "evidence": [{"cluster_id": n["id"], "public_at": str(n["public_at"]), "materiality": n["materiality"]} for n in news]},
        "valuation_snapshot": {"text": (f"market cap / TTM revenue = {dc['market_cap'] / m['ttm_revenue']:.1f}x"
                                        if dc.get("market_cap") and m.get("ttm_revenue") else None), "evidence": ["computed"]},
        "cash_runway": {"text": (f"~{m['runway_months']:.0f} months" if m.get("runway_months") is not None else
                                 ("cash-flow positive" if (m.get("annual_ocf") or 0) > 0 else None)),
                        "evidence": ["SEC XBRL cash and operating cash flow"]},
    }
    now = datetime.now(timezone.utc)
    st = pg_insert(company_thesis).values(cik=cik, ticker=ticker, sections=sections, thesis_last_updated=now)
    conn.execute(st.on_conflict_do_update(index_elements=["cik"], set_={"ticker": ticker, "sections": sections, "thesis_last_updated": now}))
    return sections
