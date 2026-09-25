"""Paper ledger: virtual accounts, orders, fills, positions, cash and daily performance.

Execution model (no market-order fantasy):
  * an approved proposal becomes a BUY order that may fill only at the OPEN of the first session
    strictly after the decision (09:30 ET rule, same as the event backtest)
  * fill price = that open +/- slippage (microcaps pay more), plus a configurable commission
  * exits are SELL orders filled the same way: at the next session's open after the exit trigger
  * split/dividend-safe: a position's value is its entry price times the growth measured INSIDE ONE
    freshly fetched price series (entry-day adjusted open -> today's adjusted close), so the entry and
    the current price always share the same adjustment basis; a split after entry changes nothing and
    dividends count as reinvested (total return). Cached series are only a fallback.
Every strategy book (A-E) is a separate account with its own cash - results are never mixed.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, func, select, text
from sqlalchemy.engine import Connection, Engine

from app.db.models import (paper_accounts, paper_cash, paper_orders, paper_performance, paper_positions, paper_trades,
                           proposed_trades, security_master)
from app.marketdata.calendar import first_tradable_session, next_trading_day


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


STARTING_CAPITAL = _f("PAPER_STARTING_CAPITAL", 10_000)
COMMISSION = _f("PAPER_COMMISSION_USD", 1.0)
# strategy -> (account name, holding days, exit rule)
STRATEGIES = {
    "B_90D": ("PUBLIC EDGE PAPER", 90, "TIME"),
    "A_30D": ("PUBLIC EDGE PAPER A-30G", 30, "TIME"),
    "C_180D": ("PUBLIC EDGE PAPER C-180G", 180, "TIME"),
    "D_THESIS": ("PUBLIC EDGE PAPER D-TEZ", 365, "THESIS"),  # 365 days = safety cap only
    "E_STOP_TIME": ("PUBLIC EDGE PAPER E-STOP", 90, "STOP"),
}
STOP_PCT = 0.15


def slippage_bps(market_cap: float | None, bucket: str | None) -> float:
    if bucket == "MICRO" or market_cap is None or market_cap < 300e6:
        return _f("PAPER_SLIPPAGE_MICRO_BPS", 50)
    if market_cap < 2e9:
        return _f("PAPER_SLIPPAGE_SMALL_BPS", 20)
    return _f("PAPER_SLIPPAGE_BPS", 10)


def ensure_accounts(conn: Connection, capital: float | None = None) -> dict[str, int]:
    capital = capital or STARTING_CAPITAL
    out = {}
    for strat, (name, _, _) in STRATEGIES.items():
        aid = conn.execute(select(paper_accounts.c.id).where(paper_accounts.c.name == name)).scalar()
        if aid is None:
            aid = conn.execute(paper_accounts.insert().values(name=name, strategy=strat, starting_capital=capital,
                                                              commission_per_trade=COMMISSION).returning(paper_accounts.c.id)).scalar_one()
            conn.execute(paper_cash.insert().values(account_id=aid, on_date=date.today(), delta=capital, balance_after=capital,
                                                    reason="DEPOSIT"))
        out[strat] = aid
    return out


def cash_of(conn: Connection, account_id: int) -> float:
    v = conn.execute(select(paper_cash.c.balance_after).where(paper_cash.c.account_id == account_id)
                     .order_by(paper_cash.c.id.desc()).limit(1)).scalar()
    return float(v or 0.0)


def _move_cash(conn: Connection, account_id: int, on: date, delta: float, reason: str, ref: int | None) -> float:
    bal = cash_of(conn, account_id) + delta
    conn.execute(paper_cash.insert().values(account_id=account_id, on_date=on, delta=delta, balance_after=bal, reason=reason,
                                            ref_id=ref))
    return bal


def queue_buy(conn: Connection, proposal: dict, decided_at: datetime) -> list[int]:
    """One BUY order per active strategy book; fills at the first session opening after the decision."""
    accts = ensure_accounts(conn)
    not_before = first_tradable_session(decided_at.date(), decided_at)
    ids = []
    for strat, aid in accts.items():
        active = conn.execute(select(paper_accounts.c.active).where(paper_accounts.c.id == aid)).scalar()
        if not active:
            continue
        ids.append(conn.execute(paper_orders.insert().values(
            account_id=aid, proposal_id=proposal["id"], ticker=proposal["ticker"], side="BUY", requested_at=decided_at,
            not_before=not_before, target_pct=proposal["suggested_position_pct"], status="PENDING",
        ).returning(paper_orders.c.id)).scalar_one())
    return ids


def _bars(conn: Connection, symbol: str, start: date, end: date):
    from app.marketdata.provider import ensure_prices
    try:
        return [b for b in ensure_prices(conn, symbol, start, end) if b.open and b.close]
    except Exception:
        from app.marketdata.provider import load_bars
        return [b for b in load_bars(conn, symbol, start, end) if b.open and b.close]


def _factor(b) -> float:
    return (b.adj_close / b.close) if (b.adj_close and b.close) else 1.0


def fresh_bars(conn: Connection, symbol: str, start: date, end: date):
    """Bars from ONE provider request (one adjustment basis). Falls back to the cache when the
    provider has nothing (offline, delisted)."""
    from app.marketdata.provider import get_provider
    try:
        got = get_provider().get_historical_prices(symbol, start, end)
    except Exception:
        got = []
    got = [b for b in got if b.open and b.close and start <= b.date <= end]
    return got or _bars(conn, symbol, start, end)


def _entry_open(conn: Connection, pos: dict) -> float:
    """Raw open paid at entry (slippage removed), in entry-day share units."""
    r = conn.execute(select(paper_trades.c.price, paper_trades.c.slippage_bps).where(and_(
        paper_trades.c.position_id == pos["id"], paper_trades.c.side == "BUY"))).first()
    return r[0] / (1 + (r[1] or 0) / 1e4) if r else pos["entry_price"]


class Growth:
    """Per-entry-share prices from one consistently adjusted series."""

    def __init__(self, bars, entry_date: date, entry_open: float):
        e = next((b for b in bars if b.date == entry_date), None)
        self.base = e.open * _factor(e) if e else None
        self.entry_open = entry_open

    @property
    def ok(self) -> bool:
        return bool(self.base)

    def close(self, b) -> float:
        return self.entry_open * (b.adj_close or b.close) / self.base

    def open(self, b) -> float:
        return self.entry_open * b.open * _factor(b) / self.base


def position_value(conn: Connection, pos: dict, on: date) -> tuple[float, float | None, date | None]:
    """(value, price per entry-share, price date) at the last close on/before `on`."""
    bars = fresh_bars(conn, pos["ticker"], pos["entry_date"], on)
    g = Growth(bars, pos["entry_date"], _entry_open(conn, pos))
    if not bars or not g.ok:
        px = pos.get("last_price") or pos["entry_price"]
        return pos["shares"] * px, pos.get("last_price"), pos.get("last_price_date")
    px = g.close(bars[-1])
    return pos["shares"] * px, px, bars[-1].date


def nav_of(conn: Connection, account_id: int, on: date) -> float:
    nav = cash_of(conn, account_id)
    for p in conn.execute(select(paper_positions).where(and_(paper_positions.c.account_id == account_id,
                                                             paper_positions.c.status != "CLOSED"))).mappings():
        nav += position_value(conn, dict(p), on)[0]
    return nav


def _proposal_info(conn: Connection, proposal_id: int | None) -> dict:
    if not proposal_id:
        return {}
    r = conn.execute(select(proposed_trades.c.gate, proposed_trades.c.cik).where(proposed_trades.c.id == proposal_id)).first()
    return {"gate": (r[0] or {}) if r else {}, "cik": r[1] if r else None}


def fill_orders(conn: Connection, today: date) -> dict:
    out = {"filled": 0, "cancelled": 0, "waiting": 0}
    rows = conn.execute(select(paper_orders).where(and_(paper_orders.c.status == "PENDING", paper_orders.c.not_before <= today))
                        .order_by(paper_orders.c.id)).mappings().all()
    for o in rows:
        bars = [b for b in _bars(conn, o["ticker"], o["not_before"], today) if b.date >= o["not_before"]]
        if not bars:
            out["waiting"] += 1
            continue
        b = bars[0]
        acct = conn.execute(select(paper_accounts).where(paper_accounts.c.id == o["account_id"])).mappings().first()
        info = _proposal_info(conn, o["proposal_id"])
        g = info.get("gate", {})
        bps = slippage_bps(g.get("market_cap"), g.get("bucket"))
        comm = float(acct["commission_per_trade"])
        if o["side"] == "BUY":
            px = b.open * (1 + bps / 1e4)
            nav = nav_of(conn, o["account_id"], b.date - timedelta(days=1))
            notional = min(o["target_pct"] * nav, cash_of(conn, o["account_id"]) - comm)
            if notional < 10:
                conn.execute(paper_orders.update().where(paper_orders.c.id == o["id"]).values(status="CANCELLED",
                                                                                                note="insufficient paper cash"))
                out["cancelled"] += 1
                continue
            shares = notional / px
            strat = acct["strategy"]
            hold = STRATEGIES[strat][1]
            sector = conn.execute(select(security_master.c.sector).where(security_master.c.cik == info.get("cik"))).scalar() \
                if info.get("cik") else None
            from app.backtest.sector import etf_for_sector
            pos_id = conn.execute(paper_positions.insert().values(
                account_id=o["account_id"], proposal_id=o["proposal_id"], ticker=o["ticker"], cik=info.get("cik"),
                shares=shares, entry_date=b.date, entry_price=px, cost=notional + comm, adj_factor=1.0,
                stop_price=px * (1 - STOP_PCT), planned_exit_date=b.date + timedelta(days=hold),
                sector_etf=etf_for_sector(sector), status="OPEN", last_price=px, last_price_date=b.date,
            ).returning(paper_positions.c.id)).scalar_one()
            gross = notional
            _move_cash(conn, o["account_id"], b.date, -(notional + comm), "BUY", o["id"])
        else:
            pos = dict(conn.execute(select(paper_positions).where(paper_positions.c.id == o["position_id"])).mappings().first())
            series = fresh_bars(conn, o["ticker"], pos["entry_date"], today)
            g = Growth(series, pos["entry_date"], _entry_open(conn, pos))
            sb = next((x for x in series if x.date >= o["not_before"]), None) if g.ok else None
            if sb is None:
                out["waiting"] += 1
                continue
            b = sb
            px = g.open(b) * (1 - bps / 1e4)
            shares = pos["shares"]
            gross = shares * px
            pos_id = pos["id"]
            proceeds = gross - comm
            conn.execute(paper_positions.update().where(paper_positions.c.id == pos_id).values(
                status="CLOSED", exit_date=b.date, exit_price=px, realized_pnl=proceeds - pos["cost"],
                last_price=px, last_price_date=b.date))
            _move_cash(conn, o["account_id"], b.date, proceeds, "SELL", o["id"])
        conn.execute(paper_orders.update().where(paper_orders.c.id == o["id"]).values(
            status="FILLED", fill_date=b.date, fill_price=px, shares=shares, slippage_bps=bps, commission=comm,
            position_id=pos_id))
        conn.execute(paper_trades.insert().values(account_id=o["account_id"], order_id=o["id"], position_id=pos_id,
                                                  ticker=o["ticker"], side=o["side"], trade_date=b.date, price=px, shares=shares,
                                                  gross=gross, commission=comm, slippage_bps=bps))
        out["filled"] += 1
    return out


def thesis_invalidated(conn: Connection, pos: dict, until: date) -> date | None:
    """First date after entry with a thesis-breaking public fact for this company."""
    if not pos.get("cik"):
        return None
    r = conn.execute(text("""
        SELECT min(d) FROM (
          SELECT e.public_date d FROM events e WHERE e.issuer_cik = :cik AND e.public_date > :entry AND e.public_date <= :until
             AND e.event_type = 'TENK_CHANGE' AND e.payload->'change_types' ? 'NEW_GOING_CONCERN'
          UNION ALL
          SELECT tc.created_at::date FROM thesis_changes tc WHERE tc.cik = :cik AND tc.change = 'THESIS_INVALIDATION'
             AND tc.created_at::date > :entry AND tc.created_at::date <= :until
          UNION ALL
          SELECT dr.detected_at::date FROM discovery_risks dr WHERE dr.cik = :cik AND dr.risk = 'GOING_CONCERN'
             AND dr.severity = 'HIGH' AND dr.detected_at::date > :entry AND dr.detected_at::date <= :until) x"""),
        {"cik": pos["cik"], "entry": pos["entry_date"], "until": until}).scalar()
    return r


def check_exits(conn: Connection, today: date) -> dict:
    out = {"exit_orders": 0}
    rows = conn.execute(select(paper_positions, paper_accounts.c.strategy).join(
        paper_accounts, paper_accounts.c.id == paper_positions.c.account_id).where(paper_positions.c.status == "OPEN")).mappings().all()
    for p in rows:
        p = dict(p)
        rule = STRATEGIES[p["strategy"]][2]
        series = fresh_bars(conn, p["ticker"], p["entry_date"], today)
        g = Growth(series, p["entry_date"], _entry_open(conn, p))
        bars = [b for b in series if b.date > p["entry_date"]] if g.ok else []
        if bars:
            last = bars[-1]
            conn.execute(paper_positions.update().where(paper_positions.c.id == p["id"]).values(
                last_price=g.close(last), last_price_date=last.date))
        trigger, reason, not_before = None, None, None
        if rule == "STOP":
            for b in bars:
                if g.close(b) <= p["stop_price"]:
                    trigger, reason = b.date, "STOP"
                    break
        if rule == "THESIS" and trigger is None:
            d = thesis_invalidated(conn, p, today)
            if d:
                trigger, reason = d, "THESIS_INVALIDATION"
        if trigger is not None:
            not_before = next_trading_day(trigger)
        elif p["planned_exit_date"] and p["planned_exit_date"] <= today:
            not_before, reason = p["planned_exit_date"], "TIME"
        if not_before is None:
            continue
        conn.execute(paper_positions.update().where(paper_positions.c.id == p["id"]).values(status="CLOSING", exit_reason=reason))
        conn.execute(paper_orders.insert().values(account_id=p["account_id"], proposal_id=p["proposal_id"], position_id=p["id"],
                                                  ticker=p["ticker"], side="SELL", requested_at=datetime.now(timezone.utc),
                                                  not_before=not_before, status="PENDING", note=reason))
        out["exit_orders"] += 1
    return out


def snapshot(conn: Connection, today: date) -> int:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    n = 0
    spy = _bars(conn, "SPY", today - timedelta(days=800), today)
    spy_by = {b.date: (b.adj_close or b.close) for b in spy}
    spy_dates = sorted(spy_by)
    for a in conn.execute(select(paper_accounts)).mappings().all():
        cash = cash_of(conn, a["id"])
        pv = 0.0
        n_open = 0
        for p in conn.execute(select(paper_positions).where(and_(paper_positions.c.account_id == a["id"],
                                                                 paper_positions.c.status != "CLOSED"))).mappings():
            pv += position_value(conn, dict(p), today)[0]
            n_open += 1
        first = conn.execute(select(func.min(paper_trades.c.trade_date)).where(paper_trades.c.account_id == a["id"])).scalar()
        spy_nav = a["starting_capital"]
        if first and spy_dates:
            base = next((spy_by[d] for d in spy_dates if d >= first), None)
            last = spy_by[spy_dates[-1]]
            if base:
                spy_nav = a["starting_capital"] * last / base
        vals = {"nav": cash + pv, "cash": cash, "positions_value": pv, "spy_nav": spy_nav, "n_open": n_open}
        conn.execute(pg_insert(paper_performance).values(account_id=a["id"], on_date=today, **vals)
                     .on_conflict_do_update(index_elements=["account_id", "on_date"], set_=vals))
        n += 1
    return n


def run(engine: Engine, today: date | None = None) -> dict:
    from app.paper.proposals import expire
    today = today or date.today()
    out = {"expired": expire(engine)}
    with engine.begin() as conn:
        ensure_accounts(conn)
        out["fills"] = fill_orders(conn, today)
    with engine.begin() as conn:
        out["exits"] = check_exits(conn, today)
    with engine.begin() as conn:
        out["fills_after_exit"] = fill_orders(conn, today)
        out["snapshots"] = snapshot(conn, today)
    return out


# ------------------------------------------------------------------ dashboard metrics

def max_drawdown(navs: list[float]) -> float | None:
    if not navs:
        return None
    peak, mdd = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1 if peak else 0.0)
    return mdd


def account_metrics(conn: Connection, account_id: int) -> dict:
    a = conn.execute(select(paper_accounts).where(paper_accounts.c.id == account_id)).mappings().first()
    perf = conn.execute(select(paper_performance).where(paper_performance.c.account_id == account_id)
                        .order_by(paper_performance.c.on_date)).mappings().all()
    closed = conn.execute(select(paper_positions).where(and_(paper_positions.c.account_id == account_id,
                                                             paper_positions.c.status == "CLOSED"))).mappings().all()
    open_ = conn.execute(select(paper_positions).where(and_(paper_positions.c.account_id == account_id,
                                                            paper_positions.c.status != "CLOSED"))).mappings().all()
    last = perf[-1] if perf else None
    unreal = sum((p["last_price"] or p["entry_price"]) * p["shares"] - p["cost"] for p in open_)
    sector_adj = []
    for p in list(closed) + list(open_):
        if not p["sector_etf"]:
            continue
        end = p["exit_date"] or p["last_price_date"]
        sb = _bars(conn, p["sector_etf"], p["entry_date"], end) if end else []
        if len(sb) >= 2:
            sret = (sb[-1].adj_close or sb[-1].close) / (sb[0].adj_close or sb[0].close) - 1
            pret = ((p["exit_price"] or p["last_price"] or p["entry_price"]) / p["entry_price"]) - 1
            sector_adj.append(pret - sret)
    hold = [((p["exit_date"] - p["entry_date"]).days) for p in closed if p["exit_date"]]
    return {
        "account": a["name"], "strategy": a["strategy"], "starting_capital": a["starting_capital"],
        "nav": last["nav"] if last else a["starting_capital"], "cash": cash_of(conn, account_id),
        "spy_nav": last["spy_nav"] if last else a["starting_capital"],
        "open_positions": len(open_), "closed_positions": len(closed),
        "realized_pnl": sum(p["realized_pnl"] or 0 for p in closed), "unrealized_pnl": unreal,
        "max_drawdown": max_drawdown([r["nav"] for r in perf]),
        "win_rate": (sum((p["realized_pnl"] or 0) > 0 for p in closed) / len(closed)) if closed else None,
        "avg_holding_days": (sum(hold) / len(hold)) if hold else None,
        "avg_sector_adjusted_return": (sum(sector_adj) / len(sector_adj)) if sector_adj else None,
    }
