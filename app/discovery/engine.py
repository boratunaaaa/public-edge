"""Discovery pipeline: universe -> fundamentals -> risks -> evidence -> per-company evaluation."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

from app.db.models import (activist_filings, discovery_companies, discovery_evidence, discovery_stage_history,
                           government_awards, insider_transactions, watchlist)
from app.db.store import create_event, job_state, save_job_state
from app.discovery import language as L
from app.discovery import metrics as M
from app.discovery import financing as F
from app.discovery import risks as R
from app.discovery import scoring as S
from app.discovery.evidence import collect, universe_ciks
from app.discovery.sec_data import build_universe, load_fundamentals, series
from app.discovery.thesis import build_thesis
from app.discovery.watchlist import auto_update

log = logging.getLogger(__name__)
FIRST_EVENTS = {"HYPERSCALER": "FIRST_HYPERSCALER", "AUTOMOTIVE_OEM": "FIRST_AUTOMOTIVE_OEM", "US_GOVERNMENT": "FIRST_DOD_CONTRACT"}


def _relations(conn: Connection, cik: str) -> tuple[list[dict], list[str], list[str]]:
    rows = conn.execute(select(discovery_evidence).where(discovery_evidence.c.cik == cik)).mappings().all()
    rels, labels, texts = [], [], []
    for r in rows:
        texts.append(r["sentence"])
        if r["kind"] in ("bottleneck", "stage"):
            # evidence stored before the boilerplate rules is re-checked at read time
            if not r["hedged"] and L.signal_ok(r["label"], r["sentence"]):
                labels.append(r["label"])
        elif r["kind"] == "customer":
            strength = L.REL_STRENGTH.get(r["agreement"], 0.4)
            if r["relation"] in ("competitor", "supplier"):
                strength = 0.0
            if r["hedged"]:
                strength *= 0.3
            if not L.relation_ok(r["sentence"]):
                strength = 0.0
            rels.append({"counterparty": r["counterparty"], "category": r["counterparty_category"], "relation": r["relation"],
                         "agreement": r["agreement"], "hedged": r["hedged"], "strength": strength,
                         "filed": r["filed_date"], "url": r["source_url"], "sentence": r["sentence"]})
    return rels, labels, texts


def _insider(conn: Connection, cik: str) -> dict:
    """Discretionary open-market buying in 90 days; buyers = independent beneficial owners."""
    from app.signals.owners import independent_owner_count, owner_key
    since = date.today() - timedelta(days=90)
    rows = conn.execute(select(insider_transactions.c.value, insider_transactions.c.owner_cik, insider_transactions.c.owner_name,
                               insider_transactions.c.joint_owner_ciks).where(and_(
        insider_transactions.c.issuer_cik == cik, insider_transactions.c.category == "OPEN_MARKET_BUY",
        insider_transactions.c.is_derivative.is_(False), insider_transactions.c.is_10b5_1.is_(False),
        insider_transactions.c.filed_date >= since))).all()
    keys = {owner_key(o, n, j) for _v, o, n, j in rows}
    return {"buy_value": float(sum(float(v or 0) for v, *_ in rows)), "buyers": independent_owner_count(conn, keys)}


NON_OPERATING_SECTORS = {"FINANCIALS", "REAL_ESTATE", "SHELL_SPAC"}
NON_OPERATING_NAME_RE = re.compile(
    r"\b(ETF|FUND|TRUST|BANCORP|BANCSHARES|BANK|BANKSHARES|REIT|CURRENCYSHARES|ACQUISITION CORP|REALTY|PROPERTIES)\b", re.I)


def _scope_excluded(conn: Connection, cik: str, name: str | None) -> str | None:
    """Banks, insurers, REITs, funds/ETFs and SPACs are outside an operating-company discovery engine."""
    from app.db.models import security_master
    row = conn.execute(select(security_master.c.sector, security_master.c.sic).where(security_master.c.cik == cik)).first()
    if row and row[0] in NON_OPERATING_SECTORS:
        return f"sector {row[0]} (SIC {row[1]})"
    if (row is None or row[0] is None) and name and NON_OPERATING_NAME_RE.search(name):
        return "name indicates a fund / bank / REIT"
    return None


def evaluate(conn: Connection, u: dict, *, is_backfill: bool) -> dict:
    cik, ticker = u["cik"], u["ticker"]
    s = lambda c: series(conn, cik, c)
    rev = s("Revenue")
    met = M.compute(rev, s("GrossProfit"), s("Cash"), s("OCF_annual"), s("RPO"), s("RD"), s("Capex_annual"), s("SBC_annual"),
                    s("CurrentAssets"), s("CurrentLiabilities"), s("Shares")).as_dict()
    rels, labels, texts = _relations(conn, cik)
    R.derived_risks(conn, cik, ticker, metrics=met, price=u.get("price"), market_cap=u.get("market_cap"), adv=u.get("adv_dollar_30d"))
    fin = F.assess(conn, cik, u.get("market_cap"), met, R.risks_for(conn, cik))
    risks = R.risks_for(conn, cik)
    stage, stage_reasons = L.classify_stage(labels, met.get("ttm_revenue"), met.get("yoy_latest"))
    prev = conn.execute(select(discovery_companies.c.stage, discovery_companies.c.interest_level)
                        .where(discovery_companies.c.cik == cik)).first()
    act = conn.execute(select(func.count()).select_from(activist_filings).where(and_(
        activist_filings.c.issuer_cik == cik, activist_filings.c.is_13d.is_(True),
        activist_filings.c.filed_date >= date.today() - timedelta(days=180)))).scalar()
    gov = conn.execute(select(func.max(government_awards.c.value_to_mcap)).where(and_(
        government_awards.c.ticker == ticker, government_awards.c.public_date_basis == "first_seen",
        or_(government_awards.c.mapping_confidence.is_(None),
            government_awards.c.mapping_confidence.in_(["VERIFIED", "HIGH_CONFIDENCE"])),
        government_awards.c.award_date >= date.today() - timedelta(days=365)))).scalar()
    themes = L.themes_in(" ".join(texts[:400]) + " " + (u.get("name") or ""))
    feats = S.features(market_cap=u.get("market_cap"), metrics=met, stage=stage, stage_prev=prev[0] if prev else None,
                       relations=rels, signal_labels=labels, insider=_insider(conn, cik), activist_13d=bool(act),
                       gov_contract_ratio=gov, risks=risks, themes=themes, texts=texts[:400], financing=fin)
    feats["stage_reasons"] = stage_reasons
    feats["scope_excluded"] = _scope_excluded(conn, cik, u.get("name"))
    sc, level, why = S.score(feats)
    candidate = S.has_real_progress(feats)
    now = datetime.now(timezone.utc)
    customers = sorted({(r["counterparty"], r["category"], r["agreement"]) for r in rels if r["strength"] >= 0.6})
    vals = dict(ticker=ticker, name=u.get("name"), market_cap=u.get("market_cap"), bucket=u.get("bucket"), themes=themes,
                stage=stage, interest_score=sc, interest_level=level, features=_jsonable(feats),
                customers=[{"counterparty": c, "category": k, "agreement": a} for c, k, a in customers], metrics=_jsonable(met),
                financing_risk_score=fin[0], financing_flags=fin[1], last_scanned_at=now)
    st = pg_insert(discovery_companies).values(cik=cik, first_discovered_at=now if candidate else None, **vals)
    conn.execute(st.on_conflict_do_update(index_elements=["cik"], set_={
        **vals, "first_discovered_at": func.coalesce(discovery_companies.c.first_discovered_at, st.excluded.first_discovered_at)}))
    created = []
    public = date.today()
    # stage transition
    if prev and prev[0] and stage and prev[0] != stage:
        conn.execute(discovery_stage_history.insert().values(cik=cik, stage=stage, previous_stage=prev[0], evidence=stage_reasons))
        advanced = prev[0] in L.STAGES and stage in L.STAGES and L.STAGES.index(stage) > L.STAGES.index(prev[0])
        if advanced:
            eid = create_event(conn, event_type="STAGE_TRANSITION", dedupe_key=f"STAGE:{cik}:{prev[0]}->{stage}", public_date=public,
                               ticker=ticker, company_name=u.get("name"), issuer_cik=cik,
                               priority=5 if stage in ("EARLY_COMMERCIAL", "PRODUCTION_RAMP") else 3,
                               title=f"${ticker}: {prev[0]} -> {stage}", payload={"from": prev[0], "to": stage, "evidence": stage_reasons,
                                                                                 "market_cap": u.get("market_cap"), "bucket": u.get("bucket")},
                               source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
                               is_backfill=is_backfill, date_quality="first_seen")
            created += [eid] if eid else []
    elif not prev and stage:
        conn.execute(discovery_stage_history.insert().values(cik=cik, stage=stage, previous_stage=None, evidence=stage_reasons))
    # first major customer events (binding-ish, not hedged, not competitor/supplier)
    strong = sorted([r for r in rels if r["strength"] >= 0.6 and r["category"] in S.MAJOR_CATEGORIES], key=lambda r: r["filed"] or date.min)
    if strong:
        created += _first_event(conn, u, "FIRST_MAJOR_CUSTOMER", strong[0], is_backfill)
        f500 = [r for r in strong if r["category"] in S.FORTUNE500_CLASS]
        if f500:
            created += _first_event(conn, u, "FIRST_FORTUNE500_CUSTOMER", f500[0], is_backfill)
        for cat, et in FIRST_EVENTS.items():
            fr = [r for r in strong if r["category"] == cat and (cat != "US_GOVERNMENT" or r["counterparty"] == "DoD")]
            if fr:
                created += _first_event(conn, u, et, fr[0], is_backfill)
    # commercial inflection flags
    last_q = rev[-1][0] if rev else None
    for fl in met.get("flags", []):
        eid = create_event(conn, event_type=fl, dedupe_key=f"INFL:{fl}:{cik}:{last_q}", public_date=public, ticker=ticker,
                           company_name=u.get("name"), issuer_cik=cik, priority=2 if fl == "CASH_RUNWAY_RISK" else 4,
                           title=f"${ticker}: {fl.replace('_', ' ').lower()}", payload={"metrics": _jsonable(met), "period_end": last_q,
                                                                                         "market_cap": u.get("market_cap"), "bucket": u.get("bucket")},
                           source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
                           is_backfill=is_backfill, date_quality="first_seen")
        created += [eid] if eid else []
    # watchlist + discovery alert
    before = conn.execute(select(watchlist.c.category).where(watchlist.c.cik == cik)).scalar()
    target = auto_update(conn, cik, ticker, level=level, candidate=candidate, why=why, bucket=u.get("bucket"))
    if target == "HIGH_INTEREST" and before != "HIGH_INTEREST":
        eid = create_event(conn, event_type="EARLY_DISCOVERY", dedupe_key=f"DISC:{cik}:{public.isoformat()}", public_date=public,
                           ticker=ticker, company_name=u.get("name"), issuer_cik=cik, priority=6,
                           title=f"${ticker}: early discovery - {', '.join(themes[:2]) or 'n/a'} ({u.get('bucket')})",
                           payload={"why": why, "score": sc, "level": level, "stage": stage, "themes": themes,
                                    "market_cap": u.get("market_cap"), "bucket": u.get("bucket"), "risks": feats["risks"],
                                    "metrics": {k: met.get(k) for k in ("ttm_revenue", "yoy_latest", "cash", "runway_months")},
                                    "customers": vals["customers"][:5], "adv_dollar_30d": u.get("adv_dollar_30d")},
                           source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
                           is_backfill=is_backfill, date_quality="first_seen")
        created += [eid] if eid else []
    if target in ("DISCOVERY", "HIGH_INTEREST", "THESIS_WEAKENING", "MONITOR") or candidate:
        build_thesis(conn, cik, ticker, dc={**vals, "name": u.get("name")}, risks=risks)
    return {"level": level, "candidate": candidate, "events": created}


def _first_event(conn, u, et, r, is_backfill) -> list[int]:
    eid = create_event(conn, event_type=et, dedupe_key=f"FIRST:{et}:{u['cik']}", public_date=date.today(), ticker=u["ticker"],
                       company_name=u.get("name"), issuer_cik=u["cik"], priority=5,
                       title=f"${u['ticker']}: {et.replace('_', ' ').lower()} - {r['counterparty']} ({r['agreement'] or r['relation']})",
                       payload={"counterparty": r["counterparty"], "category": r["category"], "agreement": r["agreement"],
                                "relation": r["relation"], "sentence": r["sentence"][:500], "evidence_filed": r["filed"],
                                "market_cap": u.get("market_cap"), "bucket": u.get("bucket")},
                       source_url=r["url"], is_backfill=is_backfill, date_quality="first_seen")
    return [eid] if eid else []


def _jsonable(d):
    from app.db.store import jsonable
    return jsonable(d)


def evaluate_all(engine: Engine, *, is_backfill: bool) -> dict:
    with engine.connect() as conn:
        uni = universe_ciks(conn, lo=50e6, hi=1e15)
        interesting = {r[0] for r in conn.execute(select(discovery_evidence.c.cik).distinct()).all()}
        interesting |= {r[0] for r in conn.execute(select(insider_transactions.c.issuer_cik).distinct().where(and_(
            insider_transactions.c.category == "OPEN_MARKET_BUY", insider_transactions.c.filed_date >= date.today() - timedelta(days=90)))).all()}
        interesting |= {r[0] for r in conn.execute(select(watchlist.c.cik)).all()}
        from app.db.models import fundamentals_quarterly as fq
        interesting |= {r[0] for r in conn.execute(select(fq.c.cik).distinct().where(and_(
            fq.c.concept == "Revenue", fq.c.period_end >= date.today() - timedelta(days=200)))).all()}
    todo = [u for c, u in uni.items() if c in interesting]
    stats = {"evaluated": 0, "candidates": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "events": 0, "errors": 0}
    for u in todo:
        try:
            with engine.begin() as conn:
                r = evaluate(conn, u, is_backfill=is_backfill)
            stats["evaluated"] += 1
            stats[r["level"]] += 1
            stats["candidates"] += r["candidate"]
            stats["events"] += len(r["events"])
        except Exception:
            stats["errors"] += 1
            log.exception("evaluate %s failed", u.get("ticker"))
    return stats


def run_discovery(engine: Engine, *, full: bool) -> dict:
    """full=True: universe + fundamentals + text risks (weekly); always: evidence + evaluation."""
    name = "discovery_engine"
    with engine.begin() as conn:
        st = job_state(conn, name)
    first = not st.get("initialized")
    out: dict = {}
    if full or first:
        log.info("discovery: building universe snapshot (prices + liquidity for all listed companies)")
        out["universe"] = build_universe(engine)
        log.info("discovery: universe %s", out["universe"])
        with engine.connect() as conn:
            uni = universe_ciks(conn, lo=50e6, hi=1e15)
        out["fundamentals"] = load_fundamentals(engine, only_ciks=set(uni))
        log.info("discovery: fundamentals %s", out["fundamentals"].get("rows"))
        out["risks"] = R.scan_text_risks(engine, {c for c, u in uni.items() if (u["market_cap"] or 0) < 10e9})
        log.info("discovery: risks %s", out["risks"])
    # SEC submissions (S-3 / 424B / 8-K 3.01 / 3.02 ...) for sub-$1B companies -> financing risk
    from app.securitymaster.build import refresh_submissions
    with engine.connect() as conn:
        small = universe_ciks(conn, lo=50e6, hi=1e9)
    out["submissions"] = refresh_submissions(engine, list(small), max_n=800)
    with engine.connect() as conn:  # SIC sector for every universe company (scope rules), refreshed monthly
        everyone = universe_ciks(conn, lo=50e6, hi=1e15)
    out["submissions_all"] = refresh_submissions(engine, list(everyone), max_n=800)
    out["evidence"] = collect(engine, max_docs=1500 if first else 600)
    log.info("discovery: evidence %s", out["evidence"])
    out["evaluation"] = evaluate_all(engine, is_backfill=first)
    log.info("discovery: evaluation %s", out["evaluation"])
    with engine.begin() as conn:
        save_job_state(conn, name, {**st, "initialized": True, "last": _jsonable(out),
                                    "last_full": date.today().isoformat() if (full or first) else st.get("last_full")}, status="idle")
    return out
