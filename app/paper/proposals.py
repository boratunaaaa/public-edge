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
VERIFIED_BASES = ("current_ticker_same_cik", "historical_symbol_verified", "vendor_trusted", "vendor_validated")


def research_mode() -> bool:
    return os.environ.get("PAPER_RESEARCH_MODE", "").strip().lower() in ("1", "true", "yes")


def research_auto_approve() -> bool:
    """Virtual-money RESEARCH books only: research proposals are approved by the system so forward evidence is
    collected unattended. MAIN books and anything real-money always need a human decision."""
    return research_mode() and os.environ.get("PAPER_RESEARCH_AUTO_APPROVE", "1").strip().lower() in ("1", "true", "yes")


def auto_approve_research(engine: Engine, now: datetime | None = None) -> int:
    if not research_auto_approve():
        return 0
    with engine.connect() as conn:
        codes = conn.execute(select(proposed_trades.c.code).where(and_(
            proposed_trades.c.approval_status == "PENDING", proposed_trades.c.research_only.is_(True)))).scalars().all()
    n = 0
    for code in codes:
        n += bool(decide(engine, code, True, by="system:research_auto", now=now).get("ok"))
    return n


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


ADV_SESSIONS = 30  # the same 30-day mean $ volume build_universe stores (app.discovery.sec_data.adv_dollar_30d)


def adv_dollar_prior_sessions(conn: Connection, symbol: str | None, on: date) -> float | None:
    """30-session mean close x volume of sessions BEFORE `on` (point in time: the decision day's own bar is never
    used). The fallback when universe_snapshots has no value - build_universe fills it for MICRO / EARLY_SMALL only,
    which made every larger company fail LOW_LIQUIDITY purely because of its market-cap class."""
    if not symbol:
        return None
    from app.marketdata.provider import ensure_prices
    try:
        bars = ensure_prices(conn, symbol, on - timedelta(days=60), on)
    except Exception:  # noqa: BLE001 - unknown liquidity stays unknown (-> LOW_LIQUIDITY), never guessed
        return None
    pairs = [(b.close, b.volume) for b in bars if b.date < on and b.close and b.volume is not None][-ADV_SESSIONS:]
    return (sum(c * v for c, v in pairs) / len(pairs)) if len(pairs) >= 5 else None


def evaluate_gate(conn: Connection, ev: dict, *, family_ok: tuple[bool, str], price_basis: str | None,
                  now: datetime | None = None, price_symbol: str | None = None) -> GateResult:
    now = now or datetime.now(timezone.utc)
    g = GateResult(ok=False)
    cik, ticker = ev.get("issuer_cik"), ev.get("ticker")
    # 1. data quality
    if ev.get("date_quality") == "estimated" or ev.get("superseded"):
        g.failed.append("DATA_QUALITY_BLOCKED")
    else:
        g.passed.append("DATA_QUALITY")
    g.data_quality = "OK" if ev.get("date_quality") == "exact" else "DATE_ONLY"
    # 2. ticker / security identity (V2: an event flagged needs_review - e.g. its ticker belonged to another issuer at
    # the event date - never feeds a proposal)
    from app.core.event_policy import source_block_reason
    if not ticker or price_basis not in VERIFIED_BASES or source_block_reason(ev):
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
    adv_source = "universe_snapshot" if adv is not None else None
    if adv is None:  # independent of the market-cap class: measure it from prior sessions of the resolved series
        adv = adv_dollar_prior_sessions(conn, price_symbol or ticker, now.date())
        adv_source = "prior_sessions_30d" if adv is not None else "unknown"
    risks = {r[0]: r[1] for r in conn.execute(select(discovery_risks.c.risk, discovery_risks.c.severity)
                                             .where(discovery_risks.c.cik == cik)).all()} if cik else {}
    g.risks = sorted(risks)
    g.info = {"market_cap": mcap, "bucket": bucket, "adv_dollar_30d": adv, "adv_source": adv_source}
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
    # 7. validated 10-K risk: hard veto, the same policy as the research score vetoes (app.core.tenk_policy); only
    # changes public strictly before this decision count
    from app.core.tenk_policy import tenk_risk
    tk = tenk_risk(conn, cik, now)
    if tk.vetoes:
        g.failed.extend(tk.vetoes)
        g.info["tenk_events"] = list(tk.event_ids)
    else:
        g.passed.append("NO_VALIDATED_10K_RISK")
    # 8. signal family evidence
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
        g = evaluate_gate(conn, ev, family_ok=fam_ok, price_basis=basis, now=now, price_symbol=sym)
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
            signal_time=ev.get("available_at") or ev.get("public_at"),
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
    out = {"checked": len(ids), "created": 0, "rejected": {}, "rejected_ids": {}}
    try:  # RESEARCH-ONLY: proposals whose score write failed earlier are no longer rescanned - score them now
        from app.scoring.engine import retry_missing
        out["scores_retried"] = retry_missing(engine, list(EVENT_FAMILY), datetime.now(timezone.utc) - timedelta(days=days + 30))
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("research score retry failed")
    for eid in ids:
        at = datetime.now(timezone.utc)  # ONE decision moment per event: the gate and the research score both use it
        r = create_proposal(engine, eid, now=at)
        if r.get("created"):
            out["created"] += 1
        else:
            for f in r.get("failed", []):
                out["rejected"][f] = out["rejected"].get(f, 0) + 1
                out["rejected_ids"].setdefault(f, []).append(eid)
        _record_score(engine, eid, r.get("failed", []), out, now=at)
    return out


def _record_score(engine: Engine, event_id: int, vetoes: list[str], out: dict, now: datetime | None = None) -> None:
    """RESEARCH-ONLY score record (app.scoring.engine), AFTER the gate decided: it never changes the gate, the size or
    the approval, and a scoring failure never stops a proposal."""
    import logging
    try:
        from app.scoring.engine import record
        record(engine, event_id, vetoes, now=now)
        out["scored"] = out.get("scored", 0) + 1
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("research score of event %s failed", event_id)
        out["score_errors"] = out.get("score_errors", 0) + 1


# paper_orders.note / decided_by wording per source_block_reason
SOURCE_NOTE = {"SUPERSEDED": "source event superseded", "IDENTITY_UNSAFE": "source event identity unsafe",
               "NEEDS_REVIEW": "source event under review", "MISSING": "source event missing"}


def source_block(conn: Connection, event_id: int | None, lock: bool = False, exclusive: bool = False) -> str | None:
    """Why the signal a proposal was built on no longer stands (event_policy.source_block_reason: superseded by a
    reclassification / late predecessor, identity unsafe, under review), None while it does. lock=True keeps the
    event row locked (FOR SHARE, or FOR UPDATE with exclusive=True) until the caller's transaction ends, so a
    concurrent supersession / identity repair waits for the fill or approval (and is then visible to every later
    one) instead of racing it."""
    from app.core.event_policy import SOURCE_COLUMNS, source_block_reason
    if event_id is None:
        return None
    q = select(*SOURCE_COLUMNS).where(events.c.id == event_id)
    if lock:
        q = q.with_for_update(read=not exclusive)
    r = conn.execute(q).mappings().first()
    return source_block_reason(r)


def source_valid(conn: Connection, event_id: int | None, lock: bool = False) -> bool:
    return source_block(conn, event_id, lock) is None


def expire(engine: Engine, now: datetime | None = None) -> int:
    """Time-outs, PENDING proposals whose issuer carries a validated 10-K veto (app.core.tenk_policy), plus PENDING
    proposals and unfilled paper BUY orders whose source event no longer stands (source_block: superseded, identity
    unsafe or under review). The affected event rows are locked (FOR SHARE)
    first, so the decision and the cancellation see one state. SELL orders are never touched here."""
    from app.core.event_policy import SOURCE_COLUMNS, source_block_reason, source_blocked_clause
    from app.db.models import paper_orders
    now = now or datetime.now(timezone.utc)
    with engine.begin() as conn:
        n = conn.execute(proposed_trades.update().where(and_(
            proposed_trades.c.approval_status == "PENDING", proposed_trades.c.expires_at <= now))
            .values(approval_status="EXPIRED", decided_at=now, decided_by="system:expiry")).rowcount
        open_ids = select(proposed_trades.c.event_id).where(proposed_trades.c.approval_status == "PENDING").union(
            select(proposed_trades.c.event_id).join(paper_orders, paper_orders.c.proposal_id == proposed_trades.c.id)
            .where(and_(paper_orders.c.status == "PENDING", paper_orders.c.side == "BUY")))
        rows = conn.execute(select(events.c.id, *SOURCE_COLUMNS).where(and_(
            events.c.id.in_(open_ids), source_blocked_clause())).order_by(events.c.id)
            .with_for_update(read=True)).mappings().all()
        # PENDING proposals whose issuer now carries a validated 10-K veto expire (the approval re-checks it under the
        # issuer lock anyway; this sweep only keeps the queue honest). BUY orders are left to fill_orders, which checks
        # the veto at each order's own session open (point in time).
        from app.core.tenk_policy import tenk_risk
        for pid, cik in conn.execute(select(proposed_trades.c.id, proposed_trades.c.cik).where(
                proposed_trades.c.approval_status == "PENDING").order_by(proposed_trades.c.id)).all():
            tk = tenk_risk(conn, cik, now)
            if tk.vetoes:
                n += conn.execute(proposed_trades.update().where(and_(
                    proposed_trades.c.id == pid, proposed_trades.c.approval_status == "PENDING")).values(
                    approval_status="EXPIRED", decided_at=now, decided_by=f"system:{tk.vetoes[0].lower()}"[:40])).rowcount
        by: dict[str, list[int]] = {}
        for r in rows:
            by.setdefault(source_block_reason(r), []).append(r["id"])
        for reason, ids in sorted(by.items()):
            n += conn.execute(proposed_trades.update().where(and_(
                proposed_trades.c.approval_status == "PENDING", proposed_trades.c.event_id.in_(ids)))
                .values(approval_status="EXPIRED", decided_at=now, decided_by=f"system:source_{reason.lower()}")).rowcount
            conn.execute(paper_orders.update().where(and_(
                paper_orders.c.status == "PENDING", paper_orders.c.side == "BUY",
                paper_orders.c.proposal_id.in_(select(proposed_trades.c.id).where(proposed_trades.c.event_id.in_(ids)))))
                .values(status="CANCELLED", note=SOURCE_NOTE[reason]))
        return n


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
        if approve:
            # lock order: proposal row -> issuer 10-K lock -> source event row. A TENK_CHANGE writer takes the issuer
            # lock before touching any event row, so taking it here BEFORE the event lock leaves no cycle.
            from app.core.tenk_policy import lock_issuer
            if p["cik"]:
                lock_issuer(conn, p["cik"], exclusive=False)
            # re-validate the source signal inside the same transaction (row lock on the event): superseded,
            # identity unsafe or under review since the proposal was created -> the proposal expires
            why = source_block(conn, p["event_id"], lock=True, exclusive=True)
            if why:
                conn.execute(proposed_trades.update().where(proposed_trades.c.id == p["id"]).values(
                    approval_status="EXPIRED", decided_at=now, decided_by=f"system:source_{why.lower()}"))
                return {"ok": False, "error": f"SOURCE_{why}"}
            # the validated 10-K hard veto, re-checked at approval (app.core.tenk_policy): a change public strictly
            # before this decision - e.g. after the proposal was created - expires the proposal. The issuer's shared
            # lock is held until this transaction commits, so no 10-K write can interleave.
            from app.core.tenk_policy import tenk_risk
            tk = tenk_risk(conn, p["cik"], now, lock=True)
            if tk.vetoes:
                conn.execute(proposed_trades.update().where(proposed_trades.c.id == p["id"]).values(
                    approval_status="EXPIRED", decided_at=now, decided_by=f"system:{tk.vetoes[0].lower()}"[:40]))
                return {"ok": False, "error": "TENK_VETO", "vetoes": list(tk.vetoes)}
        status = "APPROVED" if approve else "REJECTED"
        from app.marketdata.calendar import NY, first_tradable_session
        elig = datetime.combine(first_tradable_session(now.date(), now), datetime.min.time().replace(hour=9, minute=30), NY) \
            if approve else None
        conn.execute(proposed_trades.update().where(proposed_trades.c.id == p["id"]).values(
            approval_status=status, decided_at=now, decided_by=by[:40], eligible_market_at=elig))
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
            proposed_trades.c.approval_status == "PENDING", proposed_trades.c.notified.is_(False),
            # research proposals handled by the system are summarised in the daily PAPER RESEARCH report, not pinged
            ~proposed_trades.c.research_only if research_auto_approve() else proposed_trades.c.research_only.isnot(None))
        )).mappings().all()
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
