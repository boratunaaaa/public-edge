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
    ("latest", "Latest Events", None),
    ("insider", "Insider Buys", "INSIDER_OPEN_MARKET_BUY"),
    ("cluster", "Cluster Buys", "CLUSTER_INSIDER_BUY"),
    ("13d", "13D Activists", "ACTIVIST_13D"),
    ("contracts", "Government Contracts", "GOV_CONTRACT"),
    ("congress", "Congress Buys", "CONGRESS_BUY,CONGRESS_CLUSTER_BUY"),
    ("trump", "Trump Family Events", "TRUMP_FAMILY_SEC_EVENT"),
    ("multi", "Multi Signals", "MULTI_SIGNAL_EVENT"),
    ("f144", "Form 144", "FORM144_MAJOR_SALE"),
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
    nav += f'<a class="{"on" if active == "sources" else ""}" href="/sources">Sources</a>'
    nav += f'<a class="{"on" if active == "stats" else ""}" href="/stats">Backtest</a>'
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PUBLIC EDGE - {html.escape(title)}</title><style>{CSS}</style></head><body><header><b>PUBLIC EDGE</b>
<span class="muted"> &nbsp;kamu verisi olay tespiti — yatırım tavsiyesi değildir</span></header><nav>{nav}</nav><main>{body}</main></body></html>""")


@app.get("/", response_class=HTMLResponse)
def dashboard(tab: str = "latest", backfill: int = 0, ticker: str | None = None):
    types = next((t for k, _l, t in TABS if k == tab), None)
    rows = _events(types, ticker, 200, bool(backfill))
    trs = "".join(
        f"<tr><td>{r['public_date']}</td><td class='p'>{r['priority']}</td><td>{html.escape(r['event_type'])}</td>"
        f"<td><a href='/?tab={tab}&ticker={html.escape(r['ticker'] or '')}&backfill={backfill}'>{html.escape(r['ticker'] or '')}</a></td>"
        f"<td><a href='/event/{r['id']}'>{html.escape(r['title'])}</a>{' <span class=bf>(backfill)</span>' if r['is_backfill'] else ''}</td>"
        f"<td><a href='{html.escape(r['source_url'])}' target=_blank rel=noopener>source</a></td></tr>" for r in rows)
    toggle = f"<a href='/?tab={tab}&backfill={0 if backfill else 1}'>{'canlı olayları göster' if backfill else 'geçmiş (backfill) olayları da göster'}</a>"
    body = f"<p class=muted>{len(rows)} event {toggle}</p><table><tr><th>Public date</th><th>Prio</th><th>Type</th><th>Ticker</th><th>Event</th><th></th></tr>{trs}</table>"
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
    body = (f"<h3>Source health</h3><table><tr><th>Source</th><th>Status</th><th>Last success</th><th>Last event</th><th>Fails</th><th>Items</th><th>Last error / reason</th></tr>{trs}</table>"
            f"<h3>Jobs</h3><table><tr><th>Job</th><th>Status</th><th>Finished</th><th>State</th></tr>{jobs}</table>")
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
