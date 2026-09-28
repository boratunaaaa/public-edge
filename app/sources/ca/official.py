"""Canada official open datasets: CanadaBuys contract history, federal grants & contributions, Bank of Canada Valet.

Point in time:
  * CanadaBuys contract history: publicationDate (the award notice's publication) -> end of that day, Toronto.
  * Grants & contributions (proactive disclosure): the file has NO publication date per record (departments publish
    quarterly with a lag) -> records are only usable FORWARD, from our first observation. Historical rows are
    stored with needs_review and are never a backtest input.
  * Valet: an observation is known at the end of its date (policy rate / daily FX published the same day).
Privacy: contact names / e-mails and individual (non-business) recipients are never stored.
Issuer mapping: exact normalized supplier / recipient legal name against the Canadian listings (TSX / TSXV directory)
and the SEC security master. Subsidiaries ("NMG Matawinie Inc." for Nouveau Monde Graphite) resolve only through a
reviewed entity_edges SUBSIDIARY_OF row - never by name similarity."""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app import http
from app.core import records
from app.core.provenance import end_of_local_day
from app.entities.names import normalize_company
from app.markets import master_db
from app.markets.identity import name_to_cik
from app.sources.base import RunResult, Source

log = logging.getLogger(__name__)
PARSER_VERSION = "ca-official-v1"
CB_CONTRACTS = "https://canadabuys.canada.ca/opendata/pub/{fy}-contractHistory-contratsOctroyes.csv"
GRANTS = ("https://open.canada.ca/data/dataset/432527ab-7aac-45b5-81d6-7597107a7013/resource/"
          "1d15a62f-5656-49ad-8c88-f40ce689d831/download/grants.csv")
VALET = "https://www.bankofcanada.ca/valet/observations/{series}/json"
MACRO_SERIES = {"V39079": "policy_rate", "FXUSDCAD": "usdcad", "BD.CDN.10YR.DQ.YLD": "gov_10y", "BD.CDN.2YR.DQ.YLD": "gov_2y"}


def resolve_entity(conn: Connection, name: str | None) -> tuple[str | None, str | None, str | None, str]:
    """(security_id, cik, confidence, reason) for a supplier / recipient legal name."""
    k = normalize_company(name)
    if not k:
        return None, None, None, "EMPTY_NAME"
    edge = conn.execute(text("""SELECT dst_id, confidence FROM entity_edges WHERE src_type = 'ENTITY_NAME' AND src_id = :n
                                AND rel = 'SUBSIDIARY_OF' AND dst_type = 'SECURITY' AND confidence IN ('HIGH', 'MEDIUM')"""),
                        {"n": k[:64]}).first()
    if edge:
        return edge[0], None, edge[1], "REVIEWED_SUBSIDIARY_EDGE"
    ca = master_db.name_index(conn, "CA").get(k, set())
    if len(ca) == 1:
        return next(iter(ca)), None, "MEDIUM", "EXACT_NAME_CA_LISTING"
    cik, conf, why = name_to_cik(conn, name)
    if cik:
        return f"US:{cik}:COM", cik, conf, "EXACT_NAME_SEC"
    return None, None, None, "AMBIGUOUS" if len(ca) > 1 else why


def _f(x: str | None) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except ValueError:
        return None


def _d(x: str | None) -> date | None:
    try:
        return datetime.strptime(x[:10], "%Y-%m-%d").date() if x else None
    except ValueError:
        return None


def parse_contracts(txt: str) -> list[dict]:
    out = []
    for r in csv.DictReader(io.StringIO(txt.lstrip("﻿"))):
        pub = _d(r.get("publicationDate-datePublication"))
        if not pub:
            continue
        out.append({
            "reference": r.get("referenceNumber-numeroReference"), "amendment": r.get("amendmentNumber-numeroModification") or "000",
            "contract_number": r.get("contractNumber-numeroContrat"), "title": (r.get("title-titre-eng") or "")[:300],
            "publication_date": pub, "award_date": _d(r.get("contractAwardDate-dateAttributionContrat")),
            "start": _d(r.get("contractStartDate-contratDateDebut")), "end": _d(r.get("contractEndDate-dateFinContrat")),
            "amount": _f(r.get("contractAmount-montantContrat")), "total_value": _f(r.get("totalContractValue-valeurTotaleContrat")),
            "currency": r.get("contractCurrency-contratMonnaie"), "amendment_type": r.get("amendmentType-typeModification-eng"),
            "method": r.get("procurementMethod-methodeApprovisionnement-eng"), "notice_type": r.get("noticeType-avisType-eng"),
            "supplier": r.get("supplierLegalName-nomLegalFournisseur-eng"),
            "supplier_standardized": r.get("supplierStandardizedName-nomNormaliseFournisseur-eng"),
            "supplier_country": r.get("supplierAddressCountry-fournisseurAdressePays-eng"),
            "buyer": r.get("contractingEntityName-nomEntitContractante-eng"),
            "end_user": r.get("endUserEntitiesName-nomEntitesUtilisateurFinal-eng"),
            "unspsc": r.get("unspscDescription-eng"),
        })
    return out


def store_contract(conn: Connection, c: dict, *, is_backfill: bool) -> int | None:
    sid, cik, conf, why = resolve_entity(conn, c["supplier"])
    kind = "CONTRACT_WIN" if (c["amendment"] in ("000", "0", "") and (c["amendment_type"] or "Original") == "Original") else "CONTRACT_AMENDMENT"
    key = f"{c['reference']}:{c['amendment']}"
    avail = end_of_local_day(c["publication_date"], "CA")
    records.put(conn, country="CA", dataset="ca_canadabuys_contracts", record_key=key, as_of_date=c["publication_date"],
                available_at=avail, payload={**c, "kind": kind, "mapping": why}, parser_version=PARSER_VERSION,
                license_status="OPEN", security_id=sid, issuer_cik=cik, entity_name=c["supplier"], identity_confidence=conf,
                needs_review=sid is None, source_url="https://canadabuys.canada.ca/en/tender-opportunities")
    if not sid or not (c["total_value"] or 0) > 0:
        return None
    from app.db.store import create_event
    ticker = sid.split(":")[2] if sid.startswith("CA:") else None
    return create_event(conn, event_type=f"CA_GOV_{kind}", dedupe_key=f"CABUY:{key}", public_date=c["publication_date"],
                        ticker=ticker, issuer_cik=cik, company_name=c["supplier"],
                        title=f"{c['supplier']}: {c['currency']} {c['total_value']:,.0f} federal contract ({c['buyer'] or ''})"[:300],
                        payload=c, source_url="https://canadabuys.canada.ca/en/tender-opportunities", is_backfill=is_backfill,
                        country="CA", exchange=sid.split(":")[1] if sid.startswith("CA:") else None, security_id=sid,
                        event_family="CA_GOV_CONTRACT", identity_confidence=conf, parser_version=PARSER_VERSION,
                        available_at_override=avail, date_quality="date")


def parse_grant_row(r: dict) -> dict | None:
    if (r.get("recipient_type") or "").strip() != "F":
        return None  # for-profit businesses only: individuals / NGOs are never stored
    return {"ref": r.get("ref_number"), "amendment": r.get("amendment_number") or "0", "amendment_date": _d(r.get("amendment_date")),
            "agreement_type": r.get("agreement_type"), "recipient": r.get("recipient_legal_name"),
            "recipient_operating": r.get("recipient_operating_name"), "province": r.get("recipient_province"),
            "program": r.get("prog_name_en"), "title": (r.get("agreement_title_en") or "")[:300], "value": _f(r.get("agreement_value")),
            "start": _d(r.get("agreement_start_date")), "end": _d(r.get("agreement_end_date")),
            "description": (r.get("description_en") or "")[:500], "naics": r.get("naics_identifier"), "department": r.get("owner_org_title")}


def store_grant(conn: Connection, g: dict, *, first_seen: datetime, forward: bool) -> int | None:
    sid, cik, conf, why = resolve_entity(conn, g["recipient"])
    return records.put(conn, country="CA", dataset="ca_grants", record_key=f"{g['ref']}:{g['amendment']}",
                       as_of_date=g["amendment_date"] or g["start"], available_at=first_seen, payload={**g, "mapping": why,
                       "point_in_time": "FORWARD_FIRST_SEEN" if forward else "HISTORICAL_NO_PUBLICATION_DATE"},
                       parser_version=PARSER_VERSION, license_status="OPEN", security_id=sid, issuer_cik=cik,
                       entity_name=g["recipient"], identity_confidence=conf, needs_review=(not forward) or sid is None,
                       source_url="https://search.open.canada.ca/grants")


def stream_grants(engine: Engine, *, forward: bool, max_rows: int | None = None) -> dict:
    """Stream the ~2.3 GB CSV line by line (never saved to disk); keep for-profit recipients only."""
    now = datetime.now(timezone.utc)
    seen = stored = 0
    with http._client.stream("GET", GRANTS, headers={"User-Agent": "Mozilla/5.0 (compatible; PublicEdge/1.0)"}, timeout=1800) as r:
        r.raise_for_status()
        lines = (ln for ln in r.iter_lines())
        header = next(csv.reader([next(lines).lstrip("﻿")]))
        batch = []
        for row in csv.reader(lines):
            seen += 1
            if len(row) != len(header):
                continue
            g = parse_grant_row(dict(zip(header, row)))
            if g:
                batch.append(g)
            if len(batch) >= 500:
                with engine.begin() as c:
                    stored += sum(store_grant(c, x, first_seen=now, forward=forward) is not None for x in batch)
                batch = []
            if max_rows and seen >= max_rows:
                break
        if batch:
            with engine.begin() as c:
                stored += sum(store_grant(c, x, first_seen=now, forward=forward) is not None for x in batch)
    return {"rows": seen, "stored": stored}


def parse_valet(payload: dict) -> list[dict]:
    out = []
    for o in payload.get("observations", []):
        d = _d(o.get("d"))
        for k, v in o.items():
            if k == "d" or not isinstance(v, dict):
                continue
            val = _f(v.get("v"))
            if d and val is not None:
                out.append({"series": k, "name": MACRO_SERIES.get(k, k), "date": d, "value": val})
    return out


def sync_valet(engine: Engine, start: date) -> int:
    series = ",".join(MACRO_SERIES)
    obs = parse_valet(http.get(VALET.format(series=series), params={"start_date": start.isoformat()}).json())
    n = 0
    with engine.begin() as c:
        for o in obs:
            n += records.put(c, country="CA", dataset="ca_macro_valet", record_key=f"{o['series']}:{o['date']}",
                             as_of_date=o["date"], available_at=end_of_local_day(o["date"], "CA"), payload=o,
                             parser_version=PARSER_VERSION, license_status="PUBLIC_WITH_TERMS",
                             source_url=f"https://www.bankofcanada.ca/valet/observations/{o['series']}") is not None
    return n


def fiscal_year(d: date) -> str:
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{y + 1}"


class CanadaBuysSource(Source):
    name = "ca_canadabuys"
    interval_seconds = 12 * 3600
    critical = False
    slow = True
    country, dataset, license_status, parser_version = "CA", "ca_canadabuys_contracts", "OPEN", PARSER_VERSION
    stale_after = timedelta(days=35)  # the fiscal-year file is refreshed about monthly

    def run_live(self, engine: Engine) -> RunResult:
        fy = fiscal_year(date.today())
        r = http.get(CB_CONTRACTS.format(fy=fy))
        rows = parse_contracts(r.content.decode("utf-8-sig", "replace"))
        res = RunResult(items=len(rows), watermark={"file": fy, "last_modified": r.headers.get("last-modified")})
        with engine.connect() as c:
            known = {k for (k,) in c.execute(text("SELECT record_key FROM dataset_records WHERE dataset = 'ca_canadabuys_contracts'"))}
        for x in rows:
            if f"{x['reference']}:{x['amendment']}" in known:
                continue
            # the fiscal-year file is refreshed ~monthly: only a notice published in the last 3 days is "live"
            with engine.begin() as c:
                eid = store_contract(c, x, is_backfill=(date.today() - x["publication_date"]).days > 3)
            if eid:
                res.events.append(eid)
        return res


class BankOfCanadaValetSource(Source):
    name = "ca_boc_valet"
    interval_seconds = 12 * 3600
    critical = False
    slow = True
    country, dataset, license_status, parser_version = "CA", "ca_macro_valet", "PUBLIC_WITH_TERMS", PARSER_VERSION
    stale_after = timedelta(days=5)

    def run_live(self, engine: Engine) -> RunResult:
        return RunResult(items=sync_valet(engine, date.today() - timedelta(days=14)))
