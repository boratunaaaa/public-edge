"""Point-in-time visibility: what the run's schema may contain at the simulated moment, the capped price provider, the
point-in-time identity check and the leakage guards."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app import http
from app.core import clock
from app.db.models import (companies, corporate_actions, events, security_master, security_symbols,
                           tickers, universe_snapshots)
from app.entities.names import normalize_company
from app.marketdata.provider import Bar, MarketDataProvider, shares_as_of
from app.replay.errors import ReplayLeakage
from app.replay.source import SourceData

SYMBOL_GRACE_DAYS = 120  # same grace the security master uses for a filing-observed symbol
UNIVERSE_STALE_DAYS = 400  # production build_universe: an older cover page is not a market-cap basis
ADV_BUCKETS = ("MICRO", "EARLY_SMALL")  # production build_universe computes 30-day $ volume only for these


@dataclass
class Visibility:
    """signal_cap: last session whose bar the decision layer may see; exec_cap: the session execution may fill on."""
    phase: str = "SIGNAL"
    signal_cap: date | None = None
    exec_cap: date | None = None

    @property
    def cap(self) -> date | None:
        return self.exec_cap if self.phase == "EXECUTION" else self.signal_cap


@dataclass
class Guards:
    """Every violation raises ReplayLeakage (the run fails); nothing is ever silently clipped into a decision."""
    log: list[dict] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)

    def fail(self, kind: str, detail: str) -> None:
        self.log.append({"kind": kind, "detail": detail, "at": clock.now().isoformat()})
        raise ReplayLeakage(kind, detail)

    def review(self, event_id: int, reason: str) -> None:
        self.reviews.append({"event_id": event_id, "reason": reason, "at": clock.now().isoformat()})


class ReplayPriceProvider(MarketDataProvider):
    """Serves the input snapshot's bars. Never returns a bar after the visibility cap; a request that reaches past the
    simulated DATE is itself a leak (no production path asks for tomorrow) and fails the run."""
    name = "replay"

    def __init__(self, src: SourceData, vis: Visibility, guards: Guards):
        self.src, self.vis, self.guards = src, vis, guards
        self.requests = 0

    def get_historical_prices(self, symbol: str, start: date, end: date) -> list[Bar]:
        self.requests += 1
        if end > clock.today():
            self.guards.fail("FUTURE_PRICE_REQUEST", f"{symbol}: requested up to {end}, simulated date {clock.today()}")
        bars = self.src.bars.get(symbol)
        if bars is None:
            raise http.HttpError(f"replay://{symbol}", 404, "symbol not in the replay snapshot")
        cap = self.vis.cap
        return [b for b in bars if start <= b.date <= end and (cap is None or b.date <= cap)]


def _row(e: dict) -> dict:
    """An event as production saw it at publication: no later labels (supersession, reviews, returns)."""
    r = {k: v for k, v in e.items()}
    r["payload"] = r.get("payload") or {}
    r.setdefault("date_quality", "exact" if r.get("public_at") else "date")
    r.setdefault("country", "US")
    r.update(is_backfill=False, superseded=False, needs_review=False, returns_status="pending")
    return r


class Stager:
    def __init__(self, src: SourceData, guards: Guards):
        self.src, self.guards = src, guards
        self.staged: set[int] = set()
        self.by_time = sorted(src.events, key=lambda e: (e["available_at"], e["id"]))

    def stage_events(self, conn: Connection, now: datetime, event_types: tuple[str, ...] | None = None) -> list[int]:
        new = [e for e in self.by_time if e["id"] not in self.staged and e["available_at"] <= now]
        for e in new:
            conn.execute(events.insert().values(**_row(e)))
            self.staged.add(e["id"])
        return [e["id"] for e in new if event_types is None or e["event_type"] in event_types]

    def stage_reference(self, conn: Connection, today: date) -> None:
        """Symbol periods, the SEC-style ticker registry, security master and corporate actions AS KNOWN ON `today`:
        a period ends at its last observation up to today, the registry maps each issuer to the symbol it used most
        recently up to today (never today's real ticker file), delistings / actions appear only once dated."""
        for t in (tickers, companies, security_symbols, security_master, corporate_actions):
            conn.execute(t.delete())
        rows = []
        for s in self.src.symbols:
            if s["first_seen"] <= today:
                rows.append({**s, "last_seen": min(s["last_seen"], today), "n_obs": 1})
        if rows:
            conn.execute(security_symbols.insert(), rows)
        # registry: latest observed symbol per issuer, if still observed within the grace window; a symbol maps to
        # the issuer that used it most recently
        latest: dict[str, dict] = {}
        for r in rows:
            if r["last_seen"] >= today - timedelta(days=SYMBOL_GRACE_DAYS):
                cur = latest.get(r["cik"])
                if cur is None or r["last_seen"] > cur["last_seen"]:
                    latest[r["cik"]] = r
        by_symbol: dict[str, dict] = {}
        for r in latest.values():
            if r["symbol"] not in by_symbol or r["last_seen"] > by_symbol[r["symbol"]]["last_seen"]:
                by_symbol[r["symbol"]] = r
        for cik in sorted({r["cik"] for r in rows}):
            name = self.src.companies.get(cik, cik)
            conn.execute(companies.insert().values(cik=cik, name=name, name_norm=normalize_company(name) or cik))
        for sym, r in sorted(by_symbol.items()):
            cid = conn.execute(text("SELECT id FROM companies WHERE cik = :c"), {"c": r["cik"]}).scalar()
            conn.execute(tickers.insert().values(symbol=sym, company_id=cid))
        for m in self.src.security_master:
            known = m.get("delisting_date") is not None and m["delisting_date"] <= today
            conn.execute(security_master.insert().values(
                cik=m["cik"], name=m.get("name"), sector=m.get("sector"), status="DELISTED" if known else "UNKNOWN",
                delisting_date=m["delisting_date"] if known else None,
                delisting_reason=m.get("delisting_reason") if known else None))
        acts = [a for a in self.src.corporate_actions if a["action_date"] <= today]
        if acts:
            conn.execute(corporate_actions.insert(), [{**a, "details": a.get("details") or {}} for a in acts])

    def build_universe(self, conn: Connection, today: date, ciks: set[str], price_cap: date) -> int:
        """Point-in-time universe_snapshots row per issuer (what the production gate reads): production formulas -
        shares_as_of (facts FILED on/before today), mcap_bucket, 30-day mean $ volume for MICRO / EARLY_SMALL only -
        on the snapshot's bars up to price_cap."""
        from app.news.classify import mcap_bucket
        n = 0
        reg = dict(conn.execute(text("SELECT c.cik, t.symbol FROM tickers t JOIN companies c ON c.id = t.company_id")).all())
        for cik in sorted(c for c in ciks if c):
            sym = reg.get(cik)
            facts = self.src.share_facts.get(cik) or []
            bars = [b for b in self.src.bars.get(sym or "", []) if b.date <= price_cap and b.close]
            sh = shares_as_of(facts, today)
            if not sym or not bars or sh is None or (today - sh[1].end).days > UNIVERSE_STALE_DAYS:
                continue
            price, shares = bars[-1].close, sh[0]
            mcap = price * shares
            bucket = mcap_bucket(mcap)
            pairs = [(b.close, b.volume) for b in bars[-30:] if b.volume is not None]
            adv = (sum(c * v for c, v in pairs) / len(pairs)) if (bucket in ADV_BUCKETS and pairs) else None
            vals = {"ticker": sym, "name": self.src.companies.get(cik), "price": price, "shares": shares,
                    "shares_end": sh[1].end, "market_cap": mcap, "bucket": bucket, "adv_dollar_30d": adv}
            conn.execute(pg_insert(universe_snapshots).values(as_of=today, cik=cik, **vals)
                         .on_conflict_do_update(index_elements=["as_of", "cik"], set_=vals))
            n += 1
        return n


def identity_check(conn: Connection, ev: dict, on: date) -> str | None:
    """None = the event's security identity holds at the event date; else a REVIEW reason (the event is not traded).
    Point in time: only symbol periods observed up to the simulated date exist in the schema."""
    t, cik = ev.get("ticker"), ev.get("issuer_cik")
    if not t:
        return "NO_TICKER"
    owners = set(conn.execute(text("""SELECT DISTINCT cik FROM security_symbols WHERE symbol = :s AND source <> 'sec_registry'
                                       AND first_seen - :g <= :d AND :d <= last_seen + :g"""),
                              {"s": t, "d": on, "g": SYMBOL_GRACE_DAYS}).scalars())
    if cik:
        if owners and cik not in owners:
            return "TICKER_OWNED_BY_OTHER_ISSUER_AT_EVENT_DATE"
        return None
    if len(owners) == 1:
        return None
    return "AMBIGUOUS_SYMBOL_OWNER" if owners else "NO_ISSUER_IDENTITY"


def check_state(conn: Connection, guards: Guards, now: datetime, today: date, cap: date | None) -> None:
    """Structural leakage audit of the run's schema after every step."""
    q = lambda sql, **p: conn.execute(text(sql), p).scalar()  # noqa: E731
    if q("SELECT count(*) FROM events WHERE available_at > :n", n=now):
        guards.fail("FUTURE_EVENT_VISIBLE", "an event with available_at after the simulated time is in the schema")
    if cap is not None and q("SELECT count(*) FROM market_prices WHERE date > :c", c=cap):
        guards.fail("FUTURE_PRICE_STORED", f"a price bar after {cap} is in the schema")
    if q("SELECT count(*) FROM event_returns"):
        guards.fail("FUTURE_OUTCOME", "event_returns rows exist during the run (outcomes are evaluated after it)")
    for sql, kind in (("SELECT count(*) FROM security_master WHERE delisting_date > :t", "FUTURE_DELISTING"),
                      ("SELECT count(*) FROM corporate_actions WHERE action_date > :t", "FUTURE_CORPORATE_ACTION"),
                      ("SELECT count(*) FROM universe_snapshots WHERE as_of > :t", "FUTURE_UNIVERSE"),
                      ("SELECT count(*) FROM security_symbols WHERE last_seen > :t OR first_seen > :t", "FUTURE_SYMBOL_PERIOD"),
                      ("SELECT count(*) FROM discovery_risks WHERE detected_at::date > :t", "FUTURE_REGULATORY_LABEL"),
                      ("SELECT count(*) FROM thesis_changes WHERE created_at::date > :t", "FUTURE_THESIS_LABEL"),
                      ("SELECT count(*) FROM paper_orders WHERE fill_date > :t", "FUTURE_FILL")):
        if q(sql, t=today):
            guards.fail(kind, sql)
    if q("SELECT count(*) FROM events WHERE needs_review OR superseded"):
        guards.fail("FUTURE_REVIEW_LABEL", "a later review / supersession label reached the replay")
    if q("""SELECT count(*) FROM proposed_trades p JOIN events e ON e.id = p.event_id
            WHERE e.available_at > p.proposal_created_at"""):
        guards.fail("AVAILABLE_AT_VIOLATION", "a proposal was created before its event was available")
    if q("SELECT count(*) FROM paper_orders WHERE status = 'FILLED' AND fill_date < not_before"):
        guards.fail("EARLY_FILL", "an order filled before its first eligible session")


def dump(obj) -> str:
    return json.dumps(obj, default=str, sort_keys=True)
