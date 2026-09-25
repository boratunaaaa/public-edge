"""Proposed paper trades: eligibility gate, position sizing, Turkish Telegram text, human approval.

Nothing here places an order anywhere. An APPROVED proposal becomes a virtual order in paper_orders,
filled later from stored market prices (app.paper.ledger). Proposals are never auto-approved.

Gate (every rule must pass; the failed rules are recorded):
  data quality not BLOCKED, verified price identity (same CIK), not a duplicate (same ticker within
  DUP_DAYS or already held), liquidity (30-day $ volume >= MIN_ADV and no HIGH LOW_LIQUIDITY),
  no severe financing risk (GOING_CONCERN / CASH_RUNWAY HIGH), and the signal family is not
  empirically UNDERPERFORMING / BLOCKED / unproven - unless research-only mode is on.
Sizing (paper risk limits, not advice): max 5% per position, 2% for microcaps, 1% with
HIGH_FINANCING_RISK; the suggestion is min(SUGGESTED_PCT, cap).
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, select, text
from sqlalchemy.engine import Connection, Engine

from app.db.models import discovery_risks, events, paper_positions, proposed_trades, universe_snapshots


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


MAX_POSITION_PCT = 0.05
MICROCAP_MAX_PCT = 0.02
FINANCING_RISK_MAX_PCT = 0.01
SUGGESTED_PCT = 0.03
MICROCAP_USD = 300e6
MIN_ADV = _f("PAPER_MIN_ADV_USD", 1_000_000)
DUP_DAYS = 30
PROPOSAL_TTL_HOURS = int(_f("PAPER_PROPOSAL_TTL_HOURS", 72))
STOP_PCT = 0.15
SEVERE = {"GOING_CONCERN", "CASH_RUNWAY"}
VERIFIED_BASES = ("current_ticker_same_cik", "historical_symbol_verified")


def research_mode() -> bool:
    return os.environ.get("PAPER_RESEARCH_MODE", "").strip().lower() in ("1", "true", "yes")


@dataclass
class GateResult:
    ok: bool
    failed: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    max_pct: float = 0.0
    suggested_pct: float = 0.0
    data_quality: str = "BLOCKED"
    risks: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)


def size_limits(market_cap: float | None, bucket: str | None, risk_names: set[str]) -> float:
    cap = MAX_POSITION_PCT
    if bucket == "MICRO" or (market_cap is not None and market_cap < MICROCAP_USD) or market_cap is None:
        cap = min(cap, MICROCAP_MAX_PCT)  # unknown size is treated as the risky case
    if "HIGH_FINANCING_RISK" in risk_names:
        cap = min(cap, FINANCING_RISK_MAX_PCT)
    return cap


def evaluate_gate(conn: Connection, ev: dict, *, family_ok: tuple[bool, str], price_basis: str | None,
                  now: datetime | None = None) -> GateResult:
    now = now or datetime.now(timezone.utc)
    g = GateResult(ok=False)
    cik, ticker = ev.get("issuer_cik"), ev.get("ticker")
    # 1. data quality
    if ev.get("date_quality") == "estimated" or ev.get("superseded"):
        g.failed.append("DATA_QUALITY_BLOCKED")
    else:
        g.passed.append("DATA_QUALITY")
    g.data_quality = "OK" if ev.get("date_quality") == "exact" else "DATE_ONLY"
    # 2. ticker / security identity
    if not ticker or price_basis not in VERIFIED_BASES:
        g.failed.append("UNRESOLVED_SECURITY_MAPPING")
        g.data_quality = "BLOCKED"
    else:
        g.passed.append("SECURITY_MAPPING")
    # 3. duplicate
    dup = conn.execute(select(proposed_trades.c.id).where(and_(
        proposed_trades.c.ticker == ticker, proposed_trades.c.proposal_created_at > now - timedelta(days=DUP_DAYS)))).first() \
        if ticker else None
    held = conn.execute(select(paper_positions.c.id).where(and_(
        paper_positions.c.ticker == ticker, paper_positions.c.status != "CLOSED"))).first() if ticker else None
    if dup or held:
        g.failed.append("DUPLICATE")
    else:
        g.passed.append("NOT_DUPLICATE")
    # 4. liquidity + size
    uni = conn.execute(select(universe_snapshots.c.market_cap, universe_snapshots.c.bucket, universe_snapshots.c.adv_dollar_30d)
                       .where(universe_snapshots.c.cik == cik).order_by(universe_snapshots.c.as_of.desc()).limit(1)).first() \
        if cik else None
    mcap, bucket, adv = (uni[0], uni[1], uni[2]) if uni else (None, None, None)
    risks = {r[0]: r[1] for r in conn.execute(select(discovery_risks.c.risk, discovery_risks.c.severity)
                                             .where(discovery_risks.c.cik == cik)).all()} if cik else {}
    g.risks = sorted(risks)
    g.info = {"market_cap": mcap, "bucket": bucket, "adv_dollar_30d": adv}
    if adv is None or adv < MIN_ADV or risks.get("LOW_LIQUIDITY") == "HIGH":
        g.failed.append("LOW_LIQUIDITY")
    else:
        g.passed.append("LIQUIDITY")
    # 5. severe financing risk
    if any(risks.get(r) == "HIGH" for r in SEVERE):
        g.failed.append("SEVERE_FINANCING_RISK")
    else:
        g.passed.append("NO_SEVERE_FINANCING_RISK")
    # 6. direction: a long-only paper book never buys on a NEGATIVE signal (e.g. a new 10-K risk)
    direction = (ev.get("payload") or {}).get("direction")
    if direction in ("NEGATIVE", "MIXED"):
        g.failed.append("NOT_A_LONG_SIGNAL")
    # 7. signal family evidence
    allowed, why = family_ok
    if allowed and why != "FAMILY_OUTPERFORMING":
        why += ":RESEARCH_MODE_OVERRIDE"  # allowed only because research mode is on - never shown as "passed"
    (g.passed if allowed else g.failed).append(why)
    if not allowed and why in ("FAMILY_UNDERPERFORMING",):
        g.info["family_warning"] = why
    g.max_pct = size_limits(mcap, bucket, set(risks))
    g.suggested_pct = min(SUGGESTED_PCT, g.max_pct)
    g.ok = not g.failed
    return g


def _code() -> str:
    return "PE" + secrets.token_hex(3).upper()


def reference_price(conn: Connection, symbol: str, on: date) -> tuple[date, float] | None:
    from app.marketdata.provider import ensure_prices
    bars = [b for b in ensure_prices(conn, symbol, on - timedelta(days=10), on) if b.close]
    return (bars[-1].date, bars[-1].close) if bars else None


RISK_TR = {"HIGH_FINANCING_RISK": "yakın zamanda yeni hisse/borç ile para toplama ihtimali yüksek",
           "DILUTION": "hisse sayısı artıyor (sulanma)", "ATM_OFFERING": "piyasadan sürekli hisse satış programı var",
           "CONVERTIBLE_DEBT": "hisseye dönüşebilen borç var", "WARRANTS": "hisseye dönüşebilen varantlar var",
           "MATERIAL_WEAKNESS": "muhasebe kontrollerinde zayıflık bildirildi", "REVERSE_SPLIT": "hisse birleştirme yapıldı",
           "MIN_BID_DEFICIENCY": "borsanın minimum fiyat kuralını karşılamıyor", "HIGH_SBC": "yüksek hisse bazlı ücret"}
FAMILY_TR = {"INSIDER_BUY": "yönetici hisse alımı", "INSIDER_CLUSTER": "birden çok yöneticinin alımı", "13D": "aktivist yatırımcı",
             "ACTIVIST_CROWDING": "birden çok aktivist yatırımcı", "HOUSE_BUY": "Kongre üyesi alımı",
             "GOV_CONTRACT": "devlet sözleşmesi", "MULTI_SIGNAL": "birden çok sinyal", "10K_CHANGE": "yıllık rapor değişimi",
             "NEWS": "önemli haber", "DISCOVERY": "erken keşif"}


TENK_TR = {"RISK_DECREASE": "risk bölümü belirgin şekilde kısaldı", "LIQUIDITY_IMPROVEMENT": "nakit durumu dili iyileşti",
           "REMOVED_GOING_CONCERN": "'faaliyetin sürekliliği' şüphesi rapordan kalktı",
           "NEW_CAPACITY_EXPANSION": "üretim kapasitesi artışı anlatılıyor", "NEW_COMMERCIALIZATION_LANGUAGE": "ticari satışa geçiş dili eklendi",
           "NEW_MAJOR_CUSTOMER": "büyük müşteri/sözleşme eklendi"}


def thesis_tr(ev: dict) -> str:
    p = ev.get("payload") or {}
    if ev["event_type"] == "TENK_CHANGE":
        parts = [TENK_TR[t] for t in p.get("change_types", []) if t in TENK_TR]
        return "Yıllık raporda (10-K) önceki yıla göre: " + ("; ".join(parts) or "önemli değişiklik")
    from app.notifications.format import one_line
    return one_line(ev)


def create_proposal(engine: Engine, event_id: int, *, research_only: bool = False, now: datetime | None = None) -> dict:
    """Run the gate for one event and store a PENDING proposal when it passes."""
    from app.paper.alpha import EVENT_FAMILY, proposal_allowed
    from app.securitymaster.provider import get_security_master
    now = now or datetime.now(timezone.utc)
    with engine.begin() as conn:
        ev = conn.execute(select(events).where(events.c.id == event_id)).mappings().first()
        if ev is None:
            return {"created": False, "failed": ["NO_SUCH_EVENT"]}
        ev = dict(ev)
        fam = EVENT_FAMILY.get(ev["event_type"], ev["event_type"])
        fam_ok = proposal_allowed(conn, ev["event_type"], research_mode() or research_only)
        sym, basis, _ = get_security_master(conn).price_symbol(ev.get("issuer_cik"), now.date(), ev.get("ticker"))
        g = evaluate_gate(conn, ev, family_ok=fam_ok, price_basis=basis, now=now)
        ref = reference_price(conn, sym, now.date()) if (sym and g.ok) else None
        if ref is None and g.ok:
            g.failed.append("NO_REFERENCE_PRICE")
            g.ok = False
        if not g.ok:
            return {"created": False, "failed": g.failed, "passed": g.passed}
        risks_tr = [RISK_TR.get(r, r) for r in g.risks if r in RISK_TR]
        if g.info.get("bucket") == "MICRO" or (g.info.get("market_cap") or 0) < MICROCAP_USD:
            risks_tr.append("çok küçük şirket: fiyat sert oynayabilir")
        if fam_ok[1] != "FAMILY_OUTPERFORMING":
            risks_tr.append("bu sinyal türünün geçmiş başarısı henüz kanıtlanmadı (araştırma modu)")
        code = _code()
        pid = conn.execute(proposed_trades.insert().values(
            code=code, ticker=sym, cik=ev.get("issuer_cik"), event_id=event_id, signal_family=fam, event_type=ev["event_type"],
            proposal_created_at=now, reference_date=ref[0], entry_reference_price=ref[1],
            suggested_position_pct=g.suggested_pct, max_position_pct=g.max_pct, stop_reference=round(ref[1] * (1 - STOP_PCT), 4),
            thesis=thesis_tr(ev)[:500], risks=risks_tr, reasons=[FAMILY_TR.get(fam, fam)], data_quality=g.data_quality,
            gate={"passed": g.passed, "failed": g.failed, "family_check": fam_ok[1], **g.info}, approval_status="PENDING",
            expires_at=now + timedelta(hours=PROPOSAL_TTL_HOURS), research_only=fam_ok[1] != "FAMILY_OUTPERFORMING",
        ).returning(proposed_trades.c.id)).scalar_one()
    return {"created": True, "id": pid, "code": code}


def scan_new_events(engine: Engine, days: int = 2) -> dict:
    """Automatic proposals from fresh live events. Families that are UNDERPERFORMING, BLOCKED or not yet
    proven are skipped unless PAPER_RESEARCH_MODE is on."""
    from app.paper.alpha import EVENT_FAMILY
    with engine.connect() as conn:
        ids = [r[0] for r in conn.execute(select(events.c.id).where(and_(
            events.c.is_backfill.is_(False), events.c.superseded.is_(False), events.c.event_type.in_(list(EVENT_FAMILY)),
            events.c.public_date >= date.today() - timedelta(days=days),
            ~select(proposed_trades.c.id).where(proposed_trades.c.event_id == events.c.id).exists())).order_by(events.c.id)).all()]
    out = {"checked": len(ids), "created": 0, "rejected": {}}
    for eid in ids:
        r = create_proposal(engine, eid)
        if r.get("created"):
            out["created"] += 1
        else:
            for f in r.get("failed", []):
                out["rejected"][f] = out["rejected"].get(f, 0) + 1
    return out


def expire(engine: Engine, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    with engine.begin() as conn:
        return conn.execute(proposed_trades.update().where(and_(
            proposed_trades.c.approval_status == "PENDING", proposed_trades.c.expires_at <= now))
            .values(approval_status="EXPIRED", decided_at=now, decided_by="system:expiry")).rowcount


def decide(engine: Engine, code: str, approve: bool, by: str, now: datetime | None = None) -> dict:
    """Human decision. Only a PENDING, unexpired proposal can change state; approval creates paper
    BUY orders (one per active strategy book) that fill at the NEXT session - never a real order."""
    from app.paper.ledger import queue_buy
    now = now or datetime.now(timezone.utc)
    with engine.begin() as conn:
        p = conn.execute(select(proposed_trades).where(proposed_trades.c.code == code.strip().upper())
                         .with_for_update()).mappings().first()
        if p is None:
            return {"ok": False, "error": "NOT_FOUND"}
        if p["approval_status"] != "PENDING":
            return {"ok": False, "error": f"ALREADY_{p['approval_status']}"}
        if p["expires_at"] <= now:
            conn.execute(proposed_trades.update().where(proposed_trades.c.id == p["id"]).values(
                approval_status="EXPIRED", decided_at=now, decided_by="system:expiry"))
            return {"ok": False, "error": "EXPIRED"}
        status = "APPROVED" if approve else "REJECTED"
        conn.execute(proposed_trades.update().where(proposed_trades.c.id == p["id"]).values(
            approval_status=status, decided_at=now, decided_by=by[:40]))
        orders = queue_buy(conn, dict(p), now) if approve else []
    return {"ok": True, "status": status, "orders": orders}


def pct_txt(x: float) -> str:
    return f"%{x * 100:.1f}".replace(".0", "").replace(".", ",")


def format_proposal(p: dict, dashboard_url: str | None = None) -> str:
    risks = p.get("risks") or []
    lines = ["🧪 <b>PUBLIC EDGE – SANAL İŞLEM ADAYI</b>", "",
             f"Hisse: <b>{p['ticker']}</b>",
             f"Sinyal: {', '.join(p.get('reasons') or [])}",
             f"Ne oldu: {p.get('thesis', '')}",
             f"Veri kalitesi: {'iyi' if p.get('data_quality') == 'OK' else 'yalnız tarih bazlı' if p.get('data_quality') == 'DATE_ONLY' else p.get('data_quality')}",
             f"Önerilen büyüklük: sanal portföyün {pct_txt(p['suggested_position_pct'])} (en fazla {pct_txt(p['max_position_pct'])})",
             f"Referans fiyat: {p['entry_reference_price']:.2f} $ ({p['reference_date']:%d.%m.%Y} kapanış)",
             f"Zarar-kes referansı: {p['stop_reference']:.2f} $"]
    if risks:
        lines += ["", "⚠️ Riskler:"] + [f"• {r}" for r in risks[:5]]
    lines += ["", "Durum: <b>ONAY BEKLİYOR</b>", f"Onay kodu: <code>{p['code']}</code>"]
    if dashboard_url:
        lines.append(f"Onay/ret: {dashboard_url}")
    lines += ["", "Bu sanal işlem önerisidir."]
    return "\n".join(lines)


def notify_pending(engine: Engine) -> int:
    from app.notifications import telegram
    from app.notifications.dispatch import deliver
    if not telegram.configured():
        return 0
    url = os.environ.get("PAPER_DASHBOARD_URL") or None
    with engine.connect() as conn:
        rows = conn.execute(select(proposed_trades).where(and_(
            proposed_trades.c.approval_status == "PENDING", proposed_trades.c.notified.is_(False)))).mappings().all()
    n = 0
    for p in rows:
        r = deliver(engine, f"proposal:{p['id']}", "proposal", format_proposal(dict(p), url))
        if r is not False:
            with engine.begin() as conn:
                conn.execute(proposed_trades.update().where(proposed_trades.c.id == p["id"]).values(notified=True))
            n += r is True
    return n


def pending_summary(conn: Connection) -> dict:
    return {r[0]: r[1] for r in conn.execute(text("SELECT approval_status, count(*) FROM proposed_trades GROUP BY 1")).all()}
