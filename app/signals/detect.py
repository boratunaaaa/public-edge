"""Event construction from normalized rows: insider buys, clusters, multi-signal."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.engine import Connection

from app.db.models import events, insider_transactions
from app.db.store import create_event
from app.signals import priority as P
from app.signals.insider_type import classify as insider_classify, holdings_increase, owner_history

CLUSTER_WINDOWS = (7, 14, 30)
MULTI_WINDOW_DAYS = 30


def _money(v: float | None) -> str:
    v = v or 0
    if v >= 999.5e6:
        return f"${v / 1e9:.2f}B"
    if v >= 999.5e3:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}k"
    return f"${v:.0f}"


def role_label(is_ceo: bool, is_cfo: bool, is_director: bool, is_ten_pct: bool, title: str | None) -> str:
    if is_ceo and is_cfo:
        return "CEO/CFO"
    if is_ceo:
        return "CEO"
    if is_cfo:
        return "CFO"
    if title:
        return title[:40]
    if is_director:
        return "Director"
    if is_ten_pct:
        return "10% Owner"
    return "Insider"


# ---------------- insider open-market buys ----------------

def insider_buy_events(conn: Connection, accession: str, is_backfill: bool = False) -> list[int]:
    rows = conn.execute(select(insider_transactions).where(
        insider_transactions.c.accession == accession,
        insider_transactions.c.category == "OPEN_MARKET_BUY",
        insider_transactions.c.is_derivative.is_(False),
    )).mappings().all()
    by_owner: dict[str, list] = defaultdict(list)
    for r in rows:
        by_owner[r["owner_cik"] or r["owner_name"]].append(r)
    created = []
    for owner_key, txs in by_owner.items():
        r0 = txs[0]
        if not r0["ticker"]:
            continue
        value = float(sum(float(t["value"] or 0) for t in txs))
        shares = float(sum(float(t["shares"] or 0) for t in txs))
        if value <= 0:
            continue
        is_plan = any(t["is_10b5_1"] for t in txs)
        prio = P.insider_buy_priority(is_ceo=r0["is_ceo"], is_cfo=r0["is_cfo"], is_director=r0["is_director"],
                                      value=value, is_10b5_1=is_plan)
        tdates0 = sorted(t["trans_date"] for t in txs if t["trans_date"])
        itype = insider_classify(owner_history(conn, r0["owner_cik"], tdates0[0]), tdates0[0]) \
            if (r0["owner_cik"] and tdates0) else "unclassified"
        prio += {"opportunistic": 1, "routine": -1}.get(itype, 0)  # Cohen-Malloy-Pomorski (2012)
        inc = holdings_increase(shares, float(txs[-1]["shares_after"]) if txs[-1]["shares_after"] is not None else None)
        role = role_label(r0["is_ceo"], r0["is_cfo"], r0["is_director"], r0["is_ten_pct"], r0["officer_title"])
        tdates = tdates0
        payload = {
            "owner": r0["owner_name"], "owner_cik": r0["owner_cik"], "role": role, "officer_title": r0["officer_title"],
            "is_ceo": r0["is_ceo"], "is_cfo": r0["is_cfo"], "is_director": r0["is_director"], "is_ten_pct": r0["is_ten_pct"],
            "value": round(value, 2), "shares": shares, "avg_price": round(value / shares, 4) if shares else None,
            "shares_after": float(txs[-1]["shares_after"]) if txs[-1]["shares_after"] is not None else None,
            "n_transactions": len(txs), "transaction_dates": [str(d) for d in tdates], "is_10b5_1": is_plan,
            "insider_type": itype, "holdings_increase": inc,
            "flags": [f for f, ok in (("CEO_BUY", r0["is_ceo"]), ("CFO_BUY", r0["is_cfo"]),
                                      ("DIRECTOR_BUY", r0["is_director"]), ("GT_100K", value > 100_000),
                                      ("GT_500K", value > 500_000), ("GT_1M", value > 1_000_000)) if ok],
            "accession": accession,
        }
        eid = create_event(
            conn, event_type="INSIDER_OPEN_MARKET_BUY", dedupe_key=f"IOMB:{accession}:{owner_key}",
            public_date=r0["filed_date"], public_at=r0["accepted_at"], transaction_date=tdates[0] if tdates else None,
            ticker=r0["ticker"], company_name=r0["issuer_name"], issuer_cik=r0["issuer_cik"], priority=prio,
            title=f"{role} {r0['owner_name']} bought {_money(value)} of ${r0['ticker']}",
            payload=payload, source_url=r0["source_url"], is_backfill=is_backfill,
        )
        if eid:
            created.append(eid)
    return created


# ---------------- insider cluster buying ----------------

@dataclass
class Buy:
    filed_date: date
    owner: str
    owner_name: str
    value: float
    is_ceo: bool
    is_cfo: bool
    is_director: bool
    role: str
    source_url: str


@dataclass
class Cluster:
    window: int
    start: date
    end: date
    buyers: list[Buy]

    @property
    def n_buyers(self) -> int:
        return len({b.owner for b in self.buyers})


def find_clusters(buys: list[Buy], anchor: date, windows=CLUSTER_WINDOWS) -> list[Cluster]:
    """Clusters ending at `anchor` (a filing date). A window is reported only when it adds buyers
    compared with every smaller window, so one cluster is not reported three times."""
    out: list[Cluster] = []
    best = 1
    for w in windows:
        lo = anchor - timedelta(days=w - 1)
        inwin = [b for b in buys if lo <= b.filed_date <= anchor]
        n = len({b.owner for b in inwin})
        if n >= 2 and n > best:
            out.append(Cluster(w, min(b.filed_date for b in inwin), anchor, inwin))
            best = n
    return out


def cluster_events_for_issuer(conn: Connection, issuer_cik: str, anchor: date, is_backfill: bool = False) -> list[int]:
    lo = anchor - timedelta(days=max(CLUSTER_WINDOWS) - 1)
    rows = conn.execute(select(insider_transactions).where(
        insider_transactions.c.issuer_cik == issuer_cik, insider_transactions.c.category == "OPEN_MARKET_BUY",
        insider_transactions.c.is_derivative.is_(False), insider_transactions.c.is_10b5_1.is_(False),
        insider_transactions.c.filed_date >= lo, insider_transactions.c.filed_date <= anchor,
    )).mappings().all()
    if not rows:
        return []
    agg: dict[tuple, Buy] = {}
    ticker = next((r["ticker"] for r in rows if r["ticker"]), None)
    name = rows[0]["issuer_name"]
    for r in rows:
        key = (r["owner_cik"] or r["owner_name"], r["filed_date"])
        b = agg.get(key)
        v = float(r["value"] or 0)
        if b is None:
            agg[key] = Buy(r["filed_date"], r["owner_cik"] or r["owner_name"], r["owner_name"], v, r["is_ceo"], r["is_cfo"],
                           r["is_director"], role_label(r["is_ceo"], r["is_cfo"], r["is_director"], r["is_ten_pct"], r["officer_title"]),
                           r["source_url"])
        else:
            b.value += v
    created = []
    if not ticker:
        return created
    for c in find_clusters(list(agg.values()), anchor):
        per_owner: dict[str, dict] = {}
        for b in c.buyers:
            po = per_owner.setdefault(b.owner, {"name": b.owner_name, "role": b.role, "value": 0.0, "is_ceo": b.is_ceo,
                                                 "is_cfo": b.is_cfo, "is_director": b.is_director})
            po["value"] += b.value
        aggregate = sum(p["value"] for p in per_owner.values())
        ceo = any(p["is_ceo"] for p in per_owner.values())
        cfo = any(p["is_cfo"] for p in per_owner.values())
        directors = sum(1 for p in per_owner.values() if p["is_director"] and not (p["is_ceo"] or p["is_cfo"]))
        payload = {
            "window_days": c.window, "number_of_buyers": c.n_buyers, "aggregate_value": round(aggregate, 2),
            "CEO_present": ceo, "CFO_present": cfo, "directors_count": directors,
            "span_days": (c.end - c.start).days + 1,
            "buyers": sorted(per_owner.values(), key=lambda p: -p["value"]),
        }
        eid = create_event(
            conn, event_type="CLUSTER_INSIDER_BUY",
            dedupe_key=f"CLUSTER:{issuer_cik}:{c.window}:{c.start}:{c.n_buyers}",
            public_date=anchor, ticker=ticker, company_name=name, issuer_cik=issuer_cik,
            priority=P.cluster_priority(n_buyers=c.n_buyers, ceo=ceo, cfo=cfo, aggregate=aggregate),
            title=f"{c.n_buyers} insiders bought {_money(aggregate)} of ${ticker} within {c.window}d",
            payload=payload, source_url=c.buyers[-1].source_url, is_backfill=is_backfill,
        )
        if eid:
            created.append(eid)
    return created


# ---------------- multi signal ----------------

def signal_family(event_type: str, payload: dict) -> str | None:
    if event_type == "INSIDER_OPEN_MARKET_BUY":
        if payload.get("is_10b5_1"):
            return None
        if payload.get("is_ceo"):
            return "CEO_BUY"
        if payload.get("is_cfo"):
            return "CFO_BUY"
        return None
    if event_type == "CLUSTER_INSIDER_BUY":
        return "INSIDER_CLUSTER"
    if event_type == "ACTIVIST_13D":
        return "ACTIVIST_13D"
    if event_type == "GOV_CONTRACT" and (payload.get("value_to_mcap") or 0) >= 0.02:
        return "GOV_CONTRACT"
    if event_type in ("CONGRESS_BUY", "CONGRESS_CLUSTER_BUY"):
        return "CONGRESS_BUY"
    if event_type == "MATERIAL_NEWS" and payload.get("materiality") in ("HIGH", "CRITICAL"):
        positive = {"MAJOR_CUSTOMER", "NEW_CONTRACT", "GOVERNMENT_CONTRACT", "COMMERCIALIZATION", "PRODUCTION_START",
                    "CAPACITY_EXPANSION", "DESIGN_WIN", "FDA_APPROVAL", "GOVERNMENT_EQUITY", "GUIDANCE_RAISE",
                    "ACTIVIST", "M_AND_A"}
        return "MATERIAL_NEWS" if positive & set(payload.get("classes") or []) else None
    return None


# Independent evidence sources. CEO buy + insider cluster are the same evidence (the cluster
# already contains the CEO), so a multi-signal needs at least two DIFFERENT source groups.
FAMILY_GROUP = {"CEO_BUY": "INSIDER", "CFO_BUY": "INSIDER", "INSIDER_CLUSTER": "INSIDER",
                "ACTIVIST_13D": "ACTIVIST", "GOV_CONTRACT": "CONTRACT", "CONGRESS_BUY": "CONGRESS",
                "MATERIAL_NEWS": "NEWS"}


def multi_signal_check(conn: Connection, ticker: str | None, public_date: date, is_backfill: bool = False) -> int | None:
    if not ticker:
        return None
    lo = public_date - timedelta(days=MULTI_WINDOW_DAYS)
    rows = conn.execute(select(events.c.id, events.c.event_type, events.c.payload, events.c.public_date, events.c.priority,
                               events.c.company_name, events.c.issuer_cik, events.c.source_url)
                        .where(and_(events.c.ticker == ticker, events.c.public_date >= lo, events.c.public_date <= public_date,
                                    events.c.event_type != "MULTI_SIGNAL_EVENT",
                                    events.c.date_quality != "estimated"))  # inferred dates never combine
                        .order_by(events.c.public_date)).mappings().all()
    fam: dict[str, list] = defaultdict(list)
    for r in rows:
        f = signal_family(r["event_type"], r["payload"] or {})
        if f:
            fam[f].append(r)
    if len({FAMILY_GROUP[f] for f in fam}) < 2:
        return None
    families = sorted(fam)
    comp = [r for f in families for r in fam[f]]
    first = min(r["public_date"] for r in comp)
    features = {
        "families": families, "n_families": len(families), "source_groups": sorted({FAMILY_GROUP[f] for f in families}),
        "n_component_events": len(comp),
        "component_event_ids": [r["id"] for r in comp], "span_days": (public_date - first).days,
        "component_priorities": {f: max(r["priority"] for r in fam[f]) for f in families},
    }
    prio = min(10, max(r["priority"] for r in comp) + 2 * (len(features["source_groups"]) - 1))
    name = next((r["company_name"] for r in comp if r["company_name"]), None)
    return create_event(
        conn, event_type="MULTI_SIGNAL_EVENT", dedupe_key=f"MULTI:{ticker}:{'+'.join(families)}:{first}",
        public_date=public_date, ticker=ticker, company_name=name, issuer_cik=comp[0]["issuer_cik"], priority=prio,
        title=f"${ticker}: {' + '.join(families)} within {MULTI_WINDOW_DAYS}d", payload=features, features=features,
        source_url=comp[-1]["source_url"], is_backfill=is_backfill,
    )
