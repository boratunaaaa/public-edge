"""Commercial-inflection and balance-sheet metrics from quarterly XBRL series (pure functions)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

Series = list[tuple[date, float]]


def _by_quarter(s: Series) -> dict[tuple[int, int], float]:
    return {(d.year, (d.month - 1) // 3): v for d, v in s}


def yoy_series(s: Series) -> list[tuple[date, float]]:
    q = _by_quarter(s)
    out = []
    for d, v in s:
        prev = q.get((d.year - 1, (d.month - 1) // 3))
        if prev and prev > 0:
            out.append((d, v / prev - 1.0))
    return out


def ttm(s: Series) -> float | None:
    return sum(v for _, v in s[-4:]) if len(s) >= 4 else None


@dataclass
class Metrics:
    ttm_revenue: float | None = None
    yoy_latest: float | None = None
    yoy_prev: float | None = None
    qoq_latest: float | None = None
    gross_margin_latest: float | None = None
    gross_margin_year_ago: float | None = None
    cash: float | None = None
    annual_ocf: float | None = None
    runway_months: float | None = None
    rpo: float | None = None
    rpo_year_ago: float | None = None
    rd_to_revenue: float | None = None
    capex_growth: float | None = None
    capex_to_revenue: float | None = None
    sbc_to_revenue: float | None = None
    working_capital: float | None = None
    share_growth_yoy: float | None = None
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def compute(revenue: Series, gross_profit: Series, cash: Series, ocf_annual: Series, rpo: Series, rd: Series,
            capex_annual: Series, sbc_annual: Series, cur_assets: Series, cur_liab: Series,
            shares: Series | None = None) -> Metrics:
    m = Metrics()
    m.ttm_revenue = ttm(revenue)
    ys = yoy_series(revenue)
    if ys:
        m.yoy_latest = ys[-1][1]
        m.yoy_prev = ys[-2][1] if len(ys) >= 2 else None
    if len(revenue) >= 2 and revenue[-2][1] > 0:
        m.qoq_latest = revenue[-1][1] / revenue[-2][1] - 1
    gq = _by_quarter(gross_profit)
    rq = _by_quarter(revenue)
    margins = [(d, gq[k] / rq[k]) for d, _ in revenue for k in [(d.year, (d.month - 1) // 3)] if k in gq and rq.get(k)]
    if margins:
        m.gross_margin_latest = margins[-1][1]
        last = margins[-1][0]
        ya = [v for d, v in margins if d.year == last.year - 1 and (d.month - 1) // 3 == (last.month - 1) // 3]
        m.gross_margin_year_ago = ya[0] if ya else None
    m.cash = cash[-1][1] if cash else None
    m.annual_ocf = ocf_annual[-1][1] if ocf_annual else None
    if m.cash is not None and m.annual_ocf is not None and m.annual_ocf < 0:
        m.runway_months = m.cash / (-m.annual_ocf / 12)
    if rpo:
        m.rpo = rpo[-1][1]
        rq2 = _by_quarter(rpo)
        d = rpo[-1][0]
        m.rpo_year_ago = rq2.get((d.year - 1, (d.month - 1) // 3))
    if rd and m.ttm_revenue:
        m.rd_to_revenue = sum(v for _, v in rd[-4:]) / m.ttm_revenue if len(rd) >= 4 else None
    if len(capex_annual) >= 2 and capex_annual[-2][1] > 0:
        m.capex_growth = capex_annual[-1][1] / capex_annual[-2][1] - 1
    if capex_annual and m.ttm_revenue:
        m.capex_to_revenue = capex_annual[-1][1] / m.ttm_revenue
    if sbc_annual and m.ttm_revenue:
        m.sbc_to_revenue = sbc_annual[-1][1] / m.ttm_revenue
    if cur_assets and cur_liab:
        m.working_capital = cur_assets[-1][1] - cur_liab[-1][1]
    if shares and len(shares) >= 2:
        last_d, last_v = shares[-1]
        ya = [v for d, v in shares if 300 <= (last_d - d).days <= 430]
        if ya and ya[0] > 0:
            m.share_growth_yoy = last_v / ya[0] - 1
    m.flags = inflection_flags(m, ys, margins)
    return m


def inflection_flags(m: Metrics, ys: list[tuple[date, float]], margins: list[tuple[date, float]]) -> list[str]:
    """Trends need two consecutive confirming quarters - one strong quarter is noise."""
    f = []
    if len(ys) >= 3:
        y0, y1, y2 = ys[-3][1], ys[-2][1], ys[-1][1]
        if y1 >= 0.25 and y2 >= 0.25 and y2 > y1 > y0:
            f.append("REVENUE_ACCELERATION")
    if m.rpo and m.rpo_year_ago and m.rpo >= 1.5 * m.rpo_year_ago and m.ttm_revenue and m.rpo >= 0.5 * m.ttm_revenue:
        f.append("BACKLOG_BREAKOUT")
    if len(margins) >= 6:
        last2 = margins[-2:]
        ok = 0
        for d, v in last2:
            ya = [x for dd, x in margins if dd.year == d.year - 1 and (dd.month - 1) // 3 == (d.month - 1) // 3]
            if ya and v >= ya[0] + 0.05:
                ok += 1
        if ok == 2:
            f.append("MARGIN_INFLECTION")
    if m.capex_growth is not None and m.capex_growth >= 1.0 and (m.capex_to_revenue or 0) >= 0.10:
        f.append("CAPACITY_EXPANSION")
    if m.runway_months is not None and m.runway_months < 12:
        f.append("CASH_RUNWAY_RISK")
    return f
