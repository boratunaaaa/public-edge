"""Corporate actions of the COMPLETE SEC filing history, for the replay (FINANCING_ACTIONS).

Production fills corporate_actions from two jobs (app.securitymaster.build): the submissions JSON's "recent" block
(8-K items 1.03 / 2.01 / 3.01 / 3.02 / 5.01, S-3 / F-3 shelves, S-1, 424B prospectuses, Form 25 / 15, merger proxies,
tender responses, F-6 depositary programmes) and the EDGAR quarterly full index (INDEX_FORMS). Here the SAME two
mappings run over every filing of sec_filings (recent block + all older pages, insider forms excluded by
app.pit.readers), and every action keeps its filing's exact acceptance time: an action is visible to the replay from
that moment (date-only: from the end of its filing day), never from its filing date's start."""
from __future__ import annotations

from datetime import date, datetime


def actions_from_filings(cik: str, rows: list[dict]) -> list[dict]:
    """rows: sec_filings rows of ONE issuer (accession, form, filing_date, acceptance_at, items)."""
    from app.securitymaster.build import FORM_ACTIONS, INDEX_FORMS, parse_submissions
    cik = cik.zfill(10)
    block = {"accessionNumber": [r["accession"] for r in rows], "form": [r["form"] for r in rows],
             "filingDate": [_iso(r["filing_date"]) for r in rows], "items": [r.get("items") or "" for r in rows]}
    _master, acts = parse_submissions(cik, {"filings": {"recent": block}})
    accepted = {r["accession"]: r.get("acceptance_at") for r in rows}
    for r in rows:  # the full-index job's forms that the submissions mapping does not cover (25/A, 15-15D, ...)
        f = r["form"]
        if f in INDEX_FORMS and f not in FORM_ACTIONS:
            acts.append({"cik": cik, "action": INDEX_FORMS[f], "action_date": _date(r["filing_date"]),
                         "accession": r["accession"], "source": "sec_full_index", "details": {"form": f}})
    out, seen = [], set()
    for a in acts:
        key = (a["cik"], a["action"], a["accession"], a["action_date"])
        if key in seen:
            continue
        seen.add(key)
        out.append({**a, "source": f"pit:{a['source']}", "accepted_at": accepted.get(a["accession"])})
    return sorted(out, key=lambda a: (a["action_date"], a["accession"], a["action"]))


def _iso(d) -> str:
    return d.isoformat() if isinstance(d, (date, datetime)) else str(d)[:10]


def _date(d) -> date:
    return d if isinstance(d, date) and not isinstance(d, datetime) else date.fromisoformat(str(d)[:10])
