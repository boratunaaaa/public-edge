"""V2 Telegram reports: PUBLIC EDGE MORNING BRIEF sections (appended to the existing daily summary - one message)
and the daily PUBLIC EDGE PAPER report. Concise by construction; research books are shown in their own section."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import escape

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

BAD = ("STALE", "DEGRADED", "BLOCKED", "SCHEMA_CHANGED", "AUTH_REQUIRED")


def source_warnings(conn: Connection) -> list[str]:
    """Only actionable problems: an enabled source that fails / went stale / changed format. Planned (DISABLED) sources
    and known access blocks are listed in the health report, not in the brief."""
    rows = conn.execute(text("""SELECT source, health_status, consecutive_failures, last_success FROM source_health
                                WHERE health_status = ANY(:b) AND status <> 'DISABLED' ORDER BY source"""),
                        {"b": list(BAD)}).mappings().all()
    return [f"{escape(r['source'])}: {r['health_status']}" + (f" ({r['consecutive_failures']} hata)" if r["consecutive_failures"] else "")
            for r in rows]


def morning_sections(conn: Connection, now: datetime) -> list[str]:
    since = now - timedelta(hours=24)
    out = []
    ca = conn.execute(text("""SELECT title FROM events WHERE country = 'CA' AND NOT is_backfill AND created_at >= :s
                              ORDER BY priority DESC, id DESC LIMIT 4"""), {"s": since}).scalars().all()
    if ca:
        out += ["🇨🇦 <b>Kanada (resmi kaynak)</b>"] + [f"• {escape(t[:140])}" for t in ca] + [""]
    tr = conn.execute(text("""SELECT title FROM events WHERE country = 'TR' AND NOT is_backfill AND created_at >= :s
                              ORDER BY priority DESC, id DESC LIMIT 4"""), {"s": since}).scalars().all()
    out += (["🇹🇷 <b>BIST</b>"] + [f"• {escape(t[:140])}" for t in tr] + [""]) if tr else \
        ["🇹🇷 <b>BIST</b>: canlı resmi veri bağlantısı yok (KAP/MKK erişimi bekleniyor)", ""]
    port = conn.execute(text("""SELECT p.ticker, p.status, p.exit_reason, a.book FROM paper_positions p JOIN paper_accounts a ON a.id = p.account_id
                                WHERE (p.exit_date >= :d OR p.entry_date >= :d) ORDER BY p.id DESC LIMIT 6"""),
                        {"d": (now - timedelta(days=1)).date()}).mappings().all()
    if port:
        out += ["💼 <b>Portföy olayları (paper)</b>"] + [
            f"• {r['ticker']}: {'açıldı' if r['status'] == 'OPEN' else 'kapandı (' + (r['exit_reason'] or '') + ')'}"
            + (" [araştırma]" if r["book"] == "RESEARCH" else "") for r in port] + [""]
    warn = source_warnings(conn)
    if warn:
        out += ["⚠️ <b>Veri uyarıları</b>"] + [f"• {w}" for w in warn[:6]] + [""]
    return out


def paper_report(engine: Engine, day: date | None = None) -> str | None:
    """None when nothing happened today (no message is sent)."""
    day = day or date.today()
    with engine.connect() as c:
        props = c.execute(text("""SELECT code, ticker, signal_family, research_only FROM proposed_trades
                                  WHERE proposal_created_at::date = :d ORDER BY id"""), {"d": day}).mappings().all()
        opened = c.execute(text("""SELECT p.ticker, a.strategy, a.book, p.entry_price FROM paper_positions p JOIN paper_accounts a ON a.id = p.account_id
                                   WHERE p.entry_date = :d ORDER BY p.id"""), {"d": day}).mappings().all()
        closed = c.execute(text("""SELECT p.ticker, a.strategy, a.book, p.exit_reason, p.realized_pnl, p.cost
                                   FROM paper_positions p JOIN paper_accounts a ON a.id = p.account_id
                                   WHERE p.exit_date = :d ORDER BY p.id"""), {"d": day}).mappings().all()
        perf = c.execute(text("""SELECT DISTINCT ON (a.id) a.name, a.book, pp.nav, pp.spy_nav, a.starting_capital
                                 FROM paper_accounts a JOIN paper_performance pp ON pp.account_id = a.id
                                 WHERE a.active ORDER BY a.id, pp.on_date DESC""")).mappings().all()
        from app.db.store import job_state
        rej = (job_state(c, "paper_scan_daily").get(str(day)) or {}).get("rejected", {})
        fam = c.execute(text("""SELECT pt.signal_family, count(*) n, sum(p.realized_pnl) pnl,
                                       count(*) FILTER (WHERE p.status = 'OPEN') open_n
                                FROM paper_positions p JOIN paper_accounts a ON a.id = p.account_id
                                JOIN proposed_trades pt ON pt.id = p.proposal_id
                                WHERE a.book = 'RESEARCH' GROUP BY 1 ORDER BY 2 DESC""")).mappings().all()
        warn = source_warnings(c)
        new_sig = c.execute(text("""SELECT event_family, count(*) FROM events WHERE NOT is_backfill AND event_family IS NOT NULL
                                    AND created_at::date = :d GROUP BY 1 ORDER BY 2 DESC LIMIT 8"""), {"d": day}).all()
    if not (props or opened or closed or rej):
        return None
    L = [f"📒 <b>PUBLIC EDGE PAPER RESEARCH</b> — {day:%d.%m.%Y}", "<i>sanal para; gerçek emir yok; ana portföy kapalı</i>", ""]
    if new_sig:
        L += ["📡 Yeni sinyaller (aile)"] + [f"• {f}: {n}" for f, n in new_sig] + [""]
    if props:
        L += ["🆕 Yeni öneriler"] + [f"• {p['code']} ${p['ticker']} ({p['signal_family']})" + (" [araştırma]" if p["research_only"] else "")
                                     for p in props] + [""]
    if opened:
        L += ["🟢 Açılan pozisyonlar"] + [f"• ${o['ticker']} {o['strategy']} @ {o['entry_price']:.2f}" + (" [araştırma]" if o["book"] == "RESEARCH" else "")
                                          for o in opened] + [""]
    if closed:
        tr = {"TIME": "süre doldu", "STOP": "stop", "THESIS_INVALIDATION": "tez bozuldu"}
        L += ["🔴 Kapanan pozisyonlar"] + [
            f"• ${x['ticker']} {x['strategy']}: {tr.get(x['exit_reason'], x['exit_reason'])}, "
            f"{(x['realized_pnl'] or 0) / (x['cost'] or 1) * 100:+.1f}%" + (" [araştırma]" if x["book"] == "RESEARCH" else "") for x in closed] + [""]
    for book, title in (("MAIN", "📊 Performans (ana hesaplar, SPY'a karşı)"), ("RESEARCH", "🧪 Araştırma hesapları (ayrı)")):
        rows = [p for p in perf if p["book"] == book]
        if rows:
            L += [title] + [f"• {escape(p['name'])}: {(p['nav'] / p['starting_capital'] - 1) * 100:+.1f}%"
                            + (f" (SPY {(p['spy_nav'] / p['starting_capital'] - 1) * 100:+.1f}%)" if p["spy_nav"] else "") for p in rows] + [""]
    if fam:
        L += ["🧪 Aile bazında araştırma pozisyonları"] + [
            f"• {r['signal_family']}: {r['n']} işlem ({r['open_n']} açık), gerçekleşen {(r['pnl'] or 0):+.0f} USD" for r in fam] + [""]
    if rej:
        L += ["⛔ Veri-kalite kapısında elenen"] + [f"• {k}: {v}" for k, v in sorted(rej.items(), key=lambda x: -x[1])[:6]] + [""]
    if warn:
        L += ["⚠️ Kaynak uyarıları"] + [f"• {w}" for w in warn[:5]]
    return "\n".join(L).rstrip()
