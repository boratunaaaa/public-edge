"""FastAPI: JSON API + minimal functional dashboard (bound to localhost/tailnet only)."""
from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import and_, desc, func, select, text

from app.backtest.jobs import event_type_stats, person_stats, stats_overview, subgroup_for
from app.db.models import event_returns, events, notifications, processing_jobs, source_health
from app.db.session import get_engine
from app.sources.registry import SOURCES

app = FastAPI(title="PUBLIC EDGE", version="1.0.0")

TABS = [
    ("latest", "Son Olaylar", None),
    ("insider", "Yönetici Alımları", "INSIDER_OPEN_MARKET_BUY"),
    ("cluster", "Toplu Yönetici Alımı", "CLUSTER_INSIDER_BUY"),
    ("13d", "Aktivist Yatırımcılar (13D)", "ACTIVIST_13D"),
    ("contracts", "Devlet Sözleşmeleri", "GOV_CONTRACT"),
    ("congress", "Temsilciler Meclisi Alımları — Senato Verisi Yok", "CONGRESS_BUY,CONGRESS_CLUSTER_BUY"),
    ("trump", "Trump Ailesi", "TRUMP_FAMILY_SEC_EVENT"),
    ("multi", "Çoklu Sinyal", "MULTI_SIGNAL_EVENT"),
    ("f144", "Satış Niyeti (Form 144)", "FORM144_MAJOR_SALE"),
]


def _jsonable(o):
    return json.loads(json.dumps(o, default=lambda x: x.isoformat() if isinstance(x, (date, datetime)) else str(x)))


def _events(types: str | None, ticker: str | None, limit: int, include_backfill: bool, min_priority: int = 0) -> list[dict]:
    q = select(events.c.id, events.c.event_type, events.c.ticker, events.c.company_name, events.c.public_date,
               events.c.transaction_date, events.c.priority, events.c.title, events.c.source_url, events.c.is_backfill)
    conds = [events.c.priority >= min_priority]
    if types:
        conds.append(events.c.event_type.in_(types.split(",")))
    if ticker:
        conds.append(events.c.ticker == ticker.upper())
    if not include_backfill:
        conds.append(events.c.is_backfill.is_(False))
    with get_engine().connect() as conn:
        rows = conn.execute(q.where(and_(*conds)).order_by(desc(events.c.public_date), desc(events.c.id)).limit(limit)).mappings().all()
    return [dict(r) for r in rows]


@app.get("/health")
def health():
    """Healthy = DB reachable AND the live worker completed a source run recently."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            last = conn.execute(select(func.max(source_health.c.last_run))).scalar()
            srcs = conn.execute(select(source_health.c.source, source_health.c.status)).all()
    except Exception as e:
        return JSONResponse({"status": "error", "db": str(e)[:200]}, status_code=503)
    age = (datetime.now(timezone.utc) - last).total_seconds() if last else None
    ok = age is not None and age < 900
    body = {"status": "ok" if ok else "stale", "db": "ok", "last_source_run_age_s": age,
            "sources": {s: st for s, st in srcs}}
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/health/live")
def health_live():
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/events")
def api_events(type: str | None = None, ticker: str | None = None, limit: int = Query(100, le=1000),
               include_backfill: bool = False, min_priority: int = 0):
    return _jsonable(_events(type, ticker, limit, include_backfill, min_priority))


def _event_detail(eid: int) -> dict:
    with get_engine().connect() as conn:
        ev = conn.execute(select(events).where(events.c.id == eid)).mappings().first()
        if not ev:
            raise HTTPException(404)
        rets = conn.execute(select(event_returns).where(event_returns.c.event_id == eid).order_by(event_returns.c.horizon_days)).mappings().all()
        sg = subgroup_for(ev["event_type"], ev["payload"] or {})
        stats = event_type_stats(conn, ev["event_type"], sg)
        pstats = None
        p = ev["payload"] or {}
        if ev["event_type"] == "INSIDER_OPEN_MARKET_BUY" and p.get("owner_cik"):
            pstats = {"insider": p.get("owner"), "stats": person_stats(conn, ev["event_type"], "owner_cik", p["owner_cik"])}
        if ev["event_type"] == "CONGRESS_BUY" and p.get("politician"):
            pstats = {"politician": p.get("politician"), "stats": person_stats(conn, ev["event_type"], "politician", p["politician"])}
        notes = conn.execute(select(notifications.c.status, notifications.c.sent_at).where(notifications.c.event_id == eid)).mappings().all()
    return _jsonable({"event": dict(ev), "returns": [dict(r) for r in rets], "historical": {"subgroup": sg, "stats": stats},
                      "person": pstats, "notifications": [dict(n) for n in notes]})


@app.get("/api/events/{eid}")
def api_event(eid: int):
    return _event_detail(eid)


@app.get("/api/sources")
def api_sources():
    with get_engine().connect() as conn:
        rows = conn.execute(select(source_health)).mappings().all()
        jobs = conn.execute(select(processing_jobs)).mappings().all()
    return _jsonable({"sources": [dict(r) for r in rows], "jobs": [dict(j) for j in jobs],
                      "registered": [{"name": s.name, "enabled": s.enabled, "reason": s.disabled_reason} for s in SOURCES]})


@app.get("/api/stats")
def api_stats():
    with get_engine().connect() as conn:
        return _jsonable(stats_overview(conn))


@app.get("/api/counts")
def api_counts():
    with get_engine().connect() as conn:
        rows = conn.execute(text("""SELECT event_type, is_backfill, count(*) n FROM events GROUP BY 1,2 ORDER BY 1,2""")).mappings().all()
        tables = conn.execute(text("""SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY relname""")).mappings().all()
    return _jsonable({"events": [dict(r) for r in rows], "tables": [dict(t) for t in tables]})


# ------------------------------------------------------------------ dashboard

CSS = """body{font-family:-apple-system,system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1c2330}
header{background:#101828;color:#fff;padding:12px 16px}header b{font-size:18px}nav{display:flex;flex-wrap:wrap;gap:4px;padding:8px 16px;background:#fff;border-bottom:1px solid #e3e6ea}
nav a{padding:6px 10px;border-radius:6px;text-decoration:none;color:#344054;font-size:14px}nav a.on{background:#101828;color:#fff}
main{padding:12px 16px;max-width:1200px}table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{padding:6px 8px;border-bottom:1px solid #eef0f3;text-align:left;vertical-align:top}th{background:#f0f2f5}
.p{font-weight:700}.muted{color:#667085}.bf{color:#98a2b3}.ok{color:#067647}.err{color:#b42318}.warn{color:#b54708}
pre{white-space:pre-wrap;background:#fff;padding:10px;border:1px solid #e3e6ea;font-size:12px;overflow:auto}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:8px;padding:12px;margin:10px 0}"""


def _page(title: str, active: str, body: str) -> HTMLResponse:
    nav = "".join(f'<a class="{"on" if k == active else ""}" href="/?tab={k}">{html.escape(lbl)}</a>' for k, lbl, _ in TABS)
    nav += "".join(f'<a class="{"on" if k == active else ""}" href="/{k}">{html.escape(lbl)}</a>' for k, lbl in V2_NAV)
    nav += f'<a class="{"on" if active == "sources" else ""}" href="/sources">Kaynak Durumu</a>'
    nav += f'<a class="{"on" if active == "stats" else ""}" href="/stats">Geçmiş Performans</a>'
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PUBLIC EDGE - {html.escape(title)}</title><style>{CSS}</style></head><body><header><b>PUBLIC EDGE</b>
<span class="muted"> &nbsp;ABD kamu verisinden şirket olayları ve erken keşif</span></header><nav>{nav}</nav><main>{body}</main></body></html>""")


@app.get("/", response_class=HTMLResponse)
def dashboard(tab: str = "latest", backfill: int = 0, ticker: str | None = None):
    types = next((t for k, _l, t in TABS if k == tab), None)
    rows = _events(types, ticker, 200, bool(backfill))
    trs = "".join(
        f"<tr><td>{r['public_date']}</td><td class='p'>{r['priority']}</td><td>{html.escape(r['event_type'])}</td>"
        f"<td><a href='/?tab={tab}&ticker={html.escape(r['ticker'] or '')}&backfill={backfill}'>{html.escape(r['ticker'] or '')}</a></td>"
        f"<td><a href='/event/{r['id']}'>{html.escape(r['title'])}</a>{' <span class=bf>(backfill)</span>' if r['is_backfill'] else ''}</td>"
        f"<td><a href='{html.escape(r['source_url'])}' target=_blank rel=noopener>kaynak</a></td></tr>" for r in rows)
    toggle = f"<a href='/?tab={tab}&backfill={0 if backfill else 1}'>{'yalnız canlı olaylar' if backfill else 'geçmiş olayları da göster'}</a>"
    body = f"<p class=muted>{len(rows)} event {toggle}</p><table><tr><th>Açıklanma</th><th>Önem</th><th>Tür</th><th>Hisse</th><th>Olay</th><th></th></tr>{trs}</table>"
    return _page("events", tab, body)


def _fmt_pct(v):
    return "" if v is None else f"{v * 100:+.1f}%"


def _rate(v):
    return "" if v is None else f"{float(v) * 100:.0f}%"


@app.get("/event/{eid}", response_class=HTMLResponse)
def event_page(eid: int):
    d = _event_detail(eid)
    ev = d["event"]
    rets = "".join(f"<tr><td>{r['horizon_days']}d</td><td>{r['entry_date']} @ {r['entry_price']:.2f}</td><td>{r['exit_date']} @ {r['exit_price']:.2f}</td>"
                   f"<td>{_fmt_pct(r['ret'])}</td><td>{_fmt_pct(r['spy_ret'])}</td><td>{_fmt_pct(r['excess_ret'])}</td>"
                   f"<td>{_fmt_pct(r['mae'])}</td><td>{_fmt_pct(r['mfe'])}</td></tr>" for r in d["returns"]) or "<tr><td colspan=8 class=muted>henüz yok (ufuk dolmadı veya fiyat verisi yok)</td></tr>"
    hs = d["historical"]["stats"]
    hist = "".join(f"<tr><td>{h}d</td><td>{s['n']}</td><td>{_fmt_pct(s['mean'])}</td><td>{_fmt_pct(s['median'])}</td>"
                   f"<td>{_rate(s['win_rate'])}</td><td>{_rate(s['spy_beat_rate'])}</td>"
                   f"<td>{_fmt_pct(s['median_excess'])}</td><td class='{'ok' if s['sufficient'] else 'warn'}'>{'yeterli' if s['sufficient'] else 'yetersiz örneklem'}</td></tr>"
                   for h, s in hs.items())
    body = f"""<div class=card><h2>{html.escape(ev['title'])}</h2>
<p>Type: <b>{ev['event_type']}</b> · Ticker: <b>{ev['ticker'] or '-'}</b> · Company: {html.escape(ev['company_name'] or '-')}</p>
<p>Public (filing/publication) date: <b>{ev['public_date']}</b> {('· timestamp ' + str(ev['public_at'])) if ev['public_at'] else '(yalnızca tarih - giriş bir sonraki seansın açılışı)'}
 · Transaction date: {ev['transaction_date'] or '-'} · Priority: {ev['priority']} {'· backfill' if ev['is_backfill'] else ''}</p>
<p>Source: <a href="{html.escape(ev['source_url'])}" target=_blank rel=noopener>{html.escape(ev['source_url'])}</a></p></div>
<div class=card><h3>Bu eventin getirileri (giriş = public date sonrası ilk açılış)</h3><table><tr><th>Horizon</th><th>Entry</th><th>Exit</th><th>Return</th><th>SPY</th><th>Excess</th><th>MAE</th><th>MFE</th></tr>{rets}</table></div>
<div class=card><h3>Geçmiş benzer eventler ({ev['event_type']}/{d['historical']['subgroup']})</h3><table><tr><th>H</th><th>N</th><th>Mean</th><th>Median</th><th>Win</th><th>SPY beat</th><th>Median excess</th><th></th></tr>{hist}</table>
<p class=muted>Geçmiş performans gelecek garantisi değildir.</p></div>
<div class=card><h3>Payload</h3><pre>{html.escape(json.dumps(ev['payload'], indent=2, ensure_ascii=False))}</pre></div>
{('<div class=card><h3>Kişi bazlı</h3><pre>' + html.escape(json.dumps(d['person'], indent=2, ensure_ascii=False)) + '</pre></div>') if d['person'] else ''}"""
    return _page(ev["title"], "", body)


@app.get("/sources", response_class=HTMLResponse)
def sources_page():
    d = api_sources()
    trs = "".join(f"<tr><td>{html.escape(s['source'])}</td><td class='{'ok' if s['status'] == 'WORKING' else ('err' if s['status'] == 'ERROR' else 'warn')}'>{s['status']}</td>"
                  f"<td>{s.get('last_success') or ''}</td><td>{s.get('last_event') or ''}</td><td>{s.get('consecutive_failures')}</td>"
                  f"<td>{s.get('items_last_run')}</td><td>{html.escape((s.get('last_error') or '')[:200])}</td></tr>" for s in d["sources"])
    jobs = "".join(f"<tr><td>{html.escape(j['name'])}</td><td>{j['status']}</td><td>{j.get('last_finished') or ''}</td>"
                   f"<td><pre>{html.escape(json.dumps(j['state'])[:600])}</pre></td></tr>" for j in d["jobs"])
    body = (f"<h3>Veri kaynaklarının durumu</h3><table><tr><th>Source</th><th>Status</th><th>Last success</th><th>Last event</th><th>Fails</th><th>Items</th><th>Last error / reason</th></tr>{trs}</table>"
            f"<h3>Arka plan işleri</h3><table><tr><th>Job</th><th>Status</th><th>Finished</th><th>State</th></tr>{jobs}</table>")
    return _page("sources", "sources", body)


@app.get("/stats", response_class=HTMLResponse)
def stats_page():
    rows = api_stats()
    trs = "".join(f"<tr><td>{r['event_type']}</td><td>{r['horizon_days']}d</td><td>{r['n']}</td><td>{_fmt_pct(r['mean'])}</td><td>{_fmt_pct(r['median'])}</td>"
                  f"<td>{_rate(r['win_rate'])}</td><td>{_rate(r['spy_beat'])}</td>"
                  f"<td>{_fmt_pct(r['mean_excess'])}</td><td>{_fmt_pct(r['std'])}</td><td>{_fmt_pct(r['mae'])}</td><td>{_fmt_pct(r['mfe'])}</td>"
                  f"<td class='{'ok' if r['sufficient'] else 'warn'}'>{'yeterli' if r['sufficient'] else 'yetersiz'}</td></tr>" for r in rows)
    body = ("<p class=muted>Giriş: bilginin kamuya açıldığı an sonrası ilk açılış fiyatı (look-ahead yok). Getiriler split/temettü düzeltmeli. "
            "Geçmiş performans gelecek garantisi değildir.</p>"
            f"<table><tr><th>Type</th><th>H</th><th>N</th><th>Mean</th><th>Median</th><th>Win</th><th>SPY beat</th><th>Mean excess</th><th>Std</th><th>MAE</th><th>MFE</th><th>Sample</th></tr>{trs}</table>")
    return _page("backtest", "stats", body)


# ================================================================== V2: discovery / news / thesis / watchlist

from app.db.models import (company_thesis, discovery_companies, discovery_risks, news_items, news_story_clusters,  # noqa: E402
                           thesis_changes, watchlist, watchlist_history)

V2_NAV = [("discovery", "Keşif"), ("news", "Haberler"), ("thesis", "Şirket Görüşü"), ("watchlist", "Takip Listesi"),
          ("risks", "Küçük Şirket Riskleri"), ("thesis-changes", "Görüş Değişiklikleri")]


def _v2_page(title: str, active: str, body: str) -> HTMLResponse:
    return _page(title, active, body)


def _discovery_rows(bucket, theme, stage, min_yoy, insider, gov, customer, max_risk, level, limit=300):
    q = select(discovery_companies).where(discovery_companies.c.market_cap >= 50e6)
    if bucket:
        q = q.where(discovery_companies.c.bucket == bucket)
    if stage:
        q = q.where(discovery_companies.c.stage == stage)
    if level:
        q = q.where(discovery_companies.c.interest_level == level)
    if theme:
        q = q.where(discovery_companies.c.themes.contains([theme]))
    with get_engine().connect() as conn:
        rows = [dict(r) for r in conn.execute(q.order_by(desc(discovery_companies.c.interest_score)).limit(2000)).mappings().all()]
        risk_map: dict[str, list] = {}
        for r in conn.execute(select(discovery_risks)).mappings().all():
            risk_map.setdefault(r["cik"], []).append(f"{r['risk']}:{r['severity']}")
    out = []
    for r in rows:
        f = r["features"] or {}
        m = r["metrics"] or {}
        if min_yoy is not None and (m.get("yoy_latest") is None or m["yoy_latest"] < min_yoy):
            continue
        if insider and not f.get("insider_buy_value_90d"):
            continue
        if gov and not (f.get("government_backed") or f.get("gov_contract_to_mcap")):
            continue
        if customer and not any(c.get("category") == customer for c in (r["customers"] or [])):
            continue
        rk = risk_map.get(r["cik"], [])
        if max_risk == "no_high" and any(x.endswith(":HIGH") for x in rk):
            continue
        r["risks"] = rk
        out.append(r)
        if len(out) >= limit:
            break
    return out


@app.get("/api/discovery")
def api_discovery(bucket: str | None = None, theme: str | None = None, stage: str | None = None, min_yoy: float | None = None,
                  insider: bool = False, gov: bool = False, customer: str | None = None, max_risk: str | None = None,
                  level: str | None = None, limit: int = Query(100, le=500)):
    return _jsonable(_discovery_rows(bucket, theme, stage, min_yoy, insider, gov, customer, max_risk, level, limit))


@app.get("/discovery", response_class=HTMLResponse)
def discovery_page(bucket: str | None = None, theme: str | None = None, stage: str | None = None, min_yoy: float | None = None,
                   insider: bool = False, gov: bool = False, customer: str | None = None, max_risk: str | None = None,
                   level: str | None = None):
    rows = _discovery_rows(bucket, theme, stage, min_yoy, insider, gov, customer, max_risk, level)
    sel = lambda name, opts, cur: f"<select name={name}><option value=''>{name}</option>" + "".join(
        f"<option {'selected' if o == cur else ''}>{o}</option>" for o in opts) + "</select>"
    from app.discovery.language import STAGES, THEMES
    form = ("<form class=card>" + sel("bucket", ["MICRO", "EARLY_SMALL", "SMALL", "GROWTH", "ALREADY_SCALED"], bucket)
            + sel("theme", sorted(THEMES), theme) + sel("stage", STAGES, stage) + sel("level", ["HIGH", "MEDIUM", "LOW"], level)
            + sel("customer", ["HYPERSCALER", "TOP_SEMICONDUCTOR", "AUTOMOTIVE_OEM", "DEFENSE_PRIME", "US_GOVERNMENT", "LARGE_PHARMA", "LEADING_AI"], customer)
            + f" min YoY <input name=min_yoy size=4 value='{min_yoy if min_yoy is not None else ''}'>"
            + f" <label><input type=checkbox name=insider value=true {'checked' if insider else ''}>insider</label>"
            + f" <label><input type=checkbox name=gov value=true {'checked' if gov else ''}>gov</label>"
            + sel("max_risk", ["no_high"], max_risk) + " <button>filter</button></form>")
    trs = "".join(
        f"<tr><td><a href='/thesis?ticker={html.escape(r['ticker'] or '')}'>{html.escape(r['ticker'] or '')}</a></td><td>{html.escape((r['name'] or '')[:40])}</td>"
        f"<td>{(r['market_cap'] or 0) / 1e6:,.0f}M</td><td>{r['bucket']}</td><td>{html.escape(', '.join(r['themes'] or []))}</td><td>{r['stage']}</td>"
        f"<td>{_fmt_pct((r['metrics'] or {}).get('yoy_latest'))}</td><td class=p>{r['interest_level']} ({r['interest_score']})</td>"
        f"<td>{html.escape(', '.join(c['counterparty'] for c in (r['customers'] or [])[:3]))}</td>"
        f"<td class={'err' if any(x.endswith(':HIGH') for x in r['risks']) else 'muted'}>{html.escape(', '.join(r['risks'][:3]))}</td></tr>" for r in rows)
    body = (form + f"<p class=muted>{len(rows)} companies — İlgi seviyesi = araştırma önceliği (fiyat hareketi değil, iş ilerlemesine göre).</p>"
            "<table><tr><th>Ticker</th><th>Name</th><th>MCap</th><th>Bucket</th><th>Themes</th><th>Stage</th><th>Rev YoY</th><th>Interest</th><th>Customers</th><th>Risks</th></tr>"
            + trs + "</table>")
    return _v2_page("discovery", "discovery", body)


@app.get("/api/news")
def api_news(ticker: str | None = None, materiality: str | None = None, limit: int = Query(100, le=500)):
    q = select(news_story_clusters)
    if ticker:
        q = q.where(news_story_clusters.c.ticker == ticker.upper())
    if materiality:
        q = q.where(news_story_clusters.c.materiality == materiality)
    with get_engine().connect() as conn:
        return _jsonable([dict(r) for r in conn.execute(q.order_by(desc(news_story_clusters.c.public_at)).limit(limit)).mappings().all()])


@app.get("/news", response_class=HTMLResponse)
def news_page(ticker: str | None = None, materiality: str | None = None, min: str | None = None):
    rows = api_news(ticker, materiality, 300)
    if min == "HIGH":
        rows = [r for r in rows if r["materiality"] in ("HIGH", "CRITICAL")]
    trs = "".join(
        f"<tr><td>{r['public_at'][:16]}</td><td><a href='/news?ticker={html.escape(r['ticker'] or '')}'>{html.escape(r['ticker'] or '')}</a></td>"
        f"<td class={'err' if r['materiality'] in ('HIGH', 'CRITICAL') else 'muted'}>{r['materiality']}</td><td>{html.escape(', '.join(r['classes'] or []))}</td>"
        f"<td>{html.escape(r['agreement'] or '')}</td><td>{html.escape(r['headline'][:140])}</td><td>{r['n_items']}</td><td>{html.escape(', '.join(r['sources'] or []))}</td>"
        f"<td>{html.escape(r['mcap_bucket'] or '')}</td></tr>" for r in rows)
    body = ("<p><a href='/news?min=HIGH'>yalnız HIGH/CRITICAL</a> · public_at = hikâyenin en erken güvenilir yayın zamanı</p>"
            "<table><tr><th>Public at (UTC)</th><th>Ticker</th><th>Materiality</th><th>Classes</th><th>Agreement</th><th>Headline</th><th>Copies</th><th>Sources</th><th>Bucket</th></tr>"
            + trs + "</table>")
    return _v2_page("news", "news", body)


@app.get("/api/thesis/{ticker}")
def api_thesis(ticker: str):
    with get_engine().connect() as conn:
        t = conn.execute(select(company_thesis).where(company_thesis.c.ticker == ticker.upper())).mappings().first()
        ch = conn.execute(select(thesis_changes).where(thesis_changes.c.ticker == ticker.upper())
                          .order_by(desc(thesis_changes.c.created_at)).limit(20)).mappings().all()
    if not t:
        raise HTTPException(404)
    return _jsonable({"thesis": dict(t), "changes": [dict(c) for c in ch]})


@app.get("/thesis", response_class=HTMLResponse)
def thesis_page(ticker: str | None = None):
    if not ticker:
        with get_engine().connect() as conn:
            rows = conn.execute(select(company_thesis.c.ticker, company_thesis.c.thesis_last_updated)
                                .order_by(desc(company_thesis.c.thesis_last_updated)).limit(300)).all()
        body = "<table><tr><th>Ticker</th><th>Updated</th></tr>" + "".join(
            f"<tr><td><a href='/thesis?ticker={html.escape(t or '')}'>{html.escape(t or '')}</a></td><td>{u}</td></tr>" for t, u in rows) + "</table>"
        return _v2_page("thesis", "thesis", body)
    d = api_thesis(ticker)
    s = d["thesis"]["sections"]
    trs = "".join(f"<tr><td><b>{html.escape(k)}</b></td><td>{html.escape(str(v.get('text') or '— (kanıt yok)'))}</td>"
                  f"<td><pre>{html.escape(json.dumps(v.get('evidence'), ensure_ascii=False)[:800])}</pre></td></tr>" for k, v in s.items())
    ch = "".join(f"<tr><td>{c['created_at'][:16]}</td><td>{c['change']}</td><td>{html.escape(c['explanation'])}</td></tr>" for c in d["changes"])
    body = (f"<div class=card><h2>${html.escape(ticker.upper())} — company thesis</h2><p class=muted>Her alan yalnızca kaynaklı kanıttan üretilir; "
            f"kanıt yoksa boş bırakılır. Last updated {d['thesis']['thesis_last_updated']}</p></div>"
            f"<table><tr><th>Field</th><th>Value</th><th>Evidence</th></tr>{trs}</table>"
            f"<h3>Görüş değişiklikleri</h3><table><tr><th>At</th><th>Change</th><th>Why</th></tr>{ch}</table>")
    return _v2_page(ticker, "thesis", body)


@app.get("/api/watchlist")
def api_watchlist():
    with get_engine().connect() as conn:
        return _jsonable([dict(r) for r in conn.execute(select(watchlist).order_by(watchlist.c.category, watchlist.c.ticker)).mappings().all()])


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page():
    rows = api_watchlist()
    with get_engine().connect() as conn:
        hist = conn.execute(select(watchlist_history).order_by(desc(watchlist_history.c.created_at)).limit(50)).mappings().all()
    trs = "".join(f"<tr><td>{r['category']}</td><td><a href='/thesis?ticker={html.escape(r['ticker'] or '')}'>{html.escape(r['ticker'] or '')}</a></td>"
                  f"<td>{html.escape(r['reason'][:200])}</td><td>{'manual' if r['manual_override'] else 'auto'}</td><td>{(r['updated_at'] or '')[:16]}</td></tr>" for r in rows)
    hs = "".join(f"<tr><td>{str(h['created_at'])[:16]}</td><td>{h['cik']}</td><td>{h['from_category']} → {h['to_category']}</td>"
                 f"<td>{h['actor']}</td><td>{html.escape(h['reason'][:160])}</td></tr>" for h in hist)
    body = ("<p class=muted>Manuel değişiklik: <code>scripts/pe.sh exec worker python -m app.cli watchlist TICKER CATEGORY \"sebep\"</code></p>"
            f"<table><tr><th>Category</th><th>Ticker</th><th>Reason</th><th>By</th><th>Updated</th></tr>{trs}</table>"
            f"<h3>History</h3><table><tr><th>At</th><th>CIK</th><th>Move</th><th>By</th><th>Reason</th></tr>{hs}</table>")
    return _v2_page("watchlist", "watchlist", body)


@app.get("/risks", response_class=HTMLResponse)
def risks_page(severity: str | None = "HIGH"):
    with get_engine().connect() as conn:
        rows = conn.execute(select(discovery_risks, discovery_companies.c.ticker, discovery_companies.c.market_cap,
                                   discovery_companies.c.bucket).join(discovery_companies, discovery_companies.c.cik == discovery_risks.c.cik)
                            .where(discovery_companies.c.market_cap < 1e9, *([discovery_risks.c.severity == severity] if severity else []))
                            .order_by(discovery_companies.c.market_cap).limit(1000)).mappings().all()
    trs = "".join(f"<tr><td>{html.escape(r['ticker'] or '')}</td><td>{(r['market_cap'] or 0) / 1e6:,.0f}M</td><td>{r['risk']}</td>"
                  f"<td class={'err' if r['severity'] == 'HIGH' else 'warn'}>{r['severity']}</td><td>{html.escape(r['value'] or '')}</td>"
                  f"<td>{'<a href=' + html.escape(r['source_url']) + ' target=_blank>src</a>' if r['source_url'] else ''}</td></tr>" for r in rows)
    body = ("<p><a href='/risks?severity=HIGH'>HIGH</a> · <a href='/risks?severity=MEDIUM'>MEDIUM</a> · <a href='/risks?severity='>all</a> — sub-$1B şirketler</p>"
            f"<table><tr><th>Ticker</th><th>MCap</th><th>Risk</th><th>Severity</th><th>Detail</th><th></th></tr>{trs}</table>")
    return _v2_page("risks", "risks", body)


@app.get("/thesis-changes", response_class=HTMLResponse)
def thesis_changes_page():
    with get_engine().connect() as conn:
        rows = conn.execute(select(thesis_changes).order_by(desc(thesis_changes.c.created_at)).limit(300)).mappings().all()
    trs = "".join(f"<tr><td>{str(r['created_at'])[:16]}</td><td><a href='/thesis?ticker={html.escape(r['ticker'] or '')}'>{html.escape(r['ticker'] or '')}</a></td>"
                  f"<td class={'ok' if r['change'] == 'THESIS_STRENGTHENING' else 'err' if 'WEAK' in r['change'] or 'INVALID' in r['change'] else 'muted'}>{r['change']}</td>"
                  f"<td>{html.escape(r['explanation'])}</td><td>{html.escape(json.dumps(r['evidence'], ensure_ascii=False)[:300])}</td></tr>" for r in rows)
    body = ("<p class=muted>Tez değişimleri kanıta dayalı sınıflandırmadır; yatırım tavsiyesi değildir.</p>"
            f"<table><tr><th>At</th><th>Ticker</th><th>Change</th><th>Why</th><th>Evidence</th></tr>{trs}</table>")
    return _v2_page("thesis changes", "thesis-changes", body)


@app.get("/api/stats/news")
def api_news_stats():
    """News event returns by class x market-cap bucket (verified timestamps only)."""
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT c.cls, coalesce(e.payload->>'mcap_bucket', 'UNKNOWN') bucket, r.horizon_days, count(*) n,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY r.ret) median, avg(r.excess_ret) mean_excess,
                   (avg((r.excess_ret > 0)::int) FILTER (WHERE r.excess_ret IS NOT NULL))::float8 spy_beat
            FROM event_returns r JOIN events e ON e.id = r.event_id
            CROSS JOIN LATERAL jsonb_array_elements_text(e.payload->'classes') c(cls)
            WHERE e.event_type = 'MATERIAL_NEWS' AND e.date_quality <> 'estimated'
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""")).mappings().all()
    from app.config import settings as _s
    return _jsonable([dict(r) | {"sufficient": r["n"] >= _s.min_sample_size} for r in rows])
