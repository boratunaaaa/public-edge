"""Schedule 13D filing classification: a 13D is not by itself an activist signal.

Classes (filing level; a group filing is ONE classification, its members' roles are kept as evidence):
  ACTIVIST_13D               stated, non-hedged demands / intent (board, proxy, sale, governance, management,
                             capital allocation) and the filer is not an insider / control holder, or a filer with a
                             record of such campaigns at other issuers
  INSIDER_13D                a current officer / director of the issuer (Form 3/4/5 in the year around the filing)
  CONTROL_HOLDER_13D         founder / family control, parent company (>= 50 %), controlling shareholder with
                             board-designation rights, trust / family vehicle of an insider, SPAC sponsor, management
                             ownership restructuring
  PASSIVE_OR_STRUCTURAL_13D  investment purposes / financing / merger-consideration receipt / technical amendments
  AMBIGUOUS_13D              conflicting or missing evidence
Confidence HIGH / MEDIUM / LOW; LOW never enters activist statistics.

Item 4 is parsed per SENTENCE: the standard 13D reservation language ("may from time to time ...", "(a) through (j)",
"reserve the right", "no present plans") never produces an activist flag - only stated actions / demands do.
"""
from __future__ import annotations

import re

VERSION = "13d-class-v3.1"
FLAGS = ("board_change", "strategic_alternatives", "sale_request", "capital_allocation", "governance_change",
         "management_change", "proxy_intent", "financing_only", "passive_ownership", "technical_amendment")
STRONG = ("board_change", "proxy_intent", "sale_request", "governance_change", "management_change")
MEDIUM_FLAGS = ("strategic_alternatives", "capital_allocation")

HEDGE = re.compile(r"\bmay\b|\bmight\b|\bcould\b|from time to time|reserves? the right|\(a\)\s*(?:through|-|–|to)\s*\(j\)|"
                   r"clauses? \(a\)|paragraphs? \(a\)|subparagraphs? \(a\)|no (?:present |current )?(?:plans?|proposals?)|"
                   r"do(?:es)? not (?:currently )?have any (?:present )?plans|except as (?:set forth|described|otherwise)|"
                   r"without limitation|intend to review|continue to (?:evaluate|review)|evaluate (?:its|their) investment|"
                   r"in the future|consider(?:ing)? (?:such )?(?:matters|options|alternatives)", re.I)
DEMAND = re.compile(r"\b(?:urg\w*|request\w*|demand\w*|call(?:ed|s|ing)? (?:on|for|upon)|propos\w*|recommend\w*|advocat\w*|"
                    r"nominat\w*|intends? to|will seek|seeks? to|is seeking|delivered (?:a|the) letter|sent (?:a|the) letter|"
                    r"letter to the (?:board|issuer|company)|submitted|press release|public letter|believes? (?:that )?the (?:board|company|issuer) should|"
                    r"entered into (?:a|an) (?:cooperation|settlement|nomination) agreement|filed (?:a )?(?:preliminary )?proxy|"
                    r"ha(?:s|ve) had,? (?:and intends? to continue to have,? )?(?:constructive )?(?:discussions|conversations|engagement)|"
                    r"intends? to (?:engage|communicate|continue (?:to )?(?:engage|discuss|have))|expressed (?:its|their) (?:views|concerns))\b", re.I)
PAT = {
    "board_change": re.compile(r"nominat\w* .{0,200}(?:director|board|nominee)|slate of (?:director )?(?:nominees|candidates|persons)|board (?:seat|representation)|appoint\w* .{0,60}(?:to the board|as (?:a |an )?(?:independent )?director)|"
                               r"(?:reconstitut|refresh|replac)\w* .{0,40}board|remov\w* .{0,40}director|cooperation agreement|settlement agreement|board composition|"
                               r"composition of the board|board (?:refreshment|changes)|add\w* .{0,30}(?:new )?directors", re.I),
    "proxy_intent": re.compile(r"proxy (?:contest|fight|solicitation)|solicit\w* (?:of )?(?:proxies|consents)|consent solicitation|withhold (?:campaign|votes?)|"
                               r"definitive proxy|preliminary proxy|DEFC14A|PREC14A|at the (?:\d{4} )?(?:annual|special) meeting", re.I),
    "sale_request": re.compile(r"(?:sale of|sell) the (?:issuer|company)|take[- ]private|going[- ]private|non-binding (?:proposal|indication)|"
                               r"proposal to acquire|offer to (?:acquire|purchase) (?:all|the)|acquire all (?:of )?the (?:outstanding|remaining)|merge with|"
                               r"sale transaction|potential (?:sale|acquisition) of the (?:issuer|company)|value in a sale|potential buyers?", re.I),
    "governance_change": re.compile(r"declassif\w*|poison pill|rights plan|special meeting|bylaws?|charter amendment|separat\w* .{0,20}(?:chair|ceo)|"
                                    r"(?:corporate )?governance(?: (?:changes?|improvements?|practices|concerns|reforms?|matters))?|say[- ]on[- ]pay|majority voting|executive compensation", re.I),
    "management_change": re.compile(r"(?:replac|remov|chang|terminat)\w* .{0,40}(?:chief executive|ceo|management team|executive officers?|cfo)|new (?:ceo|management)|"
                                    r"leadership change|\([ivx0-9]{1,4}\) management\b|management (?:team )?(?:changes|succession)|operational execution", re.I),
    "strategic_alternatives": re.compile(r"strategic alternatives|strategic review|explore (?:a |the )?(?:sale|strategic)|sale process", re.I),
    "capital_allocation": re.compile(r"capital allocation|special dividend|(?:repurchase|buy[- ]?back) (?:program|of shares)|return (?:of )?capital|"
                                     r"capital (?:structure|return)|reduce (?:costs|expenses)|cost reductions?", re.I),
}
FINANCING = re.compile(r"\b(?:loan|credit (?:agreement|facility)|financing|pledg\w*|margin (?:loan|account)|collateral|note purchase|convertible notes?|"
                       r"private placement|securities purchase agreement|subscription agreement|purchase agreement|preferred (?:stock|shares)|warrants?|"
                       r"payment[- ]in[- ]kind|PIK|rights offering|backstop)\b", re.I)
PASSIVE = re.compile(r"investment purposes|for investment|passive|no (?:present |current )?(?:plans?|proposals?)|portfolio management purposes|liquidity (?:needs|purposes)", re.I)
TECHNICAL = re.compile(r"(?:filed|being filed|occasioned) (?:solely|only)|solely (?:to|as a result)|as a result of (?:a )?(?:change|decrease|increase) in the (?:number|aggregate number) of|"
                       r"(?:dilution|issuance of (?:additional )?shares by the issuer)|(?:less than|below) (?:5|five)(?:\.0)? ?%|ceased to be the beneficial owner|"
                       r"no longer (?:the )?beneficial|(?:this|the) amendment (?:is being filed|reflects) (?:to|solely)", re.I)
STRUCTURAL = re.compile(r"(?:in connection with|pursuant to|as a result of) (?:the )?(?:merger|business combination|closing|spin[- ]?off|reorganization|"
                        r"initial public offering|IPO|de-?SPAC|domestication)|merger consideration|received .{0,80}(?:in|pursuant to) the (?:merger|business combination)|"
                        r"lock[- ]?up|sponsor|founder shares|registration rights|estate planning|\bgifts?\b|distribution to (?:its )?(?:members|partners|limited partners)", re.I)
CONTROL_CUES = re.compile(r"(?:right|entitled) to (?:designate|nominate|appoint) .{0,80}(?:director|board)|stockholders'? agreement|shareholders'? agreement|"
                          r"investor rights agreement|nominating agreement|controlled company|majority (?:stockholder|shareholder|owner)|parent (?:company|entity)|"
                          r"voting agreement|voting trust|family (?:trust|limited partnership|office)|estate of|acquir\w* control|purpose of (?:acquiring|obtaining) control|"
                          r"(?:offtake|collaboration|license|licensing|supply|commercial|mutual support|share purchase|stock purchase) agreement", re.I)


SUPPORT = re.compile(r"support(?:s|ing|ed)? (?:the )?(?:re-?election|incumbent|current (?:board|management)|management|the board|the company's (?:board|nominees))|"
                     r"in support of (?:the )?(?:board|management|incumbent)", re.I)
GENERIC = {"INC", "CORP", "CORPORATION", "COMPANY", "HOLDINGS", "HOLDING", "GROUP", "TRUST", "FUND", "CAPITAL", "PARTNERS", "INTERNATIONAL",
           "GLOBAL", "FINANCIAL", "BANCORP", "BANK", "THE", "AND", "OF", "LLC", "LTD", "AMERICA", "AMERICAN", "ENERGY", "TECHNOLOGIES",
           "TECHNOLOGY", "THERAPEUTICS", "ACQUISITION", "INVESTMENT", "INVESTMENTS", "MANAGEMENT", "SERVICES", "SYSTEMS", "RESOURCES",
           "INDUSTRIES", "FAMILY", "ESTATE", "FOUNDATION", "NATIONAL", "FIRST", "UNITED", "NEW", "LIMITED", "PLC"}


def shares_issuer_name(names: list[str], issuer_name: str | None) -> bool:
    """A founder-family cue: a reporting person's name shares a distinctive token with the issuer (Nordstrom)."""
    tok = lambda s: {w for w in re.findall(r"[A-Z]{4,}", (s or "").upper()) if w not in GENERIC}  # noqa: E731
    it = tok(issuer_name)
    return bool(it) and any(tok(n) & it for n in names)


def split_sentences(text: str) -> list[str]:
    t = re.sub(r"\s+", " ", text or "")
    return [s.strip() for s in re.split(r"(?<=[.;])\s+(?=[A-Z(])", t) if len(s.strip()) > 20]


def item4_features(text: str | None, *, is_amendment: bool = False) -> dict:
    """Structured Item 4 flags + the sentences that triggered them (auditable)."""
    out = {f: False for f in FLAGS}
    ev: dict[str, list[str]] = {}
    hedged: dict[str, int] = {}
    if not text or not text.strip():
        out["empty"] = True
        return out | {"evidence": ev, "hedged_mentions": hedged}
    sents = split_sentences(text)
    for s in sents:
        hedge = bool(HEDGE.search(s)) or bool(SUPPORT.search(s))
        demand = bool(DEMAND.search(s))
        for f, rx in PAT.items():
            if rx.search(s):
                if demand and not hedge:
                    out[f] = True
                    ev.setdefault(f, []).append(s[:300])
                else:
                    hedged[f] = hedged.get(f, 0) + 1
    active = any(out[f] for f in STRONG + MEDIUM_FLAGS)
    full = " ".join(sents)
    if TECHNICAL.search(full) and not active:
        out["technical_amendment"] = True
        ev["technical_amendment"] = [m.group(0) for m in TECHNICAL.finditer(full)][:3]
    out["financing_only_raw"] = bool(re.search(r"investor rights agreement|credit agreement|securities purchase agreement|subscription agreement|"
                                               r"note purchase agreement|financing|backstop", full, re.I))
    if FINANCING.search(full) and not active:
        out["financing_only"] = True
        ev["financing_only"] = [m.group(0) for m in FINANCING.finditer(full)][:3]
    if PASSIVE.search(full) and not active:
        out["passive_ownership"] = True
    out["structural"] = bool(STRUCTURAL.search(full))
    out["control_cues"] = [m.group(0)[:120] for m in CONTROL_CUES.finditer(full)][:3]
    out["empty"] = False
    return out | {"evidence": ev, "hedged_mentions": hedged}


def classify(features: dict, *, insider: dict | None = None, percent: float | None = None, names: list[str] | None = None,
             prior_campaigns: int = 0, is_amendment: bool = False, issuer_name: str | None = None) -> tuple[str, str, list[str]]:
    """(class, confidence, reasons). insider = {"current_officer_director": bool, "former": bool, "ten_pct_only": bool,
    "linked_to_insider": bool}; prior_campaigns = other issuers where this filer made stated activist demands."""
    insider = insider or {}
    names = names or []
    strong = [f for f in STRONG if features.get(f)]
    medium = [f for f in MEDIUM_FLAGS if features.get(f)]
    reasons: list[str] = []
    current = bool(insider.get("current_officer_director"))
    vehicle = any(re.search(r"\b(?:trust|family|foundation|estate|holdings?)\b", n, re.I) for n in names) and bool(insider.get("linked_to_insider"))
    family = shares_issuer_name(names, issuer_name)
    sponsor = any(re.search(r"\bsponsor\b", n, re.I) for n in names)
    control_cue = bool(features.get("control_cues"))
    big = percent is not None and percent >= 50
    large = percent is not None and percent >= 30 and prior_campaigns < 2  # a 30 %+ block without an activist record is a control position
    founder = current and percent is not None and percent >= 20
    activist_record = prior_campaigns >= 2
    # 1. control holders (never activist statistics)
    if (big or large or founder or vehicle or sponsor or family or (control_cue and (current or (percent or 0) >= 10))) and not (strong and activist_record):
        why = [x for x, ok in (("parent/majority >= 50%", big), (">= 30% block without an activist record", large and not big), ("insider with >= 20%", founder), ("trust/family vehicle of an insider", vehicle),
                               ("SPAC sponsor", sponsor), ("founder family (name shared with the issuer)", family),
                               ("board-designation / stockholders' / investor-rights agreement", control_cue)) if ok]
        conf = "HIGH" if (big or founder or (vehicle and current) or sponsor) else "MEDIUM"
        return "CONTROL_HOLDER_13D", conf, why
    # agreement / financing / merger-driven stakes: board rights come from a contract, not a campaign
    deal = bool(control_cue or features.get("structural") or features.get("financing_only_raw"))  # incl. offtake / collaboration / purchase agreements
    if deal and strong and not activist_record and "proxy_intent" not in strong:
        return "PASSIVE_OR_STRUCTURAL_13D", "MEDIUM", ["board / governance terms stem from an agreement, financing or merger - not a campaign"]
    # 2. current insiders
    if current:
        if strong and not activist_record:
            return "AMBIGUOUS_13D", "LOW", ["current officer/director but stated activist demands"]
        if not strong:
            return "INSIDER_13D", "HIGH" if not medium else "MEDIUM", ["current officer/director of the issuer (Form 3/4/5)"]
    # 3. activists: stated demands, or an established campaign record
    if strong:
        reasons = [f"stated {f}" for f in strong] + ([f"{prior_campaigns} prior campaigns"] if prior_campaigns else [])
        conf = "HIGH" if (len(strong) >= 2 or "proxy_intent" in strong or activist_record) else "MEDIUM"
        if insider.get("former"):
            reasons.append("former officer/director (not current)")
        return "ACTIVIST_13D", conf, reasons
    if medium:
        return "ACTIVIST_13D", "MEDIUM" if activist_record else "LOW", [f"stated {f}" for f in medium]
    if activist_record and not (features.get("technical_amendment") or features.get("financing_only")):
        return "ACTIVIST_13D", "LOW", [f"no stated demand in this filing; {prior_campaigns} prior campaigns"]
    # 4. passive / structural
    if features.get("technical_amendment") or features.get("financing_only") or features.get("structural") or features.get("passive_ownership"):
        why = [f for f in ("technical_amendment", "financing_only", "structural", "passive_ownership") if features.get(f)]
        return "PASSIVE_OR_STRUCTURAL_13D", "HIGH" if len(why) >= 2 or "technical_amendment" in why else "MEDIUM", why
    if features.get("empty"):
        return "AMBIGUOUS_13D", "LOW", ["no Item 4 text (amendment not amending Item 4)"]
    return "AMBIGUOUS_13D", "LOW", ["no decisive evidence"]


# ------------------------------------------------------------------ evidence gathering + persistence

INSIDER_WINDOW_BEFORE, INSIDER_WINDOW_AFTER = 365, 30
USABLE = ("HIGH", "MEDIUM")


def investor_key(investor_cik: str | None, investor_name: str | None) -> str | None:
    if investor_cik:
        return investor_cik
    n = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9&]", " ", investor_name or "")).strip().upper()
    return f"NAME:{n}"[:160] if n else None


def insider_status(rows: list, filed) -> dict:
    """rows: Form 3/4/5 owner rows (filed_date, is_director, is_officer, is_ten_pct) of ONE person at THIS issuer."""
    from datetime import timedelta
    cur = former = ten = False
    for fd, d_, o_, t_ in rows:
        if fd is None or filed is None:
            continue
        if (d_ or o_) and filed - timedelta(days=INSIDER_WINDOW_BEFORE) <= fd <= filed + timedelta(days=INSIDER_WINDOW_AFTER):
            cur = True
        elif (d_ or o_) and fd < filed - timedelta(days=INSIDER_WINDOW_BEFORE):
            former = True
        if t_:
            ten = True
    return {"current_officer_director": cur, "former": former and not cur, "ten_pct_only": ten and not cur and not former}


def gather(conn, f: dict, campaign_index: dict | None = None) -> dict:
    """Evidence for one filing (point-in-time: only information dated on/before the filing, plus the Form 3 grace)."""
    from sqlalchemy import text
    filed, issuer = f["filed_date"], f["issuer_cik"]
    persons = conn.execute(text("""SELECT name, cik, kind FROM activist_reporting_persons WHERE accession = :a ORDER BY person_index"""),
                           {"a": f["accession"]}).mappings().all()
    ciks = {p["cik"] for p in persons if p["cik"]} | ({f["investor_cik"]} if f["investor_cik"] and f["investor_cik"] != issuer else set())
    names = [p["name"] for p in persons] or ([f["investor_name"]] if f["investor_name"] else [])
    members, agg = [], {"current_officer_director": False, "former": False, "ten_pct_only": False, "linked_to_insider": False}
    for c in sorted(ciks):
        rows = conn.execute(text("""SELECT filed_date, is_director, is_officer, is_ten_pct FROM insider_filing_owners
                                    WHERE owner_cik = :c AND issuer_cik = :i"""), {"c": c, "i": issuer}).all()
        st = insider_status(rows, filed)
        linked = conn.execute(text("""SELECT 1 FROM beneficial_owner_links l JOIN insider_filing_owners o ON o.owner_cik = l.beneficial_owner_key
                                      AND o.issuer_cik = :i AND (o.is_director OR o.is_officer) WHERE l.entity_key = :c LIMIT 1"""),
                              {"c": c, "i": issuer}).first() is not None
        members.append({"cik": c, **st, "linked_to_insider": linked})
        for k in ("current_officer_director", "former", "ten_pct_only"):
            agg[k] = agg[k] or st[k]
        agg["linked_to_insider"] = agg["linked_to_insider"] or linked
    key = investor_key(f["investor_cik"] if f["investor_cik"] != issuer else None, f["investor_name"])
    prior_other = prior_chain = 0
    if key and campaign_index is not None:
        for iss, d in campaign_index.get(key, []):
            if d < filed:
                if iss == issuer:
                    prior_chain += 1
                else:
                    prior_other += 1
    elif key:
        rows = conn.execute(text("""SELECT issuer_cik FROM thirteen_d_classification c JOIN activist_filings a ON a.accession = c.accession
                                    WHERE c.investor_key = :k AND a.filed_date < :d AND c.filing_class = 'ACTIVIST_13D'
                                      AND c.confidence IN ('HIGH', 'MEDIUM') AND (c.flags->>'stated_strong')::int > 0"""),
                            {"k": key, "d": filed}).scalars().all()
        prior_other = len({r for r in rows if r != issuer})
        prior_chain = sum(1 for r in rows if r == issuer)
    return {"insider": agg, "members": members, "names": names, "investor_key": key,
            "prior_campaign_issuers": prior_other, "prior_chain_demands": prior_chain}


def is_campaign(features: dict, percent: float | None, names: list[str], issuer_name: str | None) -> bool:
    """A filing counts toward a filer's campaign record only when its own text makes it an activist filing (stated demand,
    not an agreement / financing / control stake) - never merely because board terms are mentioned."""
    cls, conf, _ = classify(features, percent=percent, names=names, issuer_name=issuer_name)
    return cls == "ACTIVIST_13D" and conf in USABLE


def classify_filing(conn, f: dict, campaign_index: dict | None = None, persist: bool = True) -> dict:
    """f: an activist_filings row (accession, issuer_cik, investor_cik, investor_name, filed_date, percent, item4_text, is_amendment)."""
    import json as _json

    from sqlalchemy import text
    feats = item4_features(f["item4_text"], is_amendment=f["is_amendment"])
    ev = gather(conn, f, campaign_index)
    record = ev["prior_campaign_issuers"] + (2 if ev["prior_chain_demands"] else 0)
    cls, conf, reasons = classify(feats, insider=ev["insider"], percent=f["percent"], names=ev["names"],
                                  prior_campaigns=record, is_amendment=f["is_amendment"], issuer_name=f.get("issuer_name"))
    stated = sum(1 for x in STRONG if feats.get(x))
    flags = {k: bool(feats.get(k)) for k in FLAGS} | {"structural": bool(feats.get("structural")), "empty": bool(feats.get("empty")),
                                                       "stated_strong": stated, "hedged": feats.get("hedged_mentions")}
    out = {"accession": f["accession"], "issuer_cik": f["issuer_cik"], "investor_key": ev["investor_key"], "filing_class": cls,
           "confidence": conf, "reasons": reasons, "flags": flags,
           "evidence": {"item4_sentences": feats.get("evidence"), "control_cues": feats.get("control_cues"), "insider": ev["insider"],
                        "members": ev["members"], "prior_campaign_issuers": ev["prior_campaign_issuers"],
                        "prior_chain_demands": ev["prior_chain_demands"], "percent": f["percent"]}}
    if persist:
        conn.execute(text("""INSERT INTO thirteen_d_classification (accession, issuer_cik, investor_key, filing_class, confidence, reasons, flags, evidence, version)
                             VALUES (:accession, :issuer_cik, :investor_key, :filing_class, :confidence, CAST(:r AS jsonb), CAST(:fl AS jsonb), CAST(:ev AS jsonb), :v)
                             ON CONFLICT (accession) DO UPDATE SET issuer_cik = EXCLUDED.issuer_cik, investor_key = EXCLUDED.investor_key,
                               filing_class = EXCLUDED.filing_class, confidence = EXCLUDED.confidence, reasons = EXCLUDED.reasons,
                               flags = EXCLUDED.flags, evidence = EXCLUDED.evidence, version = EXCLUDED.version, classified_at = now()"""),
                     out | {"r": _json.dumps(reasons), "fl": _json.dumps(flags), "ev": _json.dumps(out["evidence"], default=str), "v": VERSION})
    return out
