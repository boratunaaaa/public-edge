"""BIST Volatility-Based Measures System (VBTS / Volatilite Bazlı Tedbir Sistemi) collector V1.

Source (official): Borsa İstanbul's own KAP disclosures ("Pay Piyasasında Volatilite Bazlı Tedbir Sistemi", sender
BORSA İSTANBUL A.Ş.), read from the public disclosure page https://www.kap.org.tr/tr/Bildirim/<index>. The page embeds
the structured disclosure header (publishDate to the second, relatedStocks, summary) and the Turkish decision text.
KAP's list / JSON APIs sit behind a WAF and are NOT used; the official machine-readable route is the MKK API Portal
(needs an account + key). Discovery = the sequential disclosure index (no WAF bypass, 0.5 req/s).

Point in time: available_at = the disclosure's publishDate (Istanbul time, exact). effective_from / effective_to are the
trading days the measure covers (session start / session end) and are never used as the knowledge time.
Identity: the BIST code as of the decision date through the V2 global security master; an unresolved code is kept with
data_quality_state=REVIEW_REQUIRED (a candidate current holder is recorded for the reviewer, never bound).
No trade signal is produced here."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection

PARSER_VERSION = "vbts-v1"
DATASET = "tr_vbts"
IST = ZoneInfo("Europe/Istanbul")
PAGE_URL = "https://www.kap.org.tr/tr/Bildirim/{index}"
WAF_MARKERS = ("150207-PPE13", "Request Rejected", "Access Denied")

MEASURES = ("SHORT_SELL_BAN", "MARGIN_TRADING_BAN", "GROSS_SETTLEMENT", "SINGLE_PRICE", "ORDER_PACKAGE",
            "INTERNET_ORDER_RESTRICTION", "OTHER_VBTS")
# escalation ladder (V1, descriptive): 1 short / margin ban -> 2 gross settlement -> 3 single price / order package /
# internet-order restriction
LEVEL = {"SHORT_SELL_BAN": 1, "MARGIN_TRADING_BAN": 1, "GROSS_SETTLEMENT": 2, "SINGLE_PRICE": 3, "ORDER_PACKAGE": 3,
         "INTERNET_ORDER_RESTRICTION": 3, "OTHER_VBTS": 0}
_PATTERNS = (
    ("SHORT_SELL_BAN", re.compile(r"açığa\s+satış", re.I)),
    ("MARGIN_TRADING_BAN", re.compile(r"kredili\s+işlem", re.I)),
    ("GROSS_SETTLEMENT", re.compile(r"brüt\s+takas", re.I)),
    ("SINGLE_PRICE", re.compile(r"tek\s+fiyat", re.I)),
    ("ORDER_PACKAGE", re.compile(r"emir\s+paketi", re.I)),
    ("INTERNET_ORDER_RESTRICTION", re.compile(r"internet\s+(?:üzerinden|aracılığıyla)?\s*(?:emir|iletil)|internet\s+emir", re.I)),
)
CODE = re.compile(r"\b([A-ZÇĞİÖŞÜ0-9]{3,6})\.E\b")
DATE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
# BIST's standard note about BIST 50 short selling is context, not a measure of the named stock
_BOILERPLATE = re.compile(r"yalnızca BIST 50|VBTS kapsamında getirilen tedbirler, payın işlem gördüğü|^Not:", re.I)


class SourceBlocked(RuntimeError):
    """WAF / bot wall answered instead of the page (never bypassed)."""


class FormatChanged(RuntimeError):
    """The page no longer carries the structure this parser depends on."""


@dataclass
class Decision:
    disclosure_index: int
    disclosure_id: str
    published_at: datetime  # exact, Istanbul
    summary: str
    related_stocks: list[str]
    text_tr: str
    raw_hash: str
    fingerprint: str


@dataclass
class VbtsEvent:
    ticker_at_event: str
    market: str
    measure_type: str
    decision_published_at: datetime
    effective_from: date | None
    effective_to: date | None
    source_url: str
    source_document_id: str
    available_at: datetime
    raw_payload_hash: str
    parser_version: str = PARSER_VERSION
    security_id: str | None = None
    first_seen_at: datetime | None = None
    data_quality_state: str = "OK"  # OK | REVIEW_REQUIRED | DATES_MISSING
    candidate_security_id: str | None = None
    sentence: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def unique_key(self) -> str:
        """Deterministic: document + security (or code when unresolved) + measure + effective_from."""
        ident = self.security_id or f"CODE:{self.ticker_at_event}"
        return f"{self.source_document_id}:{ident}:{self.measure_type}:{self.effective_from or '-'}"


def _unescape(page: str) -> str:
    return page.replace('\\"', '"').replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")


def parse_page(page: str, index: int | None = None) -> Decision | None:
    """Decision when the page is a Borsa İstanbul VBTS disclosure, None for any other disclosure."""
    if any(m in page[:5000] for m in WAF_MARKERS) and "disclosureBasic" not in page:
        raise SourceBlocked(f"KAP answered a block page for {index}")
    t = _unescape(page)
    m = re.search(r'"disclosureBasic":(\{.*?\}),"disclosureDetail"', t)
    if not m:
        raise FormatChanged(f"disclosureBasic not found on page {index}")
    try:
        basic = json.loads(m.group(1))
    except ValueError as e:
        raise FormatChanged(f"disclosureBasic not JSON on page {index}: {e}") from e
    fp = hashlib.sha256("|".join(sorted(basic)).encode()).hexdigest()[:16]
    title = (basic.get("companyTitle") or "").upper()
    summary = basic.get("summary") or ""
    if "BORSA İSTANBUL" not in title or not re.search(r"volatilite\s+bazl", summary, re.I):
        return None
    pd = basic.get("publishDate") or ""
    fmt = "%Y.%m.%d %H:%M:%S" if re.match(r"\d{4}\.", pd) else "%d.%m.%Y %H:%M:%S"
    try:
        published = datetime.strptime(pd, fmt).replace(tzinfo=IST)
    except ValueError as e:
        raise FormatChanged(f"publishDate {pd!r} on page {index}") from e
    # Turkish decision text: the content-tr block (the English block is a translation)
    # the decision is the Turkish "Açıklamalar" text block (a content-tr cell holding a text-block-value)
    blocks = [b for b in re.findall(r'content-tr[^>]*>(.*?)</td>', t, re.S) if 'text-block-value' in b]
    raw = blocks[0] if blocks else ""
    txt = re.sub(r"<[^>]+>", "\n", raw)
    txt = re.sub(r"&nbsp;|&#160;", " ", txt)
    txt = "\n".join(x.strip() for x in txt.split("\n") if x.strip())
    if not txt:
        raise FormatChanged(f"no Turkish decision text on page {index}")
    return Decision(disclosure_index=int(basic.get("disclosureIndex") or index or 0), disclosure_id=basic.get("disclosureId") or "",
                    published_at=published, summary=summary,
                    related_stocks=[x.strip() for x in (basic.get("relatedStocks") or "").split(",") if x.strip()],
                    text_tr=txt, raw_hash=hashlib.sha256(page.encode("utf-8", "replace")).hexdigest(), fingerprint=fp)


def _sentences(txt: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÇĞİÖŞÜ])|\n", txt)
    return [p.strip() for p in parts if p.strip()]


def extract_events(d: Decision, *, first_seen_at: datetime | None = None) -> list[VbtsEvent]:
    """One normalized event per (security code, measure) stated in a decision sentence. A sentence without a
    recognised measure keyword but with a code and a date range becomes OTHER_VBTS (kept, not guessed)."""
    out: list[VbtsEvent] = []
    seen: set[tuple] = set()
    for s in _sentences(d.text_tr):
        if _BOILERPLATE.search(s):
            continue
        codes = CODE.findall(s)
        if not codes:
            continue
        dates = [date(int(y), int(mo), int(dd)) for dd, mo, y in DATE.findall(s)]
        eff_from, eff_to = (dates[0], dates[1]) if len(dates) >= 2 else ((dates[0], None) if dates else (None, None))
        kinds = [k for k, rx in _PATTERNS if rx.search(s)] or ["OTHER_VBTS"]
        for code in dict.fromkeys(codes):
            for k in kinds:
                key = (code, k, eff_from)
                if key in seen:
                    continue
                seen.add(key)
                out.append(VbtsEvent(ticker_at_event=code, market="XIST", measure_type=k, decision_published_at=d.published_at,
                                     effective_from=eff_from, effective_to=eff_to,
                                     source_url=PAGE_URL.format(index=d.disclosure_index),
                                     source_document_id=f"KAP:{d.disclosure_index}", available_at=d.published_at.astimezone(timezone.utc),
                                     raw_payload_hash=d.raw_hash, first_seen_at=first_seen_at,
                                     data_quality_state="OK" if eff_from and eff_to else "DATES_MISSING",
                                     sentence=s[:600]))
    return out


def resolve(conn: Connection, ev: VbtsEvent) -> VbtsEvent:
    """Code as of the decision date via the TR symbol periods. No period covering that date -> REVIEW_REQUIRED (the
    current unique holder is only recorded as a candidate for the reviewer)."""
    from app.markets import master_db
    on = ev.decision_published_at.astimezone(IST).date()
    sid = master_db.resolve(conn, ev.ticker_at_event, on, country="TR")
    if sid:
        ev.security_id = sid
    else:
        ev.candidate_security_id = master_db.resolve(conn, ev.ticker_at_event, date.today(), country="TR")
        ev.data_quality_state = "REVIEW_REQUIRED"
    return ev


def store(conn: Connection, ev: VbtsEvent) -> int | None:
    """Idempotent on the deterministic unique key: None when this (document, security, measure, from) exists."""
    from app.core import records
    payload = {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in asdict(ev).items()}
    return records.put(conn, country="TR", dataset=DATASET, record_key=ev.unique_key[:160], as_of_date=ev.effective_from,
                       available_at=ev.available_at, payload=payload, parser_version=PARSER_VERSION,
                       license_status="PUBLIC_WITH_TERMS", security_id=ev.security_id, entity_name=ev.ticker_at_event,
                       identity_confidence="HIGH" if ev.security_id else None,
                       needs_review=ev.data_quality_state != "OK", source_url=ev.source_url)


# ------------------------------------------------------------------ state / escalation (point in time)

STATE_KEYS = {"SHORT_SELL_BAN": "short_sell_ban", "MARGIN_TRADING_BAN": "margin_trading_ban", "GROSS_SETTLEMENT": "gross_settlement",
              "SINGLE_PRICE": "single_price", "ORDER_PACKAGE": "order_package",
              "INTERNET_ORDER_RESTRICTION": "internet_order_restriction", "OTHER_VBTS": "other_vbts"}


def _known(conn: Connection, security_id: str, as_of: datetime) -> list[dict]:
    rows = conn.execute(text("""SELECT payload FROM dataset_records WHERE dataset = :d AND security_id = :s
                                AND available_at <= :t ORDER BY available_at, id"""),
                        {"d": DATASET, "s": security_id, "t": as_of}).scalars().all()
    return [dict(r) for r in rows]


def get_vbts_state(conn: Connection, security_id: str, as_of: datetime) -> dict:
    """Measures in force on as_of's Istanbul trading day, using ONLY decisions published by as_of (a decision known
    later is never moved back). An open-ended measure (no effective_to) counts from effective_from."""
    day = as_of.astimezone(IST).date()
    state = {v: False for v in STATE_KEYS.values()}
    active = []
    for p in _known(conn, security_id, as_of):
        f = date.fromisoformat(p["effective_from"]) if p.get("effective_from") else None
        t = date.fromisoformat(p["effective_to"]) if p.get("effective_to") else None
        if f and f <= day and (t is None or day <= t):
            state[STATE_KEYS[p["measure_type"]]] = True
            active.append({"measure": p["measure_type"], "from": p["effective_from"], "to": p.get("effective_to"),
                           "document": p["source_document_id"]})
    state["active"] = active
    state["level"] = max((LEVEL[a["measure"]] for a in active), default=0)
    return state


def vbts_escalation(conn: Connection, security_id: str, until: datetime) -> list[dict]:
    """Chronological VBTS_ESCALATION feature rows (no trade signal): each decision known by `until`, the highest level
    it imposes, and whether it escalates over the level in force just before it became public."""
    out = []
    for p in _known(conn, security_id, until):
        at = datetime.fromisoformat(p["available_at"])
        before = get_vbts_state(conn, security_id, at - timedelta(seconds=1))["level"]
        lvl = LEVEL[p["measure_type"]]
        if out and out[-1]["document"] == p["source_document_id"]:
            out[-1]["measures"].append(p["measure_type"])
            out[-1]["level"] = max(out[-1]["level"], lvl)
            out[-1]["escalation"] = out[-1]["level"] > out[-1]["level_before"]
            continue
        out.append({"available_at": p["available_at"], "document": p["source_document_id"], "measures": [p["measure_type"]],
                    "level": lvl, "level_before": before, "escalation": lvl > before})
    return out


# ------------------------------------------------------------------ collector (scan) + source health

def collect_range(engine, start: int, end: int, fetch, *, first_seen_at: datetime | None = None, max_pages: int | None = None,
                  store_raw: bool = False) -> dict:
    """Scan KAP disclosure indexes [start, end]. fetch(index) -> page text, '' when the index does not exist.
    Raises SourceBlocked / FormatChanged (the caller records BLOCKED / SCHEMA_CHANGED - never 'no VBTS today').
    A few individually broken pages are tolerated and counted; a run where most pages lack the structure raises."""
    st = {"scanned": 0, "missing": 0, "unparseable": 0, "documents": 0, "events": 0, "stored": 0, "duplicates": 0,
          "review_required": 0, "last_index": start - 1, "fingerprint": None, "missing_indexes": [], "unparseable_indexes": []}
    for i in range(start, end + 1):
        if max_pages is not None and st["scanned"] >= max_pages:
            break
        page = fetch(i)
        st["scanned"] += 1
        st["last_index"] = i
        if not page:
            st["missing"] += 1
            st["missing_indexes"].append(i)
            continue
        try:
            d = parse_page(page, i)
        except FormatChanged:
            st["unparseable"] += 1
            st["unparseable_indexes"].append(i)
            if st["scanned"] >= 20 and st["unparseable"] > 0.5 * st["scanned"]:
                raise
            continue
        if d is None:
            continue
        st["documents"] += 1
        st["fingerprint"] = d.fingerprint
        evs = extract_events(d, first_seen_at=first_seen_at)
        with engine.begin() as c:
            raw_id = None
            if store_raw:
                from app.core import rawlake
                raw_id = rawlake.store(c, country="TR", source="tr_vbts", doc_key=f"KAP:{d.disclosure_index}", content=page,
                                       url=PAGE_URL.format(index=d.disclosure_index), content_type="text/html",
                                       parser_version=PARSER_VERSION, retention="EVIDENCE")
            for ev in evs:
                resolve(c, ev)
                ev.extra["raw_document_id"] = raw_id
                st["events"] += 1
                st["review_required"] += ev.data_quality_state == "REVIEW_REQUIRED"
                if store(c, ev) is None:
                    st["duplicates"] += 1
                else:
                    st["stored"] += 1
    return st


class VbtsSource:
    """Live collector (V2 Source interface). NOT registered in app.sources.registry in V1: enabling it is a separate,
    reviewed step (it reads ~1,000 KAP pages a day at 0.5 req/s)."""
    name = "tr_vbts"
    interval_seconds = 1800
    critical = False
    slow = True
    enabled = True
    disabled_reason = None
    country, dataset, license_status, parser_version = "TR", DATASET, "PUBLIC_WITH_TERMS", PARSER_VERSION
    stale_after = timedelta(days=3)
    MEMBER_PAGE = "https://www.kap.org.tr/tr/sirket-bildirimleri/4028e4a14bcf2a06014be4d7e6e256b6"  # BORSA İSTANBUL A.Ş.

    def __init__(self, fetch=None, latest_index=None, max_pages: int = 600):
        self._fetch, self._latest, self.max_pages = fetch, latest_index, max_pages

    def fetch(self, i: int) -> str:
        if self._fetch:
            return self._fetch(i)
        from app import http
        try:
            return http.get(PAGE_URL.format(index=i), retries=2).text
        except http.HttpError as e:
            if e.status == 404:
                return ""
            raise

    def latest_index(self) -> int:
        if self._latest:
            return self._latest()
        from app import http
        m = re.search(r'disclosureIndex\\?":(\d+)', http.get(self.MEMBER_PAGE).text)
        if not m:
            raise FormatChanged("Borsa İstanbul member page carries no disclosureIndex")
        return int(m.group(1))

    def run_live(self, engine):
        """LIVE_INCREMENTAL only (the worker interface). Failures raise so the V2 health layer records them:
        429 -> API_QUOTA (DEGRADED), unavailable -> ERROR, layout change -> SCHEMA_CHANGED; 'no new VBTS' is a success."""
        from app.sources.base import RunResult
        col = LiveVbtsCollector(engine, fetch=self._fetch or self.fetch, latest_index=self._latest or self.latest_index)
        out = col.poll()
        o = out["outcome"]
        if o == "SOURCE_RATE_LIMITED":
            raise RateLimited("KAP answered 429 - backing off, watermark kept")
        if o == "SOURCE_UNAVAILABLE":
            raise Unavailable("KAP unavailable - watermark kept")
        if o == "SOURCE_SCHEMA_CHANGED":
            raise FormatChanged("KAP disclosure structure changed")
        if o == "SOURCE_GAP":
            raise Unavailable(f"live gap {out.get('gap')} indexes > live_max_gap: run recover_live_gap() explicitly")
        if o == "SOURCE_PARSE_ERROR":  # pages were read but could not be parsed: never a quiet "0 events" day
            raise SourceParseError(f"{out.get('parse_errors')} disclosure page(s) could not be parsed (kept for re-probe)")
        if o == "BACKING_OFF":  # no request was made: no source observation is recorded at all
            return RunResult(items=0, events=[], skipped=True, watermark={"outcome": o, "until": out.get("until")})
        # events carries the COUNT of newly stored measure records (the worker only uses its length)
        return RunResult(items=out.get("scanned", 0), events=[0] * out.get("events", 0), watermark={"outcome": o, **out})


# ================================================================== LIVE_INCREMENTAL mode (V1.1)
"""Two explicit modes:
  HISTORICAL        collect_range(start, end): an operator-chosen index window (pilot / backfill). Never automatic.
  LIVE_INCREMENTAL  LiveVbtsCollector.poll(): only indexes above the checkpoint, up to Borsa İstanbul's latest disclosure
                    index (1 request to the member page), plus a small overlap of recently MISSING / UNPARSED indexes
                    (late or out-of-order publication). A gap wider than live_max_gap is never scanned automatically:
                    the collector reports SOURCE_GAP and waits for an explicit HISTORICAL run.

Checkpoint = job_state "tr_vbts_live", updated in the SAME transaction as the page's stored records, so a restart at
any point neither loses nor duplicates a document (records are unique on their deterministic key anyway).
KAP index monotonicity is NOT assumed: an index that was empty / 404 / unparseable is re-probed while it is younger
than late_window_hours; seen VBTS documents are skipped by id."""
import os as _os
import time as _time

LIVE_JOB = "tr_vbts_live"
OUTCOMES = ("NEW_EVENTS", "NO_NEW_EVENT", "SOURCE_RATE_LIMITED", "SOURCE_UNAVAILABLE", "SOURCE_PARSE_ERROR",
            "SOURCE_SCHEMA_CHANGED", "SOURCE_GAP", "BACKING_OFF")


def _env_f(name: str, default: float) -> float:
    try:
        return float(_os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class LiveConfig:
    min_spacing_s: float = field(default_factory=lambda: _env_f("PE_VBTS_MIN_SPACING_S", 6.0))
    backoff_429_s: float = field(default_factory=lambda: _env_f("PE_VBTS_429_BACKOFF_S", 120.0))
    backoff_max_s: float = field(default_factory=lambda: _env_f("PE_VBTS_BACKOFF_MAX_S", 3600.0))
    first_lookback: int = field(default_factory=lambda: int(_env_f("PE_VBTS_FIRST_LOOKBACK", 30)))
    live_max_gap: int = field(default_factory=lambda: int(_env_f("PE_VBTS_LIVE_MAX_GAP", 3000)))
    max_pages_per_poll: int = field(default_factory=lambda: int(_env_f("PE_VBTS_MAX_PAGES_PER_POLL", 300)))
    late_window_hours: float = field(default_factory=lambda: _env_f("PE_VBTS_LATE_WINDOW_HOURS", 6.0))
    max_parse_retries: int = field(default_factory=lambda: int(_env_f("PE_VBTS_MAX_PARSE_RETRIES", 5)))
    # poll cadence (Istanbul time): decisions come after the close
    poll_decision_window_s: float = field(default_factory=lambda: _env_f("PE_VBTS_POLL_DECISION_S", 600))
    poll_market_s: float = field(default_factory=lambda: _env_f("PE_VBTS_POLL_MARKET_S", 1800))
    poll_off_s: float = field(default_factory=lambda: _env_f("PE_VBTS_POLL_OFF_S", 7200))


class RateLimited(RuntimeError):
    status = 429


class SourceParseError(RuntimeError):
    """Some pages were read but could not be parsed (worker health: DEGRADED, never TRUE_NO_EVENT)."""


class Unavailable(RuntimeError):
    pass


def poll_interval(now: datetime, cfg: LiveConfig | None = None) -> float:
    """10 min in the post-close decision window (17:30-21:00 on BIST trading days), 30 min in session, 2 h otherwise."""
    from app.markets.calendars import calendar_for
    cfg = cfg or LiveConfig()
    cal = calendar_for("XIST")
    loc = now.astimezone(IST)
    if not cal.is_trading_day(loc.date()):
        return cfg.poll_off_s
    hm = loc.hour * 60 + loc.minute
    if 17 * 60 + 30 <= hm <= 21 * 60:
        return cfg.poll_decision_window_s
    if 10 * 60 <= hm < 17 * 60 + 30:
        return cfg.poll_market_s
    return cfg.poll_off_s


def _day_metrics(st: dict, now: datetime) -> dict:
    d = now.date().isoformat()
    m = st.get("metrics") or {}
    if m.get("day") != d:
        m = {"day": d, "requests": 0, "http_429": 0, "new_documents": 0, "events": 0, "parse_errors": 0}
    st["metrics"] = m
    return m


class LiveVbtsCollector:
    def __init__(self, engine, fetch=None, latest_index=None, cfg: LiveConfig | None = None, clock=None, sleep=None):
        self.engine, self.cfg = engine, cfg or LiveConfig()
        self._fetch, self._latest = fetch, latest_index
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or _time.sleep
        self._last_req = 0.0

    # ---------------------------------------------------------------- I/O (spacing + error mapping)
    def _space(self) -> None:
        w = self.cfg.min_spacing_s - (_time.monotonic() - self._last_req)
        if w > 0:
            self.sleep(w)
        self._last_req = _time.monotonic()

    def _get(self, fn, *a):
        self._space()
        try:
            return fn(*a)
        except (RateLimited, Unavailable, SourceBlocked, FormatChanged):
            raise
        except Exception as e:  # noqa: BLE001 - map transport / HTTP errors to explicit outcomes
            status = getattr(e, "status", None)
            if status == 429:
                raise RateLimited(str(e)) from e
            raise Unavailable(f"{type(e).__name__}: {e}") from e

    def fetch(self, i: int) -> str:
        if self._fetch:
            return self._get(self._fetch, i)
        return self._get(VbtsSource().fetch, i)

    def latest_index(self) -> int:
        if self._latest:
            return self._get(self._latest)
        return self._get(VbtsSource().latest_index)

    # ---------------------------------------------------------------- checkpoint
    def load(self) -> dict:
        from app.db.store import job_state
        with self.engine.connect() as c:
            return job_state(c, LIVE_JOB)

    @staticmethod
    def _save(c, st: dict) -> None:
        from app.db.store import save_job_state
        save_job_state(c, LIVE_JOB, st)

    # ---------------------------------------------------------------- one poll
    def poll(self) -> dict:
        now = self.clock()
        st = self.load()
        m = _day_metrics(st, now)
        st["last_attempt_at"] = now.isoformat()
        if st.get("backoff_until") and now < datetime.fromisoformat(st["backoff_until"]):
            return {"outcome": "BACKING_OFF", "until": st["backoff_until"]}
        out = {"outcome": "NO_NEW_EVENT", "new_documents": 0, "events": 0, "scanned": 0, "reprobed": 0, "parse_errors": 0}
        try:
            m["requests"] += 1
            top = self.latest_index()
            nxt = st.get("next_index")
            if nxt is None:
                nxt = top - self.cfg.first_lookback + 1  # first poll: a small window only, never history
            if top - nxt + 1 > self.cfg.live_max_gap:
                out["outcome"] = "SOURCE_GAP"
                out["gap"] = top - nxt + 1
                st["source_status"] = "SOURCE_GAP"
                with self.engine.begin() as c:
                    self._save(c, st)
                return out
            # late / out-of-order: re-probe recently missing or unparsed indexes below the watermark
            retry = sorted(set(map(int, (st.get("pending_missing") or {}).keys())) | set(map(int, (st.get("pending_parse") or {}).keys())))
            # re-probes never starve new indexes: at most a third of the page budget per poll (oldest first)
            k = max(1, self.cfg.max_pages_per_poll // 3)
            if len(retry) > k:  # round-robin, so every pending index gets its turn (and can expire)
                cur = int(st.get("retry_cursor") or 0) % len(retry)
                retry = (retry[cur:] + retry[:cur])[:k]
                st["retry_cursor"] = cur + k
            else:
                st["retry_cursor"] = 0
            todo = retry + list(range(nxt, top + 1))
            for i in todo[: self.cfg.max_pages_per_poll]:
                self._one(i, st, m, out, now, is_retry=i < nxt)
            st["last_successful_poll_at"] = now.isoformat()
            st["consecutive_failures"] = 0
            st.pop("backoff_until", None)
            if out["parse_errors"]:
                out["outcome"] = "SOURCE_PARSE_ERROR"
            elif out["events"]:
                out["outcome"] = "NEW_EVENTS"
            st["source_status"] = out["outcome"]
            with self.engine.begin() as c:
                self._save(c, st)
            return out
        except RateLimited:
            m["http_429"] += 1
            return self._fail(st, now, "SOURCE_RATE_LIMITED", out, backoff=True)
        except SourceBlocked:
            return self._fail(st, now, "SOURCE_UNAVAILABLE", out, backoff=True, status="BLOCKED")
        except Unavailable:
            return self._fail(st, now, "SOURCE_UNAVAILABLE", out, backoff=False)
        except FormatChanged:
            return self._fail(st, now, "SOURCE_SCHEMA_CHANGED", out, backoff=False)

    def _fail(self, st, now, outcome, out, backoff: bool, status: str | None = None) -> dict:
        n = int(st.get("consecutive_failures") or 0) + 1
        st["consecutive_failures"] = n
        st["source_status"] = status or outcome
        if backoff:  # exponential, capped; the watermark does not move
            wait = min(self.cfg.backoff_429_s * (2 ** (n - 1)), self.cfg.backoff_max_s)
            st["backoff_until"] = (now + timedelta(seconds=wait)).isoformat()
        with self.engine.begin() as c:
            self._save(c, st)
        out["outcome"] = outcome
        return out

    def _one(self, i: int, st: dict, m: dict, out: dict, now: datetime, is_retry: bool) -> None:
        """Fetch + store one index; checkpoint moves in the same transaction as the page's records."""
        seen = set(st.get("seen_docs") or [])
        if i in seen:
            self._advance(st, i, is_retry, None)
            return
        m["requests"] += 1
        page = self.fetch(i)  # RateLimited / Unavailable propagate: nothing of this index is committed
        out["scanned"] += 1
        out["reprobed"] += is_retry
        pm = dict(st.get("pending_missing") or {})
        pp = dict(st.get("pending_parse") or {})
        key = str(i)
        if not page:
            pp.pop(key, None)  # an unparseable page that is now missing becomes a timestamped missing entry
            first = pm.setdefault(key, now.isoformat())
            if now - datetime.fromisoformat(first) > timedelta(hours=self.cfg.late_window_hours):
                pm.pop(key, None)  # missing for longer than the late window: given up (never a guessed event)
            st["pending_missing"], st["pending_parse"] = pm, pp
            with self.engine.begin() as c:
                self._advance(st, i, is_retry, None)
                self._save(c, st)
            return
        try:
            d = parse_page(page, i)
        except FormatChanged:
            m["parse_errors"] += 1
            out["parse_errors"] += 1
            pp[key] = int(pp.get(key, 0)) + 1
            if pp[key] > self.cfg.max_parse_retries:
                pp.pop(key)
                st.setdefault("parse_failed", []).append(i)  # kept for review, never silently dropped
            st["pending_parse"] = pp
            pm.pop(key, None)
            st["pending_missing"] = pm
            with self.engine.begin() as c:
                self._advance(st, i, is_retry, None)
                self._save(c, st)
            return
        pm.pop(key, None)
        pp.pop(key, None)
        st["pending_missing"], st["pending_parse"] = pm, pp
        with self.engine.begin() as c:
            if d is not None:
                raw_id = None
                try:
                    from app.core import rawlake
                    raw_id = rawlake.store(c, country="TR", source="tr_vbts", doc_key=f"KAP:{d.disclosure_index}", content=page,
                                           url=PAGE_URL.format(index=d.disclosure_index), content_type="text/html",
                                           parser_version=PARSER_VERSION, retention="EVIDENCE")
                except OSError:
                    raw_id = None  # raw lake unavailable (dev): the record keeps its raw hash
                new = 0
                for ev in extract_events(d, first_seen_at=now):
                    resolve(c, ev)
                    ev.extra["raw_document_id"] = raw_id
                    if store(c, ev) is not None:
                        new += 1
                if new:
                    m["new_documents"] += 1
                    m["events"] += new
                    out["new_documents"] += 1
                    out["events"] += new
                st["seen_docs"] = sorted(set(st.get("seen_docs") or []) | {i})[-500:]
                st["last_seen_document_id"] = f"KAP:{d.disclosure_index}"
                prev = st.get("last_seen_publication_time")
                if not prev or d.published_at.isoformat() > prev:
                    st["last_seen_publication_time"] = d.published_at.isoformat()
            self._advance(st, i, is_retry, None)
            self._save(c, st)

    @staticmethod
    def _advance(st: dict, i: int, is_retry: bool, _):
        if not is_retry:
            st["next_index"] = max(int(st.get("next_index") or 0), i + 1)


def vbts_metrics(conn) -> dict:
    """Observability: vbts_last_successful_poll, vbts_last_seen_document, vbts_requests_today, vbts_429_count,
    vbts_new_documents_today, vbts_events_today, vbts_parse_errors_today (+ status, pending re-probes)."""
    from app.db.store import job_state
    st = job_state(conn, LIVE_JOB)
    m = st.get("metrics") or {}
    today = m.get("day") == datetime.now(timezone.utc).date().isoformat()
    g = (lambda k: m.get(k, 0) if today else 0)
    return {"vbts_last_successful_poll": st.get("last_successful_poll_at"), "vbts_last_seen_document": st.get("last_seen_document_id"),
            "vbts_last_seen_publication_time": st.get("last_seen_publication_time"), "vbts_requests_today": g("requests"),
            "vbts_429_count": g("http_429"), "vbts_new_documents_today": g("new_documents"), "vbts_events_today": g("events"),
            "vbts_parse_errors_today": g("parse_errors"), "vbts_source_status": st.get("source_status"),
            "vbts_consecutive_failures": st.get("consecutive_failures", 0), "vbts_next_index": st.get("next_index"),
            "vbts_pending_reprobes": len(st.get("pending_missing") or {}) + len(st.get("pending_parse") or {})}


def recover_live_gap(engine, fetch=None, *, latest_index: int | None = None, clock=None) -> dict:
    """Explicit operator step for SOURCE_GAP (never called by the live poll): scan the gap [live next_index, latest] in
    HISTORICAL mode, verify it was covered completely (every index read, no schema change, last index reached), then -
    and only then - move the live checkpoint to latest + 1. Missing / unparseable indexes of the gap are handed to the
    live re-probe sets (with the recovery time), so a late document inside the gap is still picked up."""
    from app.db.store import job_state, save_job_state
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if fetch is None:  # the same spaced, error-mapped fetch as the live collector (6 s, 429 -> RateLimited)
        fetch = LiveVbtsCollector(engine).fetch
    with engine.connect() as c:
        st = job_state(c, LIVE_JOB)
    start = st.get("next_index")
    if start is None:
        raise ValueError("no live checkpoint yet: nothing to recover")
    end = latest_index if latest_index is not None else VbtsSource().latest_index()
    rep = collect_range(engine, start, end, fetch, first_seen_at=now)  # HISTORICAL: raises on block / schema change
    covered = rep["last_index"] == end and rep["scanned"] == end - start + 1
    if not covered:
        return {"recovered": False, "reason": "gap not fully scanned", **rep}
    with engine.begin() as c:
        cur = job_state(c, LIVE_JOB)
        if cur.get("next_index") != start:  # the live poll moved meanwhile: do not overwrite it
            return {"recovered": False, "reason": "live checkpoint changed during recovery", **rep}
        pm = dict(cur.get("pending_missing") or {})
        pm.update({str(i): now.isoformat() for i in rep["missing_indexes"]})
        pp = dict(cur.get("pending_parse") or {})
        pp.update({str(i): 1 for i in rep["unparseable_indexes"]})
        cur.update(next_index=end + 1, pending_missing=pm, pending_parse=pp, source_status="RECOVERED_GAP",
                   last_gap_recovery={"from": start, "to": end, "at": now.isoformat(), "stored": rep["stored"]})
        save_job_state(c, LIVE_JOB, cur)
    return {"recovered": True, "from": start, "to": end, **rep}
