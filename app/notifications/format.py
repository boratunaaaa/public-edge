"""Telegram message formatting (HTML parse mode)."""
from __future__ import annotations

import html
from datetime import date

DISCLAIMER = "⚠️ Kamu verisi analizi — yatırım tavsiyesi değildir."
HEADERS = {
    "INSIDER_OPEN_MARKET_BUY": "INSIDER BUY",
    "CLUSTER_INSIDER_BUY": "INSIDER CLUSTER",
    "ACTIVIST_13D": "13D ACTIVIST",
    "GOV_CONTRACT": "GOVERNMENT CONTRACT",
    "CONGRESS_BUY": "CONGRESS BUY",
    "CONGRESS_CLUSTER_BUY": "CONGRESS CLUSTER",
    "TRUMP_FAMILY_SEC_EVENT": "TRUMP FAMILY SEC FILING",
    "FORM144_MAJOR_SALE": "FORM 144 (bilgi)",
    "MULTI_SIGNAL_EVENT": "MULTI SIGNAL",
}


def e(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def money(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= div * 0.9995:  # 999,999 -> $1.00M, not $1,000k
            return f"${v / div:.2f}{suf}" if suf != "k" else f"${v / div:,.0f}k"
    return f"${v:,.0f}"


def pct(v, digits: int = 1) -> str:
    return "n/a" if v is None else f"{v * 100:+.{digits}f}%"


def _filing_when(d: date | None) -> str:
    if d is None:
        return "n/a"
    delta = (date.today() - d).days
    return "bugün" if delta == 0 else ("dün" if delta == 1 else d.isoformat())


def history_block(stats: dict | None, label: str) -> list[str]:
    if not stats:
        return ["Geçmiş benzer eventler: henüz hesaplanmadı"]
    s30, s90 = stats.get(30) or {}, stats.get(90) or {}
    n = s90.get("n") or s30.get("n") or 0
    if not s30.get("sufficient") and not s90.get("sufficient"):
        return [f"Geçmiş benzer eventler ({e(label)}): örneklem yetersiz (n={n}) — istatistik gösterilmiyor"]
    lines = [f"Geçmiş benzer eventler ({e(label)}):"]
    if s30.get("n"):
        lines.append(f"30d median: {pct(s30.get('median'))} | vs SPY: {pct(s30.get('median_excess'))}")
    if s90.get("n"):
        lines.append(f"90d median: {pct(s90.get('median'))} | vs SPY: {pct(s90.get('median_excess'))}")
        if s90.get("spy_beat_rate") is not None:
            lines.append(f"SPY beat rate (90d): {s90['spy_beat_rate'] * 100:.0f}%")
    lines.append(f"Sample: {n}")
    return lines


def format_event(ev: dict, stats: dict | None = None, stats_label: str = "", extra: list[str] | None = None,
                 market_cap: float | None = None) -> str:
    t = ev["event_type"]
    p = ev.get("payload") or {}
    tk = ev.get("ticker")
    out = [f"🚨 <b>PUBLIC EDGE — {HEADERS.get(t, t)}</b>", ""]
    if tk:
        out.append(f"Ticker: <b>${e(tk)}</b>")
    if ev.get("company_name"):
        out.append(f"Company: {e(ev['company_name'])}")
    out.append("")
    if t == "INSIDER_OPEN_MARKET_BUY":
        out.append(f"{e(p.get('role'))}: {e(p.get('owner'))} — {money(p.get('value'))} BUY")
        out.append(f"Hisse: {p.get('shares') or 0:,.0f} @ ~${p.get('avg_price') or 0:,.2f}")
        if p.get("transaction_dates"):
            out.append(f"İşlem tarihi: {e(', '.join(p['transaction_dates'][:3]))}")
        if p.get("is_10b5_1"):
            out.append("Not: 10b5-1 planı kapsamında işaretli")
    elif t == "CLUSTER_INSIDER_BUY":
        for b in (p.get("buyers") or [])[:6]:
            out.append(f"{e(b.get('role'))}: {e(b.get('name'))} {money(b.get('value'))} BUY")
        out.append("")
        out.append(f"Toplam: {money(p.get('aggregate_value'))} | Alıcı: {p.get('number_of_buyers')}")
        out.append(f"Süre: {p.get('span_days')} gün (pencere {p.get('window_days')}g)")
    elif t == "ACTIVIST_13D":
        out.append(f"Yatırımcı: {e(p.get('investor'))}")
        pc = p.get("percent")
        out.append(f"Pay: {pc:.1f}%" if pc is not None else "Pay: n/a")
        if p.get("change") is not None:
            out.append(f"Değişim: {p['change']:+.2f} puan (önceki {p.get('previous_percent')}%)")
        out.append(f"Item 4 sınıfı: {e(p.get('item4_class'))}")
        if p.get("item4_excerpt"):
            out.append(f"<i>{e(p['item4_excerpt'][:300])}…</i>")
    elif t == "GOV_CONTRACT":
        out.append(f"Alıcı: {e(p.get('recipient'))}")
        out.append(f"Tutar: {money(p.get('amount'))} | Kurum: {e(p.get('agency'))}")
        out.append(f"Contract / Market Cap: {(p.get('value_to_mcap') or 0) * 100:.1f}% ({e(p.get('level'))})")
        if p.get("description"):
            out.append(f"<i>{e(p['description'][:200])}</i>")
    elif t == "CONGRESS_BUY":
        out.append(f"{e(p.get('politician'))} ({e(p.get('chamber'))})")
        out.append(f"Tutar aralığı: {e(p.get('amount_range'))} | Sahip: {e(p.get('owner'))}")
        out.append(f"İşlem: {e(p.get('transaction_date'))} | Açıklama gecikmesi: {p.get('disclosure_delay_days')} gün")
    elif t == "CONGRESS_CLUSTER_BUY":
        out.append(f"{p.get('n_members')} üye, {p.get('window_days')} gün içinde:")
        for tr in (p.get("trades") or [])[:6]:
            out.append(f"• {e(tr.get('politician'))} {e(tr.get('date'))} {e(tr.get('amount_range'))}")
    elif t == "TRUMP_FAMILY_SEC_EVENT":
        out.append(f"Kişi: {e(p.get('person'))}")
        out.append(f"Başvuru: {e(p.get('filing_type'))}")
        if p.get("ownership"):
            out.append(f"Sahiplik: {e(p['ownership'])}")
        out.append("Yalnızca belgelenmiş kamu kaydıdır; yorum içermez.")
    elif t == "FORM144_MAJOR_SALE":
        out.append(f"{e(p.get('insider'))} ({e(p.get('relationship'))})")
        out.append(f"Önerilen satış: {money(p.get('est_value'))} ({(p.get('pct_of_outstanding') or 0) * 100:.2f}% of shares)")
        out.append("10b5-1 planı: " + ("evet" if p.get("planned_10b5_1") else "belirtilmemiş"))
        out.append("Bilgi sinyalidir; tek başına negatif sinyal değildir.")
    elif t == "MULTI_SIGNAL_EVENT":
        out.append("Birleşen sinyaller: " + e(" + ".join(p.get("families") or [])))
        out.append(f"Bileşen event sayısı: {p.get('n_component_events')} | Süre: {p.get('span_days')} gün")
    else:
        out.append(e(ev.get("title")))
    out.append(f"Filing: {_filing_when(ev.get('public_date'))}")
    if market_cap:
        out.append(f"Market Cap: {money(market_cap)}")
    out.append("")
    out += history_block(stats, stats_label)
    if extra:
        out += ["", "Ek sinyaller:"] + [f"• {e(x)}" for x in extra]
    out += ["", "Kaynak:", e(ev.get("source_url")), "", DISCLAIMER]
    return "\n".join(out)
