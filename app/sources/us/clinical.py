"""ClinicalTrials.gov (API v2) - industry-sponsored studies of listed issuers.

Point in time: the public API serves only the CURRENT version of a study. Honest historical dates exist for
  * TRIAL_REGISTERED  (studyFirstPostDate - when the registration became public)
  * RESULTS_POSTED    (resultsFirstPostDate)
Status / phase changes (TRIAL_START, TRIAL_PHASE_CHANGE, TRIAL_COMPLETION, TRIAL_TERMINATED, TRIAL_SUSPENDED) are
detected FORWARD only, by diffing each new version (lastUpdatePostDate) against the version stored before it;
available_at = the version's posting date (end of day, New York) or our first observation if earlier.
Issuer mapping: the LEAD sponsor (class INDUSTRY) by exact normalized name over the SEC security master; a
collaborator never makes a study an issuer's event. Unmapped sponsors are counted, not stored."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app import http
from app.core import records
from app.core.provenance import NY, end_of_local_day
from app.db.store import create_event
from app.markets.identity import name_to_cik
from app.sources.base import RunResult, Source

log = logging.getLogger(__name__)
PARSER_VERSION = "ctgov-v2-1"
API = "https://clinicaltrials.gov/api/v2/studies"
FIELDS = ("protocolSection.identificationModule,protocolSection.statusModule,protocolSection.sponsorCollaboratorsModule,"
          "protocolSection.designModule,protocolSection.conditionsModule,protocolSection.armsInterventionsModule,hasResults")
TERMINAL = {"TERMINATED": "TRIAL_TERMINATED", "SUSPENDED": "TRIAL_SUSPENDED", "WITHDRAWN": "TRIAL_TERMINATED",
            "COMPLETED": "TRIAL_COMPLETION"}


def _date(struct) -> date | None:
    s = (struct or {}).get("date") if isinstance(struct, dict) else struct
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_study(s: dict) -> dict:
    p = s.get("protocolSection", {})
    idm, st, sp = p.get("identificationModule", {}), p.get("statusModule", {}), p.get("sponsorCollaboratorsModule", {})
    lead = sp.get("leadSponsor", {}) or {}
    return {
        "nct_id": idm.get("nctId"), "title": idm.get("briefTitle"),
        "lead_sponsor": lead.get("name"), "sponsor_class": lead.get("class"),
        "collaborators": [c.get("name") for c in sp.get("collaborators", []) or []],
        "phases": sorted(p.get("designModule", {}).get("phases", []) or []),
        "overall_status": st.get("overallStatus"), "why_stopped": st.get("whyStopped"),
        "start_date": _date(st.get("startDateStruct")),
        "primary_completion": _date(st.get("primaryCompletionDateStruct")),
        "primary_completion_type": (st.get("primaryCompletionDateStruct") or {}).get("type"),
        "first_post_date": _date(st.get("studyFirstPostDateStruct")),
        "results_first_post_date": _date(st.get("resultsFirstPostDateStruct")),
        "last_update_post_date": _date(st.get("lastUpdatePostDateStruct")),
        "has_results": bool(s.get("hasResults")),
        "conditions": (p.get("conditionsModule", {}).get("conditions") or [])[:10],
        "interventions": [{"type": i.get("type"), "name": i.get("name")}
                          for i in (p.get("armsInterventionsModule", {}).get("interventions") or [])][:10],
    }


def diff_events(prev: dict | None, cur: dict) -> list[tuple[str, dict]]:
    """Events a new version reveals. prev=None: only the history the current snapshot proves (registration,
    results) - never a status change, whose posting date is unknown."""
    out = []
    if prev is None:
        if cur["first_post_date"]:
            out.append(("TRIAL_REGISTERED", {"date": cur["first_post_date"]}))
        if cur["results_first_post_date"]:
            out.append(("RESULTS_POSTED", {"date": cur["results_first_post_date"]}))
        return out
    if cur["results_first_post_date"] and not prev.get("results_first_post_date"):
        out.append(("RESULTS_POSTED", {"date": cur["results_first_post_date"]}))
    if cur["phases"] != prev.get("phases") and cur["phases"] and prev.get("phases"):
        out.append(("TRIAL_PHASE_CHANGE", {"from": prev.get("phases"), "to": cur["phases"]}))
    ps, cs = prev.get("overall_status"), cur["overall_status"]
    if cs != ps:
        if cs == "RECRUITING" and ps in ("NOT_YET_RECRUITING", None):
            out.append(("TRIAL_START", {"from": ps}))
        elif cs in TERMINAL:
            out.append((TERMINAL[cs], {"from": ps, "why_stopped": cur.get("why_stopped")}))
    return out


def process_study(conn, s: dict, *, first_seen: datetime | None = None, is_backfill: bool = False) -> dict:
    cur = parse_study(s)
    if not cur["nct_id"] or cur["sponsor_class"] != "INDUSTRY":
        return {"status": "SKIP_NON_INDUSTRY"}
    cik, conf, why = name_to_cik(conn, cur["lead_sponsor"])
    if not cik:
        return {"status": "UNMAPPED", "reason": why}
    ver = str(cur["last_update_post_date"] or "")
    posted = end_of_local_day(cur["last_update_post_date"]) if cur["last_update_post_date"] else None
    avail = min(x for x in (posted, first_seen) if x) if (posted or first_seen) else None
    if avail is None:
        return {"status": "NO_TIME"}
    prev_rec = records.latest(conn, "us_clinical_trials", cur["nct_id"])
    rid = records.put(conn, country="US", dataset="us_clinical_trials", record_key=cur["nct_id"], record_version=ver,
                      as_of_date=cur["last_update_post_date"], available_at=avail, payload=cur, parser_version=PARSER_VERSION,
                      license_status="PUBLIC_WITH_TERMS", issuer_cik=cik, entity_name=cur["lead_sponsor"],
                      identity_confidence=conf, source_url=f"https://clinicaltrials.gov/study/{cur['nct_id']}")
    if rid is None:
        return {"status": "KNOWN_VERSION"}
    prev = prev_rec["payload"] if prev_rec else None
    evs = []
    from app.entities.registry import resolver
    for et, info in diff_events(prev, cur):
        d = info.get("date")
        ev_avail = end_of_local_day(d) if d else avail
        if prev is not None and et not in ("RESULTS_POSTED",):
            ev_avail = avail  # a change is known when the version that shows it is posted
        pub = d or ev_avail.astimezone(NY).date()
        eid = create_event(conn, event_type=et, dedupe_key=f"CTG:{cur['nct_id']}:{et}:{d or ver}", public_date=pub,
                           ticker=resolver.ticker_for_cik(cik), issuer_cik=cik, company_name=cur["lead_sponsor"],
                           title=f"{cur['lead_sponsor']}: {et.replace('_', ' ').lower()} {cur['nct_id']} ({'/'.join(cur['phases']) or 'n/a'})",
                           payload={**info, "nct_id": cur["nct_id"], "phases": cur["phases"], "status": cur["overall_status"],
                                    "conditions": cur["conditions"], "interventions": cur["interventions"]},
                           source_url=f"https://clinicaltrials.gov/study/{cur['nct_id']}", is_backfill=is_backfill,
                           event_family="US_TRIAL_CATALYST", identity_confidence=conf, parser_version=PARSER_VERSION,
                           available_at_override=ev_avail, date_quality="date")
        if eid:
            evs.append(eid)
    return {"status": "STORED", "events": evs}


def fetch_page(since: date | None = None, token: str | None = None, page_size: int = 500) -> dict:
    q = {"pageSize": page_size, "fields": FIELDS, "query.term": "AREA[LeadSponsorClass]INDUSTRY", "sort": "LastUpdatePostDate:asc"}
    if since:
        q["filter.advanced"] = f"AREA[LastUpdatePostDate]RANGE[{since.isoformat()},MAX]"
    if token:
        q["pageToken"] = token
    return http.get(API, params=q).json()


class ClinicalTrialsSource(Source):
    name = "us_clinical_trials"
    interval_seconds = 6 * 3600
    critical = False
    slow = True
    country, dataset, license_status, parser_version = "US", "us_clinical_trials", "PUBLIC_WITH_TERMS", PARSER_VERSION
    stale_after = timedelta(days=2)

    def run_live(self, engine: Engine, since: date | None = None, max_pages: int = 40) -> RunResult:
        with engine.connect() as c:
            wm = since or c.execute(text("SELECT max(as_of_date) FROM dataset_records WHERE dataset = 'us_clinical_trials'")).scalar()
        since = (wm - timedelta(days=1)) if wm else date.today() - timedelta(days=3)
        now = datetime.now(timezone.utc)
        res, token = RunResult(watermark={"since": str(since)}), None
        stats: dict[str, int] = {}
        for _ in range(max_pages):
            page = fetch_page(since, token)
            for s in page.get("studies", []):
                with engine.begin() as c:
                    out = process_study(c, s, first_seen=now, is_backfill=wm is None)
                stats[out["status"]] = stats.get(out["status"], 0) + 1
                res.items += 1
                res.events += out.get("events", [])
            token = page.get("nextPageToken")
            if not token:
                break
        res.watermark["stats"] = stats
        return res
