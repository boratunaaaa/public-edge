"""Form 4 reporting-owner resolution and the beneficial-owner graph.

SEC Form 4 facts that drive this module:
  * a joint filing lists several reportingOwner elements, but the transaction rows carry NO owner
    reference - every row is reported jointly by all filers;
  * which filer actually holds a row is stated, when at all, in its ownership text
    (natureOfOwnership, e.g. "By Ault & Company, Inc.") or in its footnotes;
  * trusts, LLCs, family vehicles and funds that report next to a natural person are usually the
    same economic (beneficial) owner and must not be counted as independent insiders.
So a row is attributed to one owner only when the text names exactly one of them; otherwise it
stays JOINT (owner_cik NULL) - it is never silently given to the first-listed ("primary") owner.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app.db.models import beneficial_owner_links, insider_filing_owners

ENTITY_RE = re.compile(
    r"\b(TRUST|TR|TTEE|TRUSTEES?|LLC|L\.L\.C|LP|L\.P|LLP|FUNDS?|PARTNERS(HIP)?|CAPITAL|HOLDINGS?|FOUNDATION|INC|"
    r"CORP(ORATION)?|CO|LTD|LIMITED|MANAGEMENT|ADVISORS?|GROUP|ESTATE|IRA|ASSOCIATES|INVESTMENTS?|VENTURES?|"
    r"AUTHORITY|BANK|RSC|GMBH|AG|SA|PLC|N\.V|B\.V|SICAV|OFFICE|COMPANY|ENTERPRISES|INTERNATIONAL)\b\.?", re.I)
GENERIC = {"the", "of", "and", "&", "inc", "llc", "lp", "ltd", "co", "corp", "company", "trust", "fund", "funds", "group",
           "holdings", "holding", "capital", "partners", "management", "limited", "l.p.", "l.l.c.", "by", "for", "see",
           "footnote", "footnotes", "a", "an", "et", "al", "de", "can", "i", "ii", "iii", "iv", "jr", "sr"}
VEHICLE_RE = re.compile(r"\b(TRUST|FAMILY|FOUNDATION|LLC|LP|L\.P|PARTNERS(HIP)?|HOLDINGS?|IRA|ESTATE|INVESTMENTS?)\b", re.I)
CONF_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


@dataclass
class OwnerRef:
    cik: str
    name: str
    is_director: bool = False
    is_officer: bool = False
    is_ten_pct: bool = False
    officer_title: str | None = None
    is_ceo: bool = False
    is_cfo: bool = False

    @property
    def is_entity(self) -> bool:
        return is_entity(self.name)


def is_entity(name: str | None) -> bool:
    return bool(name and ENTITY_RE.search(name))


def _words(s: str | None) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if w]


def mentioned(owner_name: str, text: str | None) -> bool:
    """Does the row's ownership text / footnote name this reporting owner?"""
    if not text:
        return False
    tw = set(_words(text))
    ws = _words(owner_name)
    if not ws:
        return False
    if is_entity(owner_name):
        distinct = [w for w in ws if w not in GENERIC and not w.isdigit()] or [w for w in ws if w not in GENERIC]
        return bool(distinct) and all(w in tw for w in distinct[:3])
    # SEC person names are "LAST FIRST MIDDLE": require last + first name
    last, first = ws[0], (ws[1] if len(ws) > 1 else None)
    return last in tw and (first is None or first in tw)


def attribute(owners: list[OwnerRef], ownership_text: str | None) -> tuple[OwnerRef | None, str]:
    """(owner, attribution) for one transaction row."""
    if not owners:
        return None, "unknown"
    if len(owners) == 1:
        return owners[0], "single"
    hits = [o for o in owners if mentioned(o.name, ownership_text)]
    if len(hits) == 1:
        return hits[0], "footnote"
    return None, "joint"


def joint_roles(owners: list[OwnerRef]) -> dict:
    """Role flags of a jointly reported row: every joint filer reports beneficial ownership of it,
    so an officer/director among them makes it an officer/director purchase."""
    return {"is_director": any(o.is_director for o in owners), "is_officer": any(o.is_officer for o in owners),
            "is_ten_pct": any(o.is_ten_pct for o in owners), "is_ceo": any(o.is_ceo for o in owners),
            "is_cfo": any(o.is_cfo for o in owners),
            "officer_title": next((o.officer_title for o in owners if o.officer_title), None)}


def joint_display_name(owners: list[OwnerRef]) -> str:
    return "; ".join(o.name for o in owners)[:300] + " (ortak bildirim)"


# ------------------------------------------------------------------ graph

def filing_links(owners: list[OwnerRef], accession: str, issuer_cik: str | None) -> list[dict]:
    """Links implied by one joint filing: its filers share a beneficial-ownership group."""
    if len(owners) < 2:
        return []
    persons = [o for o in owners if not o.is_entity]
    if len(persons) == 1:
        root, conf = persons[0], "HIGH"  # one natural person + his/her vehicles
    else:
        root, conf = owners[0], "MEDIUM"  # institutional group / several principals: designated filer is the root
    out = []
    for o in owners:
        if o.cik == root.cik:
            continue
        out.append({"entity_key": o.cik, "entity_name": o.name[:300], "beneficial_owner_key": root.cik,
                    "beneficial_owner_name": root.name[:300], "relationship_type": "JOINT_FILER", "confidence": conf,
                    "source": f"form4 joint filing {accession}", "issuer_cik": issuer_cik})
    return out


def name_links(owners_at_issuer: list[OwnerRef], issuer_cik: str) -> list[dict]:
    """Vehicles named after an insider at the same issuer (e.g. 'John A Smith Revocable Trust',
    'Smith Family LP' when exactly one insider is named Smith)."""
    persons = [o for o in owners_at_issuer if not o.is_entity]
    by_last: dict[str, list[OwnerRef]] = defaultdict(list)
    for p in persons:
        ws = _words(p.name)
        if ws:
            by_last[ws[0]].append(p)
    out = []
    for ent in (o for o in owners_at_issuer if o.is_entity):
        ew = set(_words(ent.name))
        full = [p for p in persons if len(_words(p.name)) >= 2 and _words(p.name)[0] in ew and _words(p.name)[1] in ew]
        if len({p.cik for p in full}) == 1:
            p = full[0]
            out.append({"entity_key": ent.cik, "entity_name": ent.name[:300], "beneficial_owner_key": p.cik,
                        "beneficial_owner_name": p.name[:300], "relationship_type": "NAMED_TRUST", "confidence": "HIGH",
                        "source": "name rule: vehicle carries the insider's full name", "issuer_cik": issuer_cik})
            continue
        if VEHICLE_RE.search(ent.name):
            cands = {p.cik: p for last, ps in by_last.items() if last in ew and len(last) >= 3 for p in ps}
            if len(cands) == 1:
                p = next(iter(cands.values()))
                out.append({"entity_key": ent.cik, "entity_name": ent.name[:300], "beneficial_owner_key": p.cik,
                            "beneficial_owner_name": p.name[:300], "relationship_type": "FAMILY_VEHICLE", "confidence": "MEDIUM",
                            "source": "name rule: family vehicle, single insider with that surname", "issuer_cik": issuer_cik})
    return out


def save_links(conn: Connection, links: list[dict]) -> int:
    n = 0
    for lk in links:
        st = pg_insert(beneficial_owner_links).values(**lk)
        res = conn.execute(st.on_conflict_do_nothing(index_elements=["entity_key", "beneficial_owner_key", "relationship_type"])
                           .returning(beneficial_owner_links.c.id)).first()
        n += res is not None
    return n


def save_filing_owners(conn: Connection, accession: str, owners: list[OwnerRef], issuer_cik: str | None, filed) -> None:
    for i, o in enumerate(owners):
        if not o.cik:
            continue
        vals = dict(owner_index=i, owner_name=o.name[:500], is_director=o.is_director, is_officer=o.is_officer,
                    is_ten_pct=o.is_ten_pct, officer_title=o.officer_title, is_ceo=o.is_ceo, is_cfo=o.is_cfo,
                    is_entity=o.is_entity, issuer_cik=issuer_cik, filed_date=filed)
        conn.execute(pg_insert(insider_filing_owners).values(accession=accession, owner_cik=o.cik, **vals)
                     .on_conflict_do_update(index_elements=["accession", "owner_cik"], set_=vals))
    save_links(conn, filing_links(owners, accession, issuer_cik))
    if issuer_cik:
        # vehicles named after an insider of the same issuer (trusts, family LLCs) - issuer-wide rule
        save_links(conn, name_links(issuer_owner_refs(conn, issuer_cik), issuer_cik))


def issuer_owner_refs(conn: Connection, issuer_cik: str) -> list[OwnerRef]:
    rows = conn.execute(select(insider_filing_owners.c.owner_cik, insider_filing_owners.c.owner_name)
                        .where(insider_filing_owners.c.issuer_cik == issuer_cik).distinct()).all()
    seen: dict[str, OwnerRef] = {}
    for cik, name in rows:
        seen.setdefault(cik, OwnerRef(cik, name))
    return list(seen.values())


class _UF:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def beneficial_roots(conn: Connection, keys: set[str], min_confidence: str = "MEDIUM") -> dict[str, str]:
    """owner key -> beneficial-owner root (union-find over links at or above min_confidence).
    Keys of the form 'JOINT:cik1,cik2' (jointly reported rows) join all their filers."""
    uf = _UF()
    ciks: set[str] = set()
    for k in keys:
        if k.startswith("JOINT:"):
            parts = [c for c in k[6:].split(",") if c]
            ciks.update(parts)
            for c in parts[1:]:
                uf.union(parts[0], c)
            if parts:
                uf.union(k, parts[0])
        else:
            ciks.add(k)
    frontier = set(ciks)
    seen: set[str] = set()
    ok = {c for c, r in CONF_RANK.items() if r >= CONF_RANK[min_confidence]}
    for _ in range(4):  # links are shallow; four hops cover person -> trust -> LLC -> fund
        frontier -= seen
        if not frontier:
            break
        seen |= frontier
        rows = conn.execute(select(beneficial_owner_links.c.entity_key, beneficial_owner_links.c.beneficial_owner_key,
                                   beneficial_owner_links.c.confidence).where(or_(
            beneficial_owner_links.c.entity_key.in_(list(frontier)),
            beneficial_owner_links.c.beneficial_owner_key.in_(list(frontier))))).all()
        nxt: set[str] = set()
        for e, b, conf in rows:
            if conf in ok:
                uf.union(e, b)
                nxt.update((e, b))
        frontier = nxt
    return {k: uf.find(k) for k in keys}


def independent_owner_count(conn: Connection, keys: set[str]) -> int:
    if not keys:
        return 0
    return len(set(beneficial_roots(conn, keys).values()))


def owner_key(owner_cik: str | None, owner_name: str | None, joint_ciks: list[str] | None) -> str:
    if owner_cik:
        return owner_cik
    if joint_ciks:
        return "JOINT:" + ",".join(sorted(joint_ciks))
    return "NAME:" + (owner_name or "?")[:60]
