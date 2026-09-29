"""Point-in-time visibility: what the run's schema may contain at the simulated moment, the capped price provider, the
point-in-time identity check and the leakage guards."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import and_, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app import http
from app.core import clock
from app.db.models import (companies, corporate_actions, discovery_risks, events, security_master, security_symbols,
                           tickers, universe_snapshots)
from app.entities.names import normalize_company
from app.marketdata.provider import Bar, MarketDataProvider, shares_as_of
from app.replay.errors import ReplayLeakage
from app.replay.source import BENCHMARK_SERIES, SourceData

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


class PriceIndex:
    """price identity = issuer + valid symbol period + date. A requested symbol is resolved to the issuer that used it
    on the request's anchor date (symbol periods observed up to the simulated date only), then served from ONE stored
    series of that issuer (catalog: SourceData.series), restricted to that symbol period. So a renamed security
    (SSNT in 2021, stored by the vendor under today's QXO) is found, and a reused ticker never splices two issuers."""

    def __init__(self, src: SourceData):
        self.src = src
        self.periods: dict[str, list[dict]] = defaultdict(list)
        for r in src.symbols:
            if r.get("source") != "sec_registry":
                self.periods[r["symbol"]].append(r)
        from app.replay.source import derive_series
        self.catalog: dict[str, list[dict]] = defaultdict(list)
        explicit = {(x["cik"], x["series"]) for x in src.series}
        for x in list(src.series) + [d for d in derive_series(src.symbols, src.bars) if (d["cik"], d["series"]) not in explicit]:
            self.catalog[x["cik"]].append(x)

    def owner(self, symbol: str, on: date, today: date) -> tuple[str | None, list[dict]]:
        g = timedelta(days=SYMBOL_GRACE_DAYS)
        rows = [r for r in self.periods.get(symbol, []) if r["first_seen"] <= today]
        hits = {r["cik"] for r in rows if r["first_seen"] - g <= on <= min(r["last_seen"], today) + g}
        if len(hits) != 1:
            return None, []  # unknown or ambiguous owner: no price rather than a guess
        cik = next(iter(hits))
        return cik, [r for r in rows if r["cik"] == cik]

    def bars(self, symbol: str, start: date, end: date, today: date) -> list[Bar] | None:
        if symbol in BENCHMARK_SERIES:
            b = self.src.bars.get(symbol)
            return None if b is None else [x for x in b if start <= x.date <= end]
        cik, rows = self.owner(symbol, min(end, today), today)
        if cik is None:
            return None
        g = timedelta(days=SYMBOL_GRACE_DAYS)
        lo = min(r["first_seen"] for r in rows) - g
        hi = max(min(r["last_seen"], today) for r in rows) + g
        # the symbol's own series first, then the issuer's other series (e.g. the vendor's current symbol)
        for x in sorted(self.catalog.get(cik, []), key=lambda x: (x["series"] != symbol, x["series"])):
            got = [b for b in self.src.bars.get(x["series"], [])
                   if start <= b.date <= end and lo <= b.date <= hi
                   and (x.get("valid_from") is None or b.date >= x["valid_from"])
                   and (x.get("valid_to") is None or b.date <= x["valid_to"])]
            if got:
                return got  # ONE series: never a mix of adjustment bases
        return []


class ReplayPriceProvider(MarketDataProvider):
    """Serves the input snapshot through the PriceIndex. Never returns a bar after the visibility cap; a request that
    reaches past the simulated DATE is itself a leak (no production path asks for tomorrow) and fails the run."""
    name = "replay"

    def __init__(self, src: SourceData, vis: Visibility, guards: Guards, index: PriceIndex | None = None):
        self.src, self.vis, self.guards = src, vis, guards
        self.index = index or PriceIndex(src)
        self.requests = 0

    def get_historical_prices(self, symbol: str, start: date, end: date) -> list[Bar]:
        self.requests += 1
        if end > clock.today():
            self.guards.fail("FUTURE_PRICE_REQUEST", f"{symbol}: requested up to {end}, simulated date {clock.today()}")
        bars = self.index.bars(symbol, start, end, clock.today())
        if bars is None:
            raise http.HttpError(f"replay://{symbol}", 404, "no issuer / series for this symbol at this date")
        cap = self.vis.cap
        return [b for b in bars if cap is None or b.date <= cap]


def _row(e: dict) -> dict:
    """An event as production saw it at publication: no later labels (supersession, reviews, returns)."""
    r = {k: v for k, v in e.items()}
    r["payload"] = r.get("payload") or {}
    r.setdefault("date_quality", "exact" if r.get("public_at") else "date")
    r.setdefault("country", "US")
    r.update(is_backfill=False, superseded=False, needs_review=False, returns_status="pending")
    return r


class Stager:
    """Stages what became knowable. Reference data is INCREMENTAL: it changes only when the simulated date changes, and
    only the rows whose point-in-time value changed are written (a newly observed symbol period, a period's last
    observation moving forward, a registry entry that changed owner, a delisting that became public, a dated corporate
    action) - the result equals a full rebuild at that date (tested)."""

    def __init__(self, src: SourceData, guards: Guards, index: PriceIndex | None = None):
        self.src, self.guards = src, guards
        self.index = index or PriceIndex(src)
        self.staged: set[int] = set()
        self.by_time = sorted(src.events, key=lambda e: (e["available_at"], e["id"]))
        self.ref_day: date | None = None
        self._sym: dict[tuple, date] = {}  # (cik, symbol, source) -> staged last_seen
        self._reg: dict[str, str] = {}  # symbol -> cik
        self._cos: set[str] = set()
        self._sm: dict[str, date | None] = {}  # cik -> staged delisting_date
        self._acts: set[tuple] = set()
        self._risk_rows: dict[tuple, dict] = {}
        self.risk_provenance: dict[tuple, list] = {}
        self.writes = 0

    def stage_events(self, conn: Connection, now: datetime, event_types: tuple[str, ...] | None = None) -> list[int]:
        new = [e for e in self.by_time if e["id"] not in self.staged and e["available_at"] <= now]
        for e in new:
            conn.execute(events.insert().values(**_row(e)))
            self.staged.add(e["id"])
        return [e["id"] for e in new if event_types is None or e["event_type"] in event_types]

    def stage_risks(self, conn: Connection, now: datetime, today: date) -> int:
        """discovery_risks exactly as production would hold them at `now`, from the reconstructed labels public by then
        (risk_history.active_risks: production severity / escalation rules) plus HIGH_FINANCING_RISK from the production
        financing rules on those point-in-time inputs. Written as a diff; every row keeps its evidence
        (self.risk_provenance) - a row without it fails the run (check_state)."""
        from app.replay.risk_history import active_risks
        want: dict[tuple, dict] = {}
        for cik in sorted({x.cik for x in self.src.risk_labels}):
            rows = active_risks(self.src.risk_labels, cik, now)
            fin = self._financing(conn, cik, rows, now)
            if fin:
                rows["HIGH_FINANCING_RISK"] = fin
            for risk, r in rows.items():
                want[(cik, risk)] = r
        w = 0
        for key in sorted(set(self._risk_rows) - set(want)):
            conn.execute(discovery_risks.delete().where(and_(discovery_risks.c.cik == key[0], discovery_risks.c.risk == key[1])))
            del self._risk_rows[key]
            w += 1
        for key, r in sorted(want.items()):
            row = {k: r[k] for k in ("severity", "value", "source_url", "detected_at")}
            if self._risk_rows.get(key) == row:
                continue
            conn.execute(pg_insert(discovery_risks).values(cik=key[0], risk=key[1], **row)
                         .on_conflict_do_update(index_elements=["cik", "risk"], set_=row))
            self._risk_rows[key] = row
            w += 1
        self.risk_provenance = {k: want[k]["evidence"] for k in want}
        return w

    def _financing(self, conn: Connection, cik: str, rows: dict, now: datetime) -> dict | None:
        """Production financing.financing_flags / financing_score / is_high on point-in-time inputs (clock-aware
        corporate-action windows, staged risks, dilution from its label; runway stays unknown)."""
        from app.discovery.financing import SUB_BILLION, financing_flags, financing_score, is_high
        mcap = conn.execute(text("""SELECT market_cap FROM universe_snapshots WHERE cik = :c AND as_of <= :t
                                    ORDER BY as_of DESC LIMIT 1"""), {"c": cik, "t": now.date()}).scalar()
        if mcap is not None and mcap >= SUB_BILLION:
            return None
        dil = rows.get("DILUTION")
        metrics = {"share_growth_yoy": (0.5 if dil["severity"] == "HIGH" else 0.2) if dil else None, "runway_months": None}
        flags = financing_flags(conn, cik, metrics, [{"risk": k, "severity": v["severity"]} for k, v in rows.items()])
        score = financing_score(flags)
        if not is_high(flags, score):
            return None
        ev = sorted({d for v in rows.values() for d in v["evidence"]} | {f"flag:{f}" for f in flags})
        return {"severity": "HIGH", "value": f"score {score}/10: {', '.join(flags)}", "source_url": "derived:financing_flags",
                "detected_at": max([v["detected_at"] for v in rows.values()] or [now]), "evidence": ev}

    def desired_reference(self, today: date) -> dict:
        """The point-in-time reference state on `today` (pure; used by the incremental stager and by the tests)."""
        sym = {}
        for s in self.src.symbols:
            if s["first_seen"] <= today:
                sym[(s["cik"], s["symbol"], s["source"])] = (s["first_seen"], min(s["last_seen"], today))
        latest: dict[str, tuple] = {}
        for (cik, symbol, _src), (_fs, last) in sym.items():
            if last >= today - timedelta(days=SYMBOL_GRACE_DAYS) and (cik not in latest or last > latest[cik][1]):
                latest[cik] = (symbol, last)
        reg: dict[str, tuple] = {}
        for cik, (symbol, last) in latest.items():
            if symbol not in reg or last > reg[symbol][1]:
                reg[symbol] = (cik, last)
        return {"symbols": sym, "registry": {k: v[0] for k, v in reg.items()}, "companies": {k[0] for k in sym},
                "delistings": {m["cik"]: (m["delisting_date"] if m.get("delisting_date") and m["delisting_date"] <= today
                                          else None) for m in self.src.security_master},
                "actions": {(a["cik"], a["action"], a.get("accession") or "", a["action_date"])
                            for a in self.src.corporate_actions if a["action_date"] <= today}}

    def stage_reference(self, conn: Connection, today: date) -> int:
        if self.ref_day == today:
            return 0
        if self.ref_day is not None and today < self.ref_day:
            raise ValueError("reference data cannot move backwards")
        want = self.desired_reference(today)
        w = 0
        for key, (fs, last) in want["symbols"].items():
            cik, symbol, src = key
            if key not in self._sym:
                conn.execute(security_symbols.insert().values(cik=cik, symbol=symbol, source=src, first_seen=fs,
                                                              last_seen=last, n_obs=1))
            elif self._sym[key] != last:
                conn.execute(text("""UPDATE security_symbols SET last_seen = :l WHERE cik = :c AND symbol = :s
                                     AND source = :src"""), {"l": last, "c": cik, "s": symbol, "src": src})
            else:
                continue
            self._sym[key] = last
            w += 1
        for cik in sorted(want["companies"] - self._cos):
            name = self.src.companies.get(cik, cik)
            conn.execute(companies.insert().values(cik=cik, name=name, name_norm=normalize_company(name) or cik))
            self._cos.add(cik)
            w += 1
        for symbol in sorted(set(self._reg) | set(want["registry"])):
            new = want["registry"].get(symbol)
            if self._reg.get(symbol) == new:
                continue
            conn.execute(tickers.delete().where(tickers.c.symbol == symbol))
            if new is not None:
                cid = conn.execute(text("SELECT id FROM companies WHERE cik = :c"), {"c": new}).scalar()
                conn.execute(tickers.insert().values(symbol=symbol, company_id=cid))
                self._reg[symbol] = new
            else:
                self._reg.pop(symbol, None)
            w += 1
        for m in self.src.security_master:
            known = want["delistings"].get(m["cik"])
            if m["cik"] in self._sm and self._sm[m["cik"]] == known:
                continue
            vals = {"name": m.get("name"), "sector": m.get("sector"), "status": "DELISTED" if known else "UNKNOWN",
                    "delisting_date": known, "delisting_reason": m.get("delisting_reason") if known else None}
            conn.execute(pg_insert(security_master).values(cik=m["cik"], **vals)
                         .on_conflict_do_update(index_elements=["cik"], set_=vals))
            self._sm[m["cik"]] = known
            w += 1
        for a in self.src.corporate_actions:
            key = (a["cik"], a["action"], a.get("accession") or "", a["action_date"])
            if key in want["actions"] and key not in self._acts:
                conn.execute(corporate_actions.insert().values(cik=a["cik"], action=a["action"], action_date=a["action_date"],
                                                               accession=a.get("accession") or "", source=a.get("source") or "",
                                                               details=a.get("details") or {}))
                self._acts.add(key)
                w += 1
        self.ref_day = today
        self.writes += w
        return w

    def build_universe(self, conn: Connection, today: date, ciks: set[str], price_cap: date) -> int:
        """Point-in-time universe_snapshots row per issuer (what the production gate reads): production formulas -
        shares_as_of (facts FILED on/before today), mcap_bucket, 30-day mean $ volume for MICRO / EARLY_SMALL like
        build_universe (the gate measures every other class itself from prior sessions) - on the issuer's own price
        series (PriceIndex) up to price_cap."""
        from app.news.classify import mcap_bucket
        n = 0
        reg = dict(conn.execute(text("SELECT c.cik, t.symbol FROM tickers t JOIN companies c ON c.id = t.company_id")).all())
        for cik in sorted(c for c in ciks if c):
            sym = reg.get(cik)
            facts = self.src.share_facts.get(cik) or []
            got = self.index.bars(sym, price_cap - timedelta(days=60), price_cap, today) if sym else None
            bars = [b for b in (got or []) if b.close]
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


def check_state(conn: Connection, guards: Guards, now: datetime, today: date, cap: date | None,
                risk_provenance: dict | None = None) -> None:
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
    for cik, risk in conn.execute(text("SELECT cik, risk FROM discovery_risks")).all():
        if not (risk_provenance or {}).get((cik, risk)):
            guards.fail("RISK_WITHOUT_PROVENANCE", f"{cik} {risk}: no reconstructed source document behind it")


def dump(obj) -> str:
    return json.dumps(obj, default=str, sort_keys=True)
