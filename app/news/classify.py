"""Deterministic multi-label news classification, hype filter and materiality.

Pattern rules are intentionally explicit so every label is explainable. An optional LLM
(app.llm) may later re-check only candidates that pass these filters.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------- classes
CLASS_RULES: dict[str, list[str]] = {
    "MAJOR_CUSTOMER": [r"\b(hyperscaler|fortune 500|global automaker|leading (ai|semiconductor|automotive|aerospace) (company|manufacturer|customer)|major (oem|customer))\b",
                       r"\b(selected by|chosen by|awarded by) (a |an )?(leading|major|top|global)\b"],
    # commercial contracts only - financing/employment/legal agreements are not customer contracts
    "NEW_CONTRACT": [r"\b(awarded|wins?|won|secures?|secured|receives?|received|signs?|signed|enters? into|entered into) (a |an |its |the )?(?![^.]{0,60}\b(credit|loan|note|indenture|underwriting|employment|separation|consulting|securities purchase|stock purchase|placement|merger|registration rights|lease)\b)[^.]{0,60}\b(contract|order|purchase order|supply agreement|offtake agreement|customer agreement|production agreement)\b"],
    "GOVERNMENT_CONTRACT": [r"\b(department of (defense|war|energy|the navy|the army|the air force|homeland security)|u\.?s\.? (army|navy|air force|space force|government)|dod|doe|nasa|darpa|diu|afwerx|spacewerx|missile defense agency|federal)\b[^.]{0,80}\b(contract|award|order|task order|idiq|sbir|sttr|ota)\b",
                            r"\b(contract|award|order|task order|idiq|sbir|sttr|ota)\b[^.]{0,80}\b(department of (defense|war|energy)|u\.?s\.? (army|navy|air force|space force|government)|dod|nasa|darpa)\b"],
    "PARTNERSHIP": [r"\b(partnership|partners with|collaboration|collaborate|strategic alliance|joint venture|teaming agreement)\b"],
    "COMMERCIALIZATION": [r"\b(commercial (launch|availability|sales|deployment|shipments?)|first commercial|commercially available|begins? (selling|shipping))\b"],
    "PRODUCTION_START": [r"\b(start(s|ed)? (of )?(production|manufacturing)|begins? (production|manufacturing)|first (production|units|shipment)|mass production|volume production)\b"],
    "CAPACITY_EXPANSION": [r"\b(capacity expansion|expand(s|ed|ing)? (its )?(production |manufacturing )?capacity|new (manufacturing )?(facility|plant|fab|line)|break(s)? ground)\b"],
    "DESIGN_WIN": [r"\bdesign[- ]wins?\b", r"\b(selected|chosen) (as|for) [^.]{0,40}(supplier|platform)\b"],
    "TECH_BREAKTHROUGH": [r"\b(breakthrough|world[- ]first|first[- ]ever|record[- ]breaking|industry[- ]first)\b"],
    "CLINICAL_RESULT": [r"\b(phase (1|2|3|i|ii|iii)[ab]?|topline|primary endpoint|clinical (trial|study) results?)\b"],
    "FDA_APPROVAL": [r"\b(fda (approval|approves|approved|clears|cleared|clearance)|receives? fda|granted (fda )?approval|510\(k\) clearance|breakthrough device designation)\b"],
    # "CRL" alone is also a ticker (Charles River) -> only the spelled-out letter counts
    "FDA_REJECTION": [r"\b(complete response letter|refus(al|e) to file|fda (declined|rejects?|rejected))\b"],
    "REGULATORY": [r"\b(regulator|sec (investigation|subpoena)|warning letter|consent order|nrc (approval|license|construction permit))\b"],
    "M_AND_A": [r"\b(agree[sd]? to acquire|to be acquired by|definitive (merger )?agreement to (acquire|be acquired|merge)|"
                r"completes? (the |its )?(acquisition|merger)|announces? (the )?(acquisition|merger)|merger agreement|"
                r"to merge with|tender offer (for|to acquire) (all|any and all|the) (of the )?(outstanding )?(shares|common stock)|"
                r"all-(cash|stock) (transaction|deal)|take[- ]private)\b"],
    "ACTIVIST": [r"\b(activist|proxy (fight|contest)|nominat\w+ .{0,30}directors?|schedule 13d)\b"],
    "CAPITAL_RAISE": [r"\b(public offering|private placement|registered direct|pricing of|priced (an? )?offering|at[- ]the[- ]market|convertible (senior )?notes|pipe financing|credit facility|term loan|senior (secured )?notes offering)\b"],
    "DILUTION": [r"\b(public offering|registered direct|at[- ]the[- ]market|warrants?|private placement of (common )?shares)\b"],
    "BANKRUPTCY_RISK": [r"\b(chapter 11|chapter 7|bankruptcy|going concern|forbearance|default notice|delist\w*)\b"],
    "LAWSUIT": [r"\b(lawsuit|class action|complaint filed|litigation|sued)\b"],
    "GOVERNMENT_SUPPORT": [r"\b((federal|government|doe|department of energy|nasa|dod|darpa|nih|nsf|state) (grant|loan|funding|award|support)|"
                           r"loan (guarantee|programs office)|chips act|inflation reduction act|advanced manufacturing tax credit|"
                           r"dpa title iii|defense production act|federal (funding|support)|selected to receive)\b"],
    "GOVERNMENT_EQUITY": [r"\b(government|department of [a-z]+|u\.?s\.?) (equity stake|equity investment|to take a stake|takes? (a )?stake)\b"],
    "MANAGEMENT_CHANGE": [r"\b(appoints?|appointed|names?|named|resigns?|resigned|steps? down|retire(s|ment)?|elects?|elected)\b[^.]{0,60}\b(ceo|cfo|coo|cto|chief|president|chair\w*|board of directors|director)\b"],
    "INSIDER_EVENT": [r"\b(insider (buying|purchase|selling)|form 4|purchased shares in the open market)\b"],
    "EARNINGS": [r"\b(reports? (first|second|third|fourth)[- ]quarter|quarterly results|fiscal (year|quarter) \d{4} results|earnings)\b"],
    "GUIDANCE_RAISE": [r"\b(raises?|raised|increases?|increased|boosts?) [^.]{0,40}\b(guidance|outlook|forecast)\b"],
    "GUIDANCE_CUT": [r"\b(lowers?|lowered|cuts?|reduces?|reduced|withdraws?) [^.]{0,40}\b(guidance|outlook|forecast)\b"],
    "SUPPLY_SHORTAGE": [r"\b(shortage|supply constraints?|supply[- ]constrained|product allocation|on allocation)\b"],
    "CUSTOMER_LOSS": [r"\b(terminat(es|ed|ion) (of )?(the |its |a )?[^.]{0,30}(contract|agreement)|loss of (a |its )?(major |largest )?customer|cancel(s|led|lation) (of )?(the |its )?order)\b"],
    "PRODUCT_LAUNCH": [r"\b(launch(es|ed)?|unveil(s|ed)?|introduc(es|ed)) [^.]{0,40}\b(product|platform|system|solution|chip|drug|device)\b"],
    "RECALL": [r"\brecall(s|ed)?\b"],
    "CYBERSECURITY": [r"\b(cyber(security)? incident|ransomware|data breach|unauthorized access)\b"],
}
MATERIAL_CLASSES = {"MAJOR_CUSTOMER", "NEW_CONTRACT", "GOVERNMENT_CONTRACT", "COMMERCIALIZATION", "PRODUCTION_START",
                    "CAPACITY_EXPANSION", "DESIGN_WIN", "FDA_APPROVAL", "FDA_REJECTION", "M_AND_A", "ACTIVIST",
                    "GOVERNMENT_EQUITY", "BANKRUPTCY_RISK", "GUIDANCE_RAISE", "GUIDANCE_CUT", "CUSTOMER_LOSS",
                    "CLINICAL_RESULT", "GOVERNMENT_SUPPORT"}


def classify(text: str) -> list[str]:
    t = (text or "").lower()
    out = [c for c, pats in CLASS_RULES.items() if any(re.search(p, t) for p in pats)]
    return out or ["OTHER"]


# --------------------------------------------------------------------- hype filter
# most binding first. The first match wins.
AGREEMENT_RULES: list[tuple[str, str]] = [
    ("mou", r"\b(memorandum of understanding|\bmou\b|non-binding|nonbinding)\b"),
    ("loi", r"\b(letter of intent|\bloi\b)\b"),
    ("evaluation", r"\b(evaluation (agreement|unit|program)|evaluat(e|ing|ion) (our|its|the) |testing (agreement|program)|sample (order|shipment))\b"),
    ("pilot", r"\b(pilot (program|project|order|deployment)|proof[- ]of[- ]concept|\bpoc\b)\b"),
    ("commercial_supply_agreement", r"\b((long[- ]term |multi[- ]year )?(supply|offtake|take[- ]or[- ]pay|production|purchase) agreement)\b"),
    ("purchase_order", r"\b(purchase order|\bpo\b for|follow[- ]on order|production order|firm order)\b"),
    ("binding_contract", r"\b((awarded|signed|executed|entered into) (a |an )?[^.]{0,40}\bcontract\b|definitive agreement|task order|\bidiq\b)\b"),
    ("paid_development", r"\b(paid (development|engineering)|development (contract|agreement)|nre (contract|payment))\b"),
    ("framework_agreement", r"\b(framework agreement|master (services|supply|purchase) agreement|\bmsa\b)\b"),
    ("collaboration", r"\b(collaboration|strategic partnership|partnership|alliance|collaborate)\b"),
]
BINDING = {"binding_contract", "purchase_order", "commercial_supply_agreement", "paid_development"}
AGREEMENT_WEIGHT = {"binding_contract": 1.0, "purchase_order": 1.0, "commercial_supply_agreement": 1.0,
                    "paid_development": 0.8, "framework_agreement": 0.5, "pilot": 0.35, "evaluation": 0.25,
                    "collaboration": 0.2, "loi": 0.2, "mou": 0.15, None: 0.5}


def agreement_type(text: str) -> str | None:
    t = (text or "").lower()
    hits = [name for name, rx in AGREEMENT_RULES if re.search(rx, t)]
    if not hits:
        return None
    # explicit non-binding language overrides any contract wording in the same release
    for weak in ("mou", "loi"):
        if weak in hits:
            return weak
    # most specific binding type wins over the generic "definitive agreement / contract" wording
    for strong in ("commercial_supply_agreement", "purchase_order", "paid_development", "binding_contract",
                   "framework_agreement", "pilot", "evaluation", "collaboration"):
        if strong in hits:
            return strong
    return hits[0]


# --------------------------------------------------------------------- materiality
BUCKETS = [("MICRO", 50e6, 300e6), ("EARLY_SMALL", 300e6, 1e9), ("SMALL", 1e9, 3e9), ("GROWTH", 3e9, 10e9)]


def mcap_bucket(mcap: float | None) -> str | None:
    if mcap is None:
        return None
    if mcap < 50e6:
        return "BELOW_UNIVERSE"
    for name, lo, hi in BUCKETS:
        if lo <= mcap < hi:
            return name
    return "ALREADY_SCALED"


CLASS_BASE = {"FDA_APPROVAL": 3, "FDA_REJECTION": 3, "M_AND_A": 3, "BANKRUPTCY_RISK": 3, "GOVERNMENT_EQUITY": 3,
              "MAJOR_CUSTOMER": 2, "NEW_CONTRACT": 2, "GOVERNMENT_CONTRACT": 2, "CUSTOMER_LOSS": 2, "GUIDANCE_CUT": 2,
              "GUIDANCE_RAISE": 2, "CLINICAL_RESULT": 2, "PRODUCTION_START": 2, "COMMERCIALIZATION": 2, "DESIGN_WIN": 2,
              "ACTIVIST": 2, "CAPACITY_EXPANSION": 1, "GOVERNMENT_SUPPORT": 1, "CAPITAL_RAISE": 1, "DILUTION": 1,
              "PARTNERSHIP": 0, "PRODUCT_LAUNCH": 0, "EARNINGS": 1, "MANAGEMENT_CHANGE": 1, "LAWSUIT": 1,
              "RECALL": 1, "CYBERSECURITY": 1, "TECH_BREAKTHROUGH": 0, "SUPPLY_SHORTAGE": 1, "REGULATORY": 1,
              "INSIDER_EVENT": 0, "OTHER": 0}
LEVELS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


@dataclass
class Materiality:
    level: str
    score: float
    reasons: list[str] = field(default_factory=list)


def materiality(classes: list[str], *, agreement: str | None, contract_value: float | None, market_cap: float | None,
                annual_revenue: float | None, tier: int) -> Materiality:
    reasons = []
    base = max((CLASS_BASE.get(c, 0) for c in classes), default=0)
    score = float(base)
    ratio_mcap = (contract_value / market_cap) if (contract_value and market_cap) else None
    ratio_rev = (contract_value / annual_revenue) if (contract_value and annual_revenue) else None
    if ratio_mcap is not None:
        bump = 3 if ratio_mcap >= 0.25 else 2 if ratio_mcap >= 0.10 else 1 if ratio_mcap >= 0.02 else -1 if ratio_mcap < 0.002 else 0
        score += bump
        reasons.append(f"contract/market_cap={ratio_mcap:.1%}")
    if ratio_rev is not None:
        score += 2 if ratio_rev >= 0.5 else 1 if ratio_rev >= 0.15 else 0
        reasons.append(f"contract/annual_revenue={ratio_rev:.1%}")
    contractish = {"NEW_CONTRACT", "MAJOR_CUSTOMER", "GOVERNMENT_CONTRACT", "PARTNERSHIP", "DESIGN_WIN"} & set(classes)
    if contractish:
        w = AGREEMENT_WEIGHT.get(agreement, 0.5)
        if w < 0.5:
            score = min(score, 1.0)  # hype filter: MOU/LOI/pilot/collaboration cannot be HIGH on wording alone
            reasons.append(f"non-binding ({agreement}) - capped")
        elif agreement in BINDING:
            reasons.append(f"binding ({agreement})")
    bucket = mcap_bucket(market_cap)
    if bucket == "ALREADY_SCALED" and base < 3:
        score -= 1
        reasons.append("large cap context")
    if tier >= 4:
        score -= 1
        reasons.append("low-tier source")
    idx = 0 if score < 1.5 else 1 if score < 2.5 else 2 if score < 4 else 3
    return Materiality(LEVELS[idx], score, reasons)
