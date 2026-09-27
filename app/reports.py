"""Daily intelligence (pre-market) and weekly discovery reports for Telegram."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, desc, select
from sqlalchemy.engine import Engine

from app.db.models import discovery_companies, discovery_risks, events, thesis_changes, watchlist, watchlist_history
from app.notifications.format import THEME_TR, e, money, one_line


TEZ = {"THESIS_STRENGTHENING": "olumlu yönde güçlendi", "THESIS_WEAKENING": "zayıfladı", "THESIS_INVALIDATION": "ciddi bozuldu"}
WL_TR = {"DISCOVERY": "keşif listesi", "HIGH_INTEREST": "yakın takip", "MONITOR": "izleme", "THESIS_WEAKENING": "dikkat", "ARCHIVED": "arşiv"}
STAGE_SHORT = {"RESEARCH": "araştırma", "PRE-COMMERCIAL": "satış öncesi", "PILOT": "deneme üretimi", "CUSTOMER_QUALIFICATION": "müşteri testi",
               "EARLY_COMMERCIAL": "satışa yeni başladı", "PRODUCTION_RAMP": "üretimi artırıyor", "MASS_PRODUCTION": "seri üretim", "SCALED": "büyük ölçek"}
RISK_SHORT = {"GOING_CONCERN": "iflas riski uyarısı", "DILUTION": "hisse sulanması", "LOW_LIQUIDITY": "az işlem görüyor",
              "MIN_BID_DEFICIENCY": "borsa uyarısı", "NASDAQ_MIN_BID_RISK": "1$ altı fiyat", "CASH_RUNWAY": "nakit azalıyor",
              "FREQUENT_CAPITAL_RAISES": "sık para topluyor", "REVERSE_SPLIT": "hisse birleştirme", "LATE_FILING": "geç rapor"}


def _ev(conn, types: list[str], since: datetime, limit: int, min_priority: int = 0, include_backfill: bool = False):
    q = select(events.c.title, events.c.ticker, events.c.priority, events.c.event_type, events.c.payload,
               events.c.company_name, events.c.public_date).where(and_(
        events.c.event_type.in_(types), events.c.created_at >= since, events.c.priority >= min_priority,
        *([] if include_backfill else [events.c.is_backfill.is_(False)])))
    return conn.execute(q.order_by(desc(events.c.priority), desc(events.c.created_at)).limit(limit)).mappings().all()


def _section(title: str, rows: list[str]) -> list[str]:
    return [f"<b>{e(title)}</b>"] + [f"• {e(r)}" for r in rows] + [""] if rows else []


def daily_report(engine: Engine, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    with engine.connect() as conn:
        disc = _ev(conn, ["EARLY_DISCOVERY", "STAGE_TRANSITION", "FIRST_MAJOR_CUSTOMER", "FIRST_HYPERSCALER",
                          "FIRST_AUTOMOTIVE_OEM", "FIRST_DOD_CONTRACT", "FIRST_FORTUNE500_CUSTOMER"], since, 5)
        news = _ev(conn, ["MATERIAL_NEWS"], since, 5, min_priority=5)
        clusters = _ev(conn, ["CLUSTER_INSIDER_BUY"], since, 5)
        gov = _ev(conn, ["GOV_CONTRACT"], since, 5)
        gov_news = [r for r in _ev(conn, ["MATERIAL_NEWS"], since, 20)
                    if "GOVERNMENT_CONTRACT" in ((r["payload"] or {}).get("classes") or [])][:max(0, 5 - len(gov))]
        cong = _ev(conn, ["CONGRESS_BUY", "CONGRESS_CLUSTER_BUY"], since, 5)
        tch = conn.execute(select(thesis_changes.c.ticker, thesis_changes.c.change, thesis_changes.c.explanation).where(and_(
            thesis_changes.c.created_at >= since, thesis_changes.c.change != "NEUTRAL")).order_by(desc(thesis_changes.c.created_at)).limit(5)).all()
        wl = {r[0] for r in conn.execute(select(watchlist.c.ticker).where(watchlist.c.category != "ARCHIVED")).all() if r[0]}
        wl_dev = conn.execute(select(events).where(and_(
            events.c.ticker.in_(wl), events.c.created_at >= since)).order_by(desc(events.c.priority)).limit(10)).mappings().all() if wl else []
    ol = lambda rows: [one_line(dict(r)) for r in rows]
    parts = _section("🔬 Yeni keşfedilen şirketler", ol(disc)) + \
        _section("📰 Önemli şirket haberleri", ol(news)) + \
        _section("🟢 Birden fazla yöneticinin hisse aldığı şirketler", ol(clusters)) + \
        _section("🏛 Devlet sözleşmeleri", ol(gov) + ol(gov_news)) + \
        _section("🏛 Temsilciler Meclisi alımları (Senato verisi yok)", ol(cong)) + \
        _section("🧭 Takip ettiğimiz şirketlerde görüş değişikliği",
                 [f"{t}: {TEZ.get(c, c)} — {x[:90]}" for t, c, x in tch]) + \
        _section("👀 Takip listesindeki şirketlerde yeni gelişmeler", ol(wl_dev))
    from app.reports_v2 import morning_sections
    with engine.connect() as conn:
        more = morning_sections(conn, now)
    head = f"🗞 <b>PUBLIC EDGE MORNING BRIEF</b> — {now:%d.%m.%Y}\n"
    us = parts or ["Son 24 saatte kayda değer bir gelişme olmadı.", ""]
    return head + "\n🇺🇸 <b>ABD</b>\n" + "\n".join(us + more).rstrip()


def alert_scorecard(conn, now: datetime) -> list[str]:
    """How did the alerts we actually SENT do? (30-day return vs S&P 500, alerts 30-120 days old)."""
    from sqlalchemy import text as _t
    rows = conn.execute(_t("""
        SELECT e.event_type, r.excess_ret FROM notifications n JOIN events e ON e.id = n.event_id
        LEFT JOIN event_returns r ON r.event_id = e.id AND r.horizon_days = 30
        WHERE n.kind = 'event' AND n.status = 'sent' AND n.sent_at BETWEEN :a AND :b"""),
        {"a": now - timedelta(days=120), "b": now - timedelta(days=30)}).all()
    if not rows:
        return ["Henüz sonucu ölçülebilecek kadar eski bildirim yok (bildirimden en az 30 gün geçmesi gerekiyor)."]
    measured = [x for _, x in rows if x is not None]
    wins = sum(1 for x in measured if x > 0)
    unknown = len(rows) - len(measured)
    # counts only - an unmeasured alert is reported, never silently dropped from the tally
    return [f"Son 1-4 ayda gönderilen {len(rows)} bildirim: {wins} tanesinde hisse 1 ay içinde borsa ortalamasını geçti, "
            f"{len(measured) - wins} tanesinde geçemedi, {unknown} tanesinin sonucu ölçülemedi (fiyat verisi yok)."]


def weekly_report(engine: Engine, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    with engine.connect() as conn:
        new = conn.execute(select(discovery_companies.c.ticker, discovery_companies.c.bucket, discovery_companies.c.themes,
                                  discovery_companies.c.stage).where(discovery_companies.c.first_discovered_at >= since)
                           .order_by(desc(discovery_companies.c.interest_score)).limit(10)).all()
        tch = conn.execute(select(thesis_changes.c.ticker, thesis_changes.c.change, thesis_changes.c.explanation).where(and_(
            thesis_changes.c.created_at >= since, thesis_changes.c.change != "NEUTRAL")).limit(10)).all()
        milestones = _ev(conn, ["STAGE_TRANSITION", "FIRST_MAJOR_CUSTOMER", "FIRST_HYPERSCALER", "FIRST_AUTOMOTIVE_OEM",
                                "FIRST_DOD_CONTRACT", "FIRST_FORTUNE500_CUSTOMER", "REVENUE_ACCELERATION", "BACKLOG_BREAKOUT"],
                         since, 10, include_backfill=True)
        insider = _ev(conn, ["CLUSTER_INSIDER_BUY", "INSIDER_OPEN_MARKET_BUY"], since, 8, min_priority=5)
        gov = _ev(conn, ["GOV_CONTRACT"], since, 8)
        big = sorted(gov, key=lambda r: -((r["payload"] or {}).get("value_to_mcap") or 0))[:5]
        risky = conn.execute(select(discovery_companies.c.ticker, discovery_companies.c.market_cap).where(and_(
            discovery_companies.c.bucket == "MICRO", discovery_companies.c.interest_level != "LOW")).limit(30)).all()
        risky_rows = []
        for t, mc in risky:
            n = conn.execute(select(discovery_risks.c.risk).where(and_(discovery_risks.c.cik == select(discovery_companies.c.cik).where(
                discovery_companies.c.ticker == t).scalar_subquery(), discovery_risks.c.severity == "HIGH"))).all()
            if n:
                risky_rows.append(f"{t} ({money(mc)}): " + ", ".join(RISK_SHORT.get(r[0], r[0]) for r in n[:3]))
        card = alert_scorecard(conn, now)
        moves = conn.execute(select(watchlist_history.c.cik, watchlist_history.c.from_category, watchlist_history.c.to_category,
                                    watchlist_history.c.reason, watchlist.c.ticker)
                             .join(watchlist, watchlist.c.cik == watchlist_history.c.cik)
                             .where(watchlist_history.c.created_at >= since)).all()
    adds = [f"{m.ticker} → {WL_TR.get(m.to_category, m.to_category)}" for m in moves if m.to_category in ("DISCOVERY", "HIGH_INTEREST")][:10]
    removed = [f"{m.ticker}: takipten çıkarıldı ({m.reason[:60]})" for m in moves if m.to_category == "ARCHIVED"][:10]
    ol = lambda rows: [one_line(dict(r)) for r in rows]
    parts = _section("🔬 Bu hafta keşfedilen şirketler (en ilginçten başlayarak)",
                     [f"{t} — {', '.join(THEME_TR.get(x, x) for x in (th or [])[:2]) or 'alan belirsiz'} ({STAGE_SHORT.get(s, s)})"
                      for t, b, th, s in new]) + \
        _section("🧭 En önemli görüş değişiklikleri", [f"{t}: {TEZ.get(c, c)} — {x[:80]}" for t, c, x in tch]) + \
        _section("🚀 Ticari ilerleme (ilk müşteri, satışa geçiş, büyüme hızlanması)", ol(milestones)) + \
        _section("🟢 Yönetici alımları", ol(insider)) + \
        _section("🏛 Şirket büyüklüğüne göre en büyük devlet sözleşmeleri", ol(big)) + \
        _section("⚠️ Riskli çok küçük şirketler (listede ama dikkat)", risky_rows[:8]) + \
        _section("➕ Takip listesine eklenenler", adds) + _section("➖ Takip listesinden çıkanlar", removed) + \
        _section("🧾 Bildirim karnesi (sistem ne kadar isabetli?)", card)
    head = f"📚 <b>PUBLIC EDGE HAFTALIK KEŞİF RAPORU</b> — {now:%d.%m.%Y}\n"
    body = "\n".join(parts).rstrip() if parts else "Bu hafta kayda değer bir keşif gelişmesi olmadı."
    return head + "\n" + body
