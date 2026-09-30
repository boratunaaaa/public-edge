"""Run output, computed AFTER the replay from its isolated schema (the evaluation layer: future prices are allowed here
because no decision is taken anymore - excess return vs SPY, MFE / MAE)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from app.db.models import paper_accounts, paper_orders, paper_performance, paper_positions, paper_trades, proposed_trades
from app.paper.ledger import max_drawdown
from app.replay.source import SourceData


def _px(bars, d: date, on_or_before: bool = True):
    xs = [b for b in bars if (b.date <= d if on_or_before else b.date >= d) and (b.adj_close or b.close)]
    if not xs:
        return None
    b = xs[-1] if on_or_before else xs[0]
    return b.adj_close or b.close


def collect(engine: Engine, src: SourceData, last_day: date | None) -> dict:
    spy = src.bars.get("SPY", [])
    with engine.connect() as c:
        props = c.execute(select(proposed_trades.c.signal_family, proposed_trades.c.approval_status, func.count())
                          .group_by(proposed_trades.c.signal_family, proposed_trades.c.approval_status)).all()
        orders = c.execute(select(paper_orders.c.side, paper_orders.c.status, func.count())
                           .group_by(paper_orders.c.side, paper_orders.c.status)).all()
        capped = c.execute(select(func.count()).where(paper_orders.c.note == "participation cap: partial fill")).scalar()
        trades = c.execute(select(paper_trades.c.account_id, paper_trades.c.gross, paper_trades.c.commission,
                                  paper_trades.c.slippage_bps).order_by(paper_trades.c.id)).all()
        pos = c.execute(select(paper_positions, proposed_trades.c.signal_family, paper_accounts.c.strategy)
                        .join(proposed_trades, proposed_trades.c.id == paper_positions.c.proposal_id, isouter=True)
                        .join(paper_accounts, paper_accounts.c.id == paper_positions.c.account_id)
                        .order_by(paper_positions.c.id)).mappings().all()
        perf = c.execute(select(paper_performance.c.account_id, paper_performance.c.on_date, paper_performance.c.nav,
                                paper_performance.c.positions_value).order_by(paper_performance.c.on_date,
                                                                              paper_performance.c.account_id)).all()
        accts = {r.id: (r.strategy, float(r.starting_capital)) for r in c.execute(select(paper_accounts).order_by(paper_accounts.c.id))}
    closed = [p for p in pos if p["status"] == "CLOSED"]
    per_family: dict[str, list] = defaultdict(list)
    per_pos = []
    for p in closed:
        ret = float(p["realized_pnl"]) / float(p["cost"]) if p["cost"] else None
        s0, s1 = _px(spy, p["entry_date"], on_or_before=False), _px(spy, p["exit_date"])
        spy_ret = (s1 / s0 - 1) if (s0 and s1) else None
        bars = [b for b in src.bars.get(p["ticker"], []) if p["entry_date"] <= b.date <= p["exit_date"] and b.close]
        e = float(p["entry_price"])
        mfe = (max(b.high or b.close for b in bars) / e - 1) if bars and e else None
        mae = (min(b.low or b.close for b in bars) / e - 1) if bars and e else None
        row = {"position_id": p["id"], "strategy": p["strategy"], "family": p["signal_family"], "ticker": p["ticker"],
               "entry": p["entry_date"], "exit": p["exit_date"], "reason": p["exit_reason"], "return": ret,
               "excess_vs_spy": (ret - spy_ret) if (ret is not None and spy_ret is not None) else None, "mfe": mfe, "mae": mae}
        per_pos.append(row)
        per_family[p["signal_family"] or "?"].append(row)
    nav_by_acct: dict[int, list] = defaultdict(list)
    expo: dict[int, list] = defaultdict(list)
    for aid, _d, nav, pv in perf:
        nav_by_acct[aid].append(float(nav))
        if nav:
            expo[aid].append(float(pv or 0) / float(nav))
    gross = sum(float(t.gross or 0) for t in trades)
    costs = sum(float(t.commission or 0) + float(t.gross or 0) * float(t.slippage_bps or 0) / 1e4 for t in trades)
    books = {}
    for aid, (strat, cap) in sorted(accts.items()):
        navs = nav_by_acct.get(aid, [])
        avg_nav = (sum(navs) / len(navs)) if navs else cap
        books[strat] = {"final_nav": navs[-1] if navs else cap, "return": (navs[-1] / cap - 1) if navs else 0.0,
                        "max_drawdown": max_drawdown(navs) if navs else None,
                        "avg_exposure": (sum(expo[aid]) / len(expo[aid])) if expo.get(aid) else 0.0,
                        "turnover": (sum(float(t.gross or 0) for t in trades if t.account_id == aid) / avg_nav) if avg_nav else None}

    def agg(rows):
        rs = [r["return"] for r in rows if r["return"] is not None]
        ex = [r["excess_vs_spy"] for r in rows if r["excess_vs_spy"] is not None]
        return {"n": len(rows), "mean_return": (sum(rs) / len(rs)) if rs else None,
                "mean_excess": (sum(ex) / len(ex)) if ex else None,
                "win_rate": (sum(1 for x in rs if x > 0) / len(rs)) if rs else None}
    return {"last_session": last_day, "proposals": {f"{f}:{s}": n for f, s, n in props},
            "orders": {f"{side}:{st}": n for side, st, n in orders}, "partial_fills_capped": capped,
            "fills": len(trades), "open_positions": sum(1 for p in pos if p["status"] != "CLOSED"),
            "closed_positions": len(closed), "overall": agg(per_pos), "by_family": {k: agg(v) for k, v in per_family.items()},
            "books": books, "traded_notional": gross, "transaction_costs": costs, "positions": per_pos}
