"""Simple price chart for an alert: last ~6 months of daily closes, the event day marked."""
from __future__ import annotations

import io
import logging
from datetime import date, timedelta

import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from sqlalchemy.engine import Connection  # noqa: E402

from app.marketdata.provider import BENCHMARK, ensure_prices  # noqa: E402

log = logging.getLogger(__name__)


def price_chart(conn: Connection, ticker: str, event_day: date, title: str, days: int = 180) -> bytes | None:
    """PNG bytes or None when there is not enough price data (never draws invented data)."""
    start = event_day - timedelta(days=days)
    end = min(date.today(), event_day + timedelta(days=days))
    try:
        bars = [b for b in ensure_prices(conn, ticker, start, end) if b.close]
        spy = {b.date: b for b in ensure_prices(conn, BENCHMARK, start, end) if b.close}
    except Exception as e:
        log.info("chart prices unavailable for %s: %s", ticker, e)
        return None
    if len(bars) < 20:
        return None
    d = [b.date for b in bars]
    c = [b.adj_close or b.close for b in bars]
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=110)
    ax.plot(d, c, color="#1f5aa6", linewidth=1.8, label=ticker)
    common = [x for x in d if x in spy]
    if len(common) >= 20:
        base_t = c[d.index(common[0])]
        base_s = spy[common[0]].adj_close or spy[common[0]].close
        ax.plot(common, [(spy[x].adj_close or spy[x].close) / base_s * base_t for x in common], color="#9aa5b1",
                linewidth=1.2, linestyle="--", label="Borsa ortalaması (S&P 500, aynı başlangıca ölçekli)")
    ax.axvline(event_day, color="#d9480f", linewidth=1.6)
    ymax = max(c)
    ax.annotate("Olay günü", xy=(event_day, ymax), xytext=(5, -12), textcoords="offset points", color="#d9480f", fontsize=9)
    ax.set_title(title[:90], fontsize=11)
    ax.set_ylabel("Fiyat ($)")
    ax.grid(alpha=0.25)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
