"""Point-in-time reconstruction of the production risk labels (replay-only; never written to production tables).

Every reconstructed label is derived from ONE public source document with the production rule of that label, and is
known only from the document's public time on:

    historical public document -> versioned parser/profile -> production rule (detector) -> label
    available_at = the document's acceptance / public time (never a period end, never a later enforcement or status)

Families (production consumers):
  GOING_CONCERN      gate SEVERE_FINANCING_RISK, D_THESIS exit, financing score   <- 10-K profiles (tenk_filings, SEC
                     acceptance time, production classifier tags) + EDGAR full-text hits on 10-K / 10-Q
  FINANCING_TEXT     ATM / registered direct / reverse split / min bid / convertible / warrants / material weakness /
                     late filing (production TEXT_RISKS phrases + severity rules) <- EDGAR full-text hits (filing date)
  FINANCING_ACTIONS  shelf / 424B / 3.02 PIPE / 3.01 deficiency <- corporate_actions (SEC filing dates; staged by the
                     replay itself, no reconstruction needed)
  DILUTION           cover-page share growth >= 20 % YoY (production formula) <- shares_outstanding_facts (filed dates)
  CASH_RUNWAY        runway from cash / operating cash flow <- NOT reconstructable from the database (no filed-date
                     cash / OCF facts): UNAVAILABLE
  THESIS_INVALIDATION news-based thesis_changes (since 2026-09-25 only) + 10-K going concern: PARTIAL at best

A replay is FULL only when every family in REQUIRED_FOR_FULL is COMPLETE over its window for its issuers
(coverage_policy). Nothing here changes a production threshold: the rules are imported from production modules."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from app.replay.store import guard_isolation

SCHEMA = "risk_hist_v3"  # v3: run-scoped labels keyed per assessment interval, related documents, run fingerprints
COMPLETE, PARTIAL, UNAVAILABLE = "COMPLETE", "PARTIAL", "UNAVAILABLE"
FAMILIES = ("GOING_CONCERN", "FINANCING_TEXT", "FINANCING_ACTIONS", "DILUTION", "CASH_RUNWAY", "THESIS_INVALIDATION")
# every production decision that reads risk history needs these to be complete for a FULL replay
REQUIRED_FOR_FULL = FAMILIES
TEXT_LOOKBACK_DAYS = 365  # production scan_text_risks: a phrase hit counts for 365 days
GC_DETECTOR = "tenk_gc_present"
GC_DETECTOR_VERSION = "1"
TEXT_DETECTOR = "efts_text_phrase"
TEXT_DETECTOR_VERSION = "1"
DILUTION_DETECTOR = "cover_page_share_growth"
DILUTION_DETECTOR_VERSION = "2"  # v2: one label per assessment (triggering filing)


def _rule_version_text() -> str:
    from app.discovery.risks import TEXT_RISKS
    return "text_risks:" + hashlib.sha256(json.dumps(TEXT_RISKS, sort_keys=True).encode()).hexdigest()[:12]


def _rule_version_gc() -> str:
    from app.tenk.diff import CLASSIFIER_VERSION, MIN_TOPIC_SENTENCES
    return f"tenk.classifier.v{CLASSIFIER_VERSION}+min_sentences.{MIN_TOPIC_SENTENCES}"


def end_of_public_day(d: date) -> datetime:
    """A document known only by its filing DATE becomes usable at the end of that day in New York (same conservative
    rule as the V2 event contract: tradable from the next session)."""
    from app.marketdata.calendar import NY
    return datetime.combine(d, time(23, 59, 59), NY).astimezone(timezone.utc)


@dataclass(frozen=True)
class RiskLabel:
    risk_type: str
    cik: str
    severity: str
    source_document_id: str  # accession
    source_url: str
    source_kind: str  # 10-K profile | EDGAR full-text hit | cover-page share fact
    accepted_at: datetime | None  # SEC acceptance time when known
    available_at: datetime
    valid_from: datetime
    valid_to: datetime | None
    detector: str
    detector_version: str
    rule_version: str
    confidence: str
    value: str | None = None
    security_id: str | None = None
    related_documents: tuple[str, ...] = ()  # further documents the decision rests on (e.g. the compared share facts)

    def __post_init__(self) -> None:
        if not self.source_document_id or not self.source_url:
            raise ValueError("a reconstructed risk needs source provenance (document id + url)")
        if self.available_at.tzinfo is None or self.valid_from < self.available_at:
            raise ValueError("valid_from can never precede the document's public time")

    def key(self) -> tuple:
        """One persisted row per assessment interval: the same document can open several intervals (a later
        amendment re-decides with it), so the interval start is part of the identity."""
        return (self.risk_type, self.cik, self.source_document_id, self.valid_from, self.detector_version,
                self.rule_version)


# ------------------------------------------------------------------ detectors (pure, versioned)

def gc_sentences(profile: dict) -> int:
    """Production 10-K classifier tags of ONE report: sentences tagged GOING_CONCERN anywhere in it."""
    return len({h for s, p in (profile or {}).items() if not s.startswith("_")
                for h, (tp, *_rest) in (p.get("tagged") or {}).items() if "GOING_CONCERN" in tp})


def detect_going_concern_10k(filings: list[dict]) -> list[RiskLabel]:
    """filings of ONE issuer: {accession, cik, form, accepted_at, filing_date, source_url, profile}. A report that warns
    (>= MIN_TOPIC_SENTENCES tagged sentences, production rule) yields a GOING_CONCERN label from its acceptance time
    until the next report of the issuer is public (the next report decides again). Reports classified with an older
    classifier version are skipped (coverage then PARTIAL - the caller counts them)."""
    from app.tenk.diff import CLASSIFIER_VERSION, MIN_TOPIC_SENTENCES
    ordered = sorted(filings, key=lambda f: (f["accepted_at"] or end_of_public_day(f["filing_date"]), f["accession"]))
    out = []
    for i, f in enumerate(ordered):
        prof = f.get("profile") or {}
        if (prof.get("_meta") or {}).get("v", 1) != CLASSIFIER_VERSION:
            continue
        n = gc_sentences(prof)
        if n < MIN_TOPIC_SENTENCES:
            continue
        avail = f["accepted_at"] or end_of_public_day(f["filing_date"])
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        until = (nxt["accepted_at"] or end_of_public_day(nxt["filing_date"])) if nxt else None
        out.append(RiskLabel("GOING_CONCERN", f["cik"], "HIGH", f["accession"], f["source_url"], "10-K profile",
                             f["accepted_at"], avail, avail, until, GC_DETECTOR, GC_DETECTOR_VERSION, _rule_version_gc(),
                             "HIGH" if f["accepted_at"] else "MEDIUM", f"{n} going-concern sentence(s) in {f['form']}"))
    return out


def detect_text_hits(hits: list[dict]) -> list[RiskLabel]:
    """EDGAR full-text hits {risk_key, adsh, cik, form, file_date, filename} for the production TEXT_RISKS phrases:
    one label per (risk, filing), public at the end of its filing day, counted for TEXT_LOOKBACK_DAYS like
    scan_text_risks. Severity escalation / FREQUENT_CAPITAL_RAISES are applied point in time in active_risks()."""
    from app.discovery.risks import CANON, TEXT_RISKS
    from app.discovery.sec_data import doc_url
    out, seen = [], set()
    for h in hits:
        risk = CANON.get(h["risk_key"], h["risk_key"])
        k = (risk, h["cik"], h["adsh"])
        if k in seen or not h.get("file_date"):
            continue
        seen.add(k)
        fd = date.fromisoformat(str(h["file_date"])[:10])
        avail = end_of_public_day(fd)
        out.append(RiskLabel(risk, h["cik"], TEXT_RISKS[h["risk_key"]][2], h["adsh"],
                             doc_url(h["cik"], h["adsh"], h.get("filename") or ""), "EDGAR full-text hit", None, avail, avail,
                             avail + timedelta(days=TEXT_LOOKBACK_DAYS), TEXT_DETECTOR, TEXT_DETECTOR_VERSION,
                             _rule_version_text(), "MEDIUM", f"{h.get('form')} {fd}"))
    return out


def detect_dilution(cik: str, facts: list[tuple]) -> list[RiskLabel]:
    """facts: (accession, end_date, filed, form, val). At each new filing the production formula is applied to what
    was filed by then: last cover-page count vs the one 300-430 days earlier (discovery.metrics.compute). Every
    assessment is its own interval: the triggering filing (the one filed that day) is the source document, the two
    compared facts are related documents - so an amendment that restates the base opens a new interval even though
    the latest count (and its accession) is unchanged."""
    out = []
    for filed in sorted({f[2] for f in facts}):
        trigger = sorted({f[0] for f in facts if f[2] == filed})
        known: dict[date, tuple] = {}
        for f in sorted((f for f in facts if f[2] <= filed), key=lambda f: (f[1], f[2])):
            known[f[1]] = f  # an end date re-reported later replaces the earlier value (latest filed wins)
        series = sorted(known.items())
        if len(series) < 2:
            continue
        last_d, last = series[-1]
        ya = [v for d, v in series if 300 <= (last_d - d).days <= 430]
        if not ya or not ya[0][4]:
            continue
        g = last[4] / ya[0][4] - 1
        if g < 0.2:
            continue
        nxt = min((x[2] for x in facts if x[2] > filed), default=None)
        avail = end_of_public_day(filed)
        base = ya[0]
        out.append(RiskLabel("DILUTION", cik, "HIGH" if g >= 0.5 else "MEDIUM", trigger[0],
                             f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{trigger[0].replace('-', '')}/",
                             "cover-page share fact", None, avail, avail, end_of_public_day(nxt) if nxt else None,
                             DILUTION_DETECTOR, DILUTION_DETECTOR_VERSION, "metrics.share_growth_yoy>=0.2", "HIGH",
                             f"shares outstanding +{g:.0%} YoY: {last[4]} ({last[0]}, {last[1]}) vs {base[4]} "
                             f"({base[0]}, {base[1]})",
                             related_documents=tuple(sorted({last[0], base[0], *trigger[1:]} - {trigger[0]}))))
    return out


def active_risks(labels: list[RiskLabel], cik: str, at: datetime) -> dict[str, dict]:
    """Production-shaped discovery_risks rows of one issuer at `at` from the labels public by then (severity
    escalation and FREQUENT_CAPITAL_RAISES exactly as scan_text_risks). Each row keeps its evidence."""
    live = [x for x in labels if x.cik == cik and x.available_at <= at and (x.valid_to is None or at < x.valid_to)]
    out: dict[str, dict] = {}
    by: dict[str, list[RiskLabel]] = {}
    for x in live:
        by.setdefault(x.risk_type, []).append(x)
    for risk, xs in by.items():
        latest = max(xs, key=lambda x: x.available_at)
        sev = latest.severity
        if risk in ("ATM_OFFERING", "REGISTERED_DIRECT") and len({x.source_document_id for x in xs}) >= 3:
            sev = "HIGH"
        out[risk] = {"severity": sev, "value": latest.value, "source_url": latest.source_url,
                     "detected_at": latest.available_at,
                     "evidence": sorted({d for x in xs for d in (x.source_document_id, *x.related_documents)})}
    raises = {x.source_document_id for r in ("ATM_OFFERING", "REGISTERED_DIRECT") for x in by.get(r, [])}
    if len(raises) >= 3:
        ev = [x for r in ("ATM_OFFERING", "REGISTERED_DIRECT") for x in by.get(r, [])]
        out["FREQUENT_CAPITAL_RAISES"] = {"severity": "HIGH", "value": f"{len(raises)} offering filings",
                                          "source_url": max(ev, key=lambda x: x.available_at).source_url,
                                          "detected_at": max(x.available_at for x in ev), "evidence": sorted(raises)}
    return out


# ------------------------------------------------------------------ coverage policy

@dataclass
class Coverage:
    """status per family for one scope (issuer set) and window."""
    families: dict[str, dict] = field(default_factory=dict)  # family -> {status, from, to, why}

    def status(self, family: str) -> str:
        return (self.families.get(family) or {}).get("status", UNAVAILABLE)


def coverage_policy(cov: Coverage, start: date, end: date) -> dict:
    """FULL iff every REQUIRED_FOR_FULL family is COMPLETE and its covered range contains [start, end]."""
    missing = {}
    for fam in REQUIRED_FOR_FULL:
        f = cov.families.get(fam) or {}
        st = f.get("status", UNAVAILABLE)
        lo, hi = f.get("from"), f.get("to")
        if st != COMPLETE or lo is None or lo > start or (hi is not None and hi < end):
            missing[fam] = st if st != COMPLETE else PARTIAL
    flag = ("RISK_HISTORY_COMPLETE" if not missing else
            "RISK_HISTORY_UNAVAILABLE" if all(v == UNAVAILABLE for v in missing.values()) and
            len(missing) == len(REQUIRED_FOR_FULL) else "RISK_HISTORY_PARTIAL")
    return {"flag": flag, "comparability": "FULL" if not missing else "LIMITED", "families": cov.families,
            "not_complete": missing}


# ------------------------------------------------------------------ store (replay database only)

DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE IF NOT EXISTS {SCHEMA}.risk_labels (
  id BIGSERIAL PRIMARY KEY, risk_type TEXT NOT NULL, cik TEXT NOT NULL, security_id TEXT, severity TEXT NOT NULL,
  value TEXT, source_document_id TEXT NOT NULL, source_url TEXT NOT NULL, source_kind TEXT NOT NULL,
  accepted_at TIMESTAMPTZ, available_at TIMESTAMPTZ NOT NULL, valid_from TIMESTAMPTZ NOT NULL, valid_to TIMESTAMPTZ,
  detector TEXT NOT NULL, detector_version TEXT NOT NULL, rule_version TEXT NOT NULL, confidence TEXT NOT NULL,
  reconstructed_at TIMESTAMPTZ NOT NULL, run_id TEXT NOT NULL, related_documents TEXT NOT NULL DEFAULT '',
  CHECK (valid_from >= available_at),
  UNIQUE (run_id, risk_type, cik, source_document_id, valid_from, detector_version, rule_version));
CREATE TABLE IF NOT EXISTS {SCHEMA}.risk_coverage (
  family TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL, covered_from DATE, covered_to DATE, why TEXT,
  run_id TEXT NOT NULL, computed_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (family, scope, run_id));
CREATE TABLE IF NOT EXISTS {SCHEMA}.reconstruction_progress (
  run_id TEXT NOT NULL, family TEXT NOT NULL, done_cik TEXT NOT NULL, done_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (run_id, family, done_cik));
CREATE TABLE IF NOT EXISTS {SCHEMA}.reconstruction_runs (
  run_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, source_digest TEXT NOT NULL, config JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL, source_verified_at TIMESTAMPTZ);
"""


class RunInputChanged(RuntimeError):
    """A run id was resumed with a different source file or detector / rule configuration."""


class SourceIncomplete(RuntimeError):
    """The export is not verifiably complete (no trailer, missing / duplicate issuers, unfinished phrase scans)."""


class RiskHistoryStore:
    def __init__(self, url: str):
        guard_isolation(url, "replay_risk")  # same production-database refusal as the replay store
        self.engine: Engine = create_engine(url, future=True, poolclass=NullPool)

    def create(self) -> None:
        with self.engine.begin() as c:
            for stmt in DDL.strip().split(";\n"):
                if stmt.strip():
                    c.execute(text(stmt))

    def done(self, run_id: str, family: str) -> set[str]:
        with self.engine.connect() as c:
            return set(c.execute(text(f"SELECT done_cik FROM {SCHEMA}.reconstruction_progress WHERE run_id = :r AND family = :f"),
                                 {"r": run_id, "f": family}).scalars())

    def write(self, run_id: str, family: str, cik: str, labels: list[RiskLabel]) -> int:
        """Run-scoped and idempotent per (run, label key): every run holds its complete, immutable label set (a later
        run with a longer source history - e.g. a new report that ends an earlier label's validity - writes its own
        rows and never changes or hides another run's). The issuer is checkpointed in the same transaction."""
        now = datetime.now(timezone.utc)
        n = 0
        with self.engine.begin() as c:
            for x in labels:
                n += c.execute(text(f"""INSERT INTO {SCHEMA}.risk_labels (risk_type, cik, security_id, severity, value,
                    source_document_id, source_url, source_kind, accepted_at, available_at, valid_from, valid_to, detector,
                    detector_version, rule_version, confidence, reconstructed_at, run_id, related_documents)
                    VALUES (:rt, :cik, :sid, :sev, :val, :doc, :url, :kind, :acc, :av, :vf, :vt, :det, :dv, :rv, :conf, :now,
                            :run, :rel)
                    ON CONFLICT (run_id, risk_type, cik, source_document_id, valid_from, detector_version, rule_version)
                    DO NOTHING"""),
                    {"rt": x.risk_type, "cik": x.cik, "sid": x.security_id, "sev": x.severity, "val": x.value,
                     "doc": x.source_document_id, "url": x.source_url, "kind": x.source_kind, "acc": x.accepted_at,
                     "av": x.available_at, "vf": x.valid_from, "vt": x.valid_to, "det": x.detector,
                     "dv": x.detector_version, "rv": x.rule_version, "conf": x.confidence, "now": now, "run": run_id,
                     "rel": ",".join(x.related_documents)}).rowcount
            c.execute(text(f"""INSERT INTO {SCHEMA}.reconstruction_progress (run_id, family, done_cik, done_at)
                               VALUES (:r, :f, :c, :now) ON CONFLICT DO NOTHING"""),
                      {"r": run_id, "f": family, "c": cik, "now": now})
        return n

    def begin_run(self, run_id: str, fingerprint: str, source_digest: str, config: dict) -> None:
        """Pins the run's immutable inputs: a restart must present the SAME source and configuration, otherwise its
        labels (written from the old inputs) and the new coverage would describe different data."""
        with self.engine.begin() as c:
            c.execute(text(f"""INSERT INTO {SCHEMA}.reconstruction_runs (run_id, fingerprint, source_digest, config, created_at)
                               VALUES (:r, :f, :d, CAST(:c AS jsonb), :now) ON CONFLICT (run_id) DO NOTHING"""),
                      {"r": run_id, "f": fingerprint, "d": source_digest, "c": json.dumps(config, sort_keys=True),
                       "now": datetime.now(timezone.utc)})
            have = c.execute(text(f"SELECT fingerprint FROM {SCHEMA}.reconstruction_runs WHERE run_id = :r"),
                             {"r": run_id}).scalar()
        if have != fingerprint:
            raise RunInputChanged(f"run {run_id} was started with other inputs ({have} != {fingerprint}); use a new run id")

    def mark_verified(self, run_id: str) -> None:
        with self.engine.begin() as c:
            c.execute(text(f"UPDATE {SCHEMA}.reconstruction_runs SET source_verified_at = :now WHERE run_id = :r"),
                      {"r": run_id, "now": datetime.now(timezone.utc)})

    def set_coverage(self, run_id: str, scope: str, cov: Coverage) -> None:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as c:
            for fam, f in cov.families.items():
                c.execute(text(f"""INSERT INTO {SCHEMA}.risk_coverage (family, scope, status, covered_from, covered_to, why,
                                       run_id, computed_at) VALUES (:f, :s, :st, :lo, :hi, :why, :r, :now)
                                   ON CONFLICT (family, scope, run_id) DO UPDATE SET status = :st, covered_from = :lo,
                                       covered_to = :hi, why = :why, computed_at = :now"""),
                          {"f": fam, "s": scope, "st": f["status"], "lo": f.get("from"), "hi": f.get("to"),
                           "why": f.get("why"), "r": run_id, "now": now})

    def load(self, ciks: list[str], until: datetime, run_id: str | None = None) -> tuple[list[RiskLabel], Coverage]:
        """Labels of these issuers public by `until` (a replay window end) and the coverage of their scope."""
        with self.engine.connect() as c:
            rows = c.execute(text(f"""SELECT * FROM {SCHEMA}.risk_labels WHERE cik = ANY(:c) AND available_at <= :u
                                        AND (CAST(:r AS text) IS NULL OR run_id = CAST(:r AS text)) ORDER BY available_at, id"""),
                             {"c": ciks, "u": until, "r": run_id}).mappings().all()
            covs = c.execute(text(f"""SELECT v.family, v.status, v.covered_from, v.covered_to, v.why
                                        FROM {SCHEMA}.risk_coverage v JOIN {SCHEMA}.reconstruction_runs u USING (run_id)
                                        WHERE v.scope = :s AND u.source_verified_at IS NOT NULL
                                          AND (CAST(:r AS text) IS NULL OR v.run_id = CAST(:r AS text)) ORDER BY v.computed_at"""),
                             {"s": scope_key(ciks), "r": run_id}).mappings().all()
        labels = [RiskLabel(r["risk_type"], r["cik"], r["severity"], r["source_document_id"], r["source_url"],
                            r["source_kind"], r["accepted_at"], r["available_at"], r["valid_from"], r["valid_to"],
                            r["detector"], r["detector_version"], r["rule_version"], r["confidence"], r["value"],
                            r["security_id"], tuple(d for d in r["related_documents"].split(",") if d)) for r in rows]
        cov = Coverage({r["family"]: {"status": r["status"], "from": r["covered_from"], "to": r["covered_to"],
                                      "why": r["why"]} for r in covs})
        return labels, cov


def scope_key(ciks: list[str]) -> str:
    return "ciks:" + hashlib.sha256(",".join(sorted(ciks)).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ reconstruction job (offline, replay database only)

def tenk_full_from(by_year: dict[int, int]) -> date | None:
    """First Jan 1 from which the 10-K history is complete (>= 80 % of the median later year), plus one year: a
    going-concern STATE needs each issuer's previous annual report, which is only guaranteed a year later."""
    years = sorted(by_year)
    for i, y in enumerate(years):
        later = sorted(by_year[x] for x in years[i:])
        med = later[len(later) // 2]
        if med and by_year[y] >= 0.8 * med:
            return date(y + 1, 1, 1)
    return None


def dilution_inventory(facts: list[tuple]) -> dict:
    """What the dilution detector can decide for ONE issuer: the first filing date at which a 300-430-day comparison
    pair exists, the largest gap between consecutive filings after it (a gap > 430 days leaves the rule undecidable)
    and the last filing (nothing is known beyond last + 430 days)."""
    filed = sorted({f[2] for f in facts})
    first = None
    for d in filed:
        ends = sorted({f[1] for f in facts if f[2] <= d})
        if ends and any(300 <= (ends[-1] - e).days <= 430 for e in ends):
            first = d
            break
    after = [d for d in filed if first and d >= first]
    gap = max(((b - a).days for a, b in zip(after, after[1:])), default=0)
    return {"from": first, "max_gap_days": gap, "last": filed[-1] if filed else None}


def _bounded(lo, hi, horizon):
    return lo, (min(hi, horizon) if hi else horizon)


def compute_coverage(meta: dict, inv: dict[str, dict], ciks: list[str]) -> Coverage:
    """Issuer-specific, bounded coverage from the verified source inventories and detector prerequisites. The scope is
    COMPLETE only when EVERY issuer is; nothing extends past the export horizon (meta["end"])."""
    horizon = date.fromisoformat(meta["end"])
    fam: dict[str, dict] = {}
    efts = meta.get("efts_scan") or {}
    scanned = bool(efts.get("done")) and not efts.get("truncated")
    e_lo = date.fromisoformat(efts["from"]) + timedelta(days=TEXT_LOOKBACK_DAYS) if scanned else None
    e_hi = date.fromisoformat(efts["to"]) if scanned else None
    outdated = sorted(c for c in ciks if (inv.get(c) or {}).get("outdated_profiles"))
    # an issuer without an inventory at all counts as missing; so does any failed / unprocessed / absent 10-K
    missing = {c: (inv.get(c) or {}).get("gc_missing", ["no inventory for this issuer"]) for c in ciks}
    missing = {c: w for c, w in missing.items() if w}
    gc_from = tenk_full_from({int(k): v for k, v in (meta.get("tenk_by_year") or {}).items()})
    if scanned and not outdated and not missing:
        lo, hi = _bounded(e_lo, e_hi, horizon)
        fam["GOING_CONCERN"] = {"status": COMPLETE, "from": lo, "to": hi,
                                "why": "full-text scan of the production phrases + current-classifier 10-K profiles"}
    elif scanned or gc_from:
        reasons = ([f"10-K profiles with an older classifier for {outdated}"] if outdated else []) + \
                  ([f"missing / failed / unprocessed 10-K profiles for {len(missing)} issuer(s): "
                    + "; ".join(f"{c}: {', '.join(w[:3])}" for c, w in sorted(missing.items())[:5])] if missing else []) + \
                  ([] if scanned else ["no full-text scan: 10-K profiles only"])
        fam["GOING_CONCERN"] = {"status": PARTIAL, "from": e_lo or gc_from, "to": min(e_hi, horizon) if e_hi else horizon,
                                "why": " | ".join(reasons), "missing_issuers": sorted(missing)}
    fam["FINANCING_TEXT"] = ({"status": COMPLETE, "from": e_lo, "to": min(e_hi, horizon), "why": "full-text scan"}
                             if scanned else {"status": UNAVAILABLE, "why": "no untruncated full-text scan for this scope"})
    act = [inv.get(c, {}).get("actions") or {} for c in ciks]
    if all(a.get("earliest") and a.get("fetched") for a in act) and act:
        lo = max(date.fromisoformat(a["earliest"]) for a in act) + timedelta(days=730)
        hi = min(date.fromisoformat(a["fetched"]) for a in act)
        fam["FINANCING_ACTIONS"] = {"status": COMPLETE, "from": lo, "to": min(hi, horizon),
                                    "why": "each issuer's SEC submissions (recent list) from its earliest action + 730d"}
    else:
        miss = [c for c, a in zip(ciks, act) if not (a.get("earliest") and a.get("fetched"))]
        fam["FINANCING_ACTIONS"] = {"status": PARTIAL if len(miss) < len(ciks) else UNAVAILABLE,
                                    "why": f"no submissions inventory for {miss}"}
    dil = [inv.get(c, {}).get("dilution") or {} for c in ciks]
    ok = [d for d in dil if d.get("from")]
    if dil and len(ok) == len(dil) and all(d["max_gap_days"] <= 430 for d in ok):
        lo = max(date.fromisoformat(str(d["from"])) for d in ok)
        hi = min(date.fromisoformat(str(d["last"])) + timedelta(days=430) for d in ok)
        fam["DILUTION"] = {"status": COMPLETE, "from": lo, "to": min(hi, horizon), "why": "comparison pair for every issuer"}
    else:
        bad = [c for c, d in zip(ciks, dil) if not d.get("from") or d.get("max_gap_days", 0) > 430]
        fam["DILUTION"] = {"status": PARTIAL if ok else UNAVAILABLE,
                           "why": f"no 300-430-day comparison pair or a filing gap > 430 days for {bad}"}
    fam["CASH_RUNWAY"] = {"status": UNAVAILABLE, "why": "no filed-date cash / operating-cash-flow facts in the database"}
    thesis = meta.get("thesis_changes_from")
    fam["THESIS_INVALIDATION"] = ({"status": PARTIAL, "from": gc_from, "to": horizon,
                                   "why": f"news thesis_changes only since {thesis}; 10-K going concern before"}
                                  if gc_from else {"status": UNAVAILABLE, "why": "no thesis inputs"})
    return Coverage(fam)


TRAILER = "_trailer"


def read_source(path):
    """Issuer-partitioned JSON lines (scripts/risk_source_export.py): the first line is the metadata, every further
    line ONE issuer, the last line the exporter's completion trailer - read lazily, so memory holds one issuer."""
    with open(path) as fh:
        meta = json.loads(fh.readline())
        yield "meta", meta
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            yield ("trailer", rec[TRAILER]) if TRAILER in rec else ("issuer", _issuer(rec))


def _dt(v):
    return v if isinstance(v, datetime) or v is None else datetime.fromisoformat(str(v))


def _d(v):
    return v.date() if isinstance(v, datetime) else v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _issuer(rec: dict) -> dict:
    for f in rec.get("tenk", []):
        f["accepted_at"] = _dt(f.get("accepted_at")) if f.get("accepted_at") else None
        f["filing_date"] = _d(f["filing_date"])
    for f in rec.get("tenk_other") or []:
        f["filing_date"] = _d(f["filing_date"])
    if rec.get("delisting_date"):
        rec["delisting_date"] = _d(rec["delisting_date"])
    rec["shares"] = [(f[0], _d(f[1]), _d(f[2]), f[3], int(f[4])) for f in rec.get("shares", [])]
    return rec


ANNUAL_GAP_DAYS = 460  # a 10-K is due every fiscal year (+ filing deadline slack); a longer silence = a report is missing


def gc_missing(rec: dict, start: date, end: date, jobs: dict | None) -> list[str]:
    """Why the GOING_CONCERN state of ONE issuer cannot be known over [start, end] from the exported profiles (empty =
    every 10-K that matters was profiled). The export lists only status='ok' profiles in rec['tenk']; everything else
    must be declared: rec['tenk_other'] (failed / unparsable / not-found / superseded-without-replacement 10-Ks) and
    meta['tenk_jobs'] (issuers whose 10-K ingestion never completed or was given up). An export without these
    inventories cannot prove completeness."""
    lo = start - timedelta(days=400)
    why = []
    if "tenk_other" not in rec:
        why.append("export has no failed/unprocessed 10-K inventory")
    if jobs is None:
        why.append("export has no 10-K ingestion job state")
    else:
        cik = rec["cik"]
        if cik not in set(jobs.get("backfill_done") or []):
            why.append("10-K backfill not completed for this issuer")
        if cik in set(jobs.get("backfill_gave_up") or []) or cik in set(jobs.get("live_gave_up") or []):
            why.append("10-K ingestion gave up on this issuer")
        q = (jobs.get("live_queue") or {}).get(cik)
        if q and str(q) <= end.isoformat():
            why.append(f"10-K queued since {q}, not yet processed")
    ok = {f["accession"][:20] for f in rec.get("tenk", [])}
    for f in rec.get("tenk_other") or []:
        if lo <= f["filing_date"] <= end and f["accession"][:20] not in ok:
            why.append(f"10-K {f['accession'][:20]} {f['filing_date']}: {f['status']}")
    # at every moment of [start, end] the latest report must be at most ANNUAL_GAP_DAYS old (a new issuer with no
    # report yet is not flagged here; its first report is covered by the status checks above)
    known = sorted({f["filing_date"] for f in rec.get("tenk", []) + (rec.get("tenk_other") or []) if f["filing_date"] <= end})
    upto_start = [d for d in known if d <= start]
    if upto_start and (start - upto_start[-1]).days > ANNUAL_GAP_DAYS:
        why.append(f"no 10-K between {upto_start[-1]} and the window start {start}")
    inside = upto_start[-1:] + [d for d in known if d > start]
    for a, b in zip(inside, inside[1:]):
        if (b - a).days > ANNUAL_GAP_DAYS:
            why.append(f"no 10-K between {a} and {b}")
    gone = rec.get("delisting_date")
    if known and (end - known[-1]).days > ANNUAL_GAP_DAYS and not (gone and gone <= known[-1] + timedelta(days=ANNUAL_GAP_DAYS)):
        why.append(f"no 10-K after {known[-1]}")
    return why


def issuer_inventory(rec: dict, start: date, end: date, jobs: dict | None = None) -> dict:
    from app.tenk.diff import CLASSIFIER_VERSION
    lo = start - timedelta(days=400)
    outdated = [f["accession"] for f in rec.get("tenk", []) if lo <= f["filing_date"] <= end
                and ((f.get("profile") or {}).get("_meta") or {}).get("v", 1) != CLASSIFIER_VERSION]
    d = dilution_inventory(rec.get("shares", []))
    return {"outdated_profiles": outdated, "gc_missing": gc_missing(rec, start, end, jobs), "actions": rec.get("actions") or {},
            "dilution": {k: (str(v) if isinstance(v, date) else v) for k, v in d.items()}}


def source_digest(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def run_config() -> dict:
    """Everything besides the source that decides a label: detector + rule versions."""
    return {"gc": [GC_DETECTOR, GC_DETECTOR_VERSION, _rule_version_gc()],
            "text": [TEXT_DETECTOR, TEXT_DETECTOR_VERSION, _rule_version_text(), TEXT_LOOKBACK_DAYS],
            "dilution": [DILUTION_DETECTOR, DILUTION_DETECTOR_VERSION, "metrics.share_growth_yoy>=0.2"]}


def verify_export(meta: dict, trailer: dict | None, seen: list[str]) -> list[str]:
    """Problems that make the export unverifiable (empty = complete): every declared issuer exactly once, the
    trailer written after the last issuer, and every declared phrase scanned over every issuer batch."""
    bad = []
    declared = sorted(meta.get("ciks") or [])
    if trailer is None:
        bad.append("no completion trailer: the export stopped early")
    dup = sorted({c for c in seen if seen.count(c) > 1})
    if dup:
        bad.append(f"duplicate issuer records {dup}")
    miss = sorted(set(declared) - set(seen))
    if miss:
        bad.append(f"missing issuers {miss[:10]}{'...' if len(miss) > 10 else ''} ({len(miss)})")
    extra = sorted(set(seen) - set(declared))
    if extra:
        bad.append(f"undeclared issuers {extra[:10]}")
    if trailer is not None:
        if trailer.get("issuers") != len(declared) or trailer.get("ciks_digest") != scope_key(declared):
            bad.append("trailer does not match the declared issuer list")
        efts = meta.get("efts_scan")
        if efts:
            done = trailer.get("efts_batches") or {}
            short = [p for p in efts.get("phrases", []) if done.get(p) != trailer.get("batches")]
            if short:
                bad.append(f"phrase scans not finished for every batch: {short}")
    return bad


def reconstruct(store: RiskHistoryStore, run_id: str, path) -> dict:
    """Streams the issuer-partitioned source: one issuer in memory, labels + checkpoint in one transaction per
    (issuer, family); idempotent and restartable under the run's pinned input fingerprint. Coverage is computed
    from the per-issuer inventories and published only once the export is verified complete."""
    store.create()
    digest, conf = source_digest(path), run_config()
    it = read_source(path)
    _, meta = next(it)
    window = {"start": meta["start"], "end": meta["end"]}
    fp = hashlib.sha256(json.dumps({"source": digest, "config": conf, "window": window}, sort_keys=True).encode()).hexdigest()
    store.begin_run(run_id, fp, digest, {**conf, **window})
    start, end = date.fromisoformat(meta["start"]), date.fromisoformat(meta["end"])
    out = {"labels_written": 0, "skipped_done": 0, "by_family": {}, "issuers": 0, "fingerprint": fp[:16]}
    inv: dict[str, dict] = {}
    seen: list[str] = []
    trailer = None
    done = {f: store.done(run_id, f) for f in ("GOING_CONCERN_10K", "TEXT", "DILUTION")}
    for kind, rec in it:
        if kind == "trailer":
            trailer = rec
            continue
        if trailer is not None:
            raise SourceIncomplete("an issuer record after the completion trailer")
        cik = rec["cik"]
        seen.append(cik)
        out["issuers"] += 1
        if rec.get("efts_truncated") and meta.get("efts_scan"):
            meta["efts_scan"]["truncated"] = True  # an incomplete query leaves its scope's text coverage incomplete
        inv[cik] = issuer_inventory(rec, start, end, meta.get("tenk_jobs"))
        for fam, detect in (("GOING_CONCERN_10K", lambda r: detect_going_concern_10k(r.get("tenk", []))),
                            ("TEXT", lambda r: detect_text_hits(r.get("efts_hits", []))),
                            ("DILUTION", lambda r: detect_dilution(r["cik"], r.get("shares", [])))):
            if cik in done[fam]:
                out["skipped_done"] += 1
                continue
            labels = detect(rec)
            out["labels_written"] += store.write(run_id, fam, cik, labels)
            out["by_family"][fam] = out["by_family"].get(fam, 0) + len(labels)
        del rec
    problems = verify_export(meta, trailer, seen)
    if problems:
        # the labels written are correct for the issuers they cover, but no coverage is claimed for the scope
        raise SourceIncomplete("; ".join(problems))
    store.mark_verified(run_id)
    ciks = sorted(meta["ciks"])
    cov = compute_coverage(meta, inv, ciks)
    store.set_coverage(run_id, scope_key(ciks), cov)
    out["coverage"] = {k: v["status"] for k, v in cov.families.items()}
    out["policy"] = coverage_policy(cov, start, end)["flag"]
    return out
