"""Bottleneck / commercialization language, counterparty relations and themes (deterministic).

Keyword lists are a *starting vocabulary*, not the definition of the universe: every matched
sentence is stored as evidence, and `emerging_terms` surfaces new recurring technical phrases
that are not in the theme taxonomy yet.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass

# ------------------------------------------------------------------ bottleneck / commercial signals
# label -> (sentence regex, stage hint or None)
SIGNALS: dict[str, tuple[str, str | None]] = {
    "SOLE_SUPPLIER": (r"\b(sole[- ]source|sole supplier|only (domestic|u\.?s\.?|american|north american) (producer|supplier|manufacturer)|one of (only )?(a )?few (suppliers|producers|manufacturers))\b", None),
    "LIMITED_SUPPLY_BASE": (r"\b(limited (number of )?(suppliers|supplier base)|few qualified suppliers)\b", None),
    "CAPACITY_CONSTRAINED": (r"\b((supply|capacity)[- ]constrained|demand (exceeds|outpaces|outstrips) (our )?(capacity|supply)|sold[- ]out (capacity|production)|fully (booked|allocated))\b", "PRODUCTION_RAMP"),
    "BACKLOG": (r"\b(order backlog|backlog (of|was|increased|grew|reached)|record backlog|book[- ]to[- ]bill)\b", None),
    "CAPACITY_EXPANSION": (r"\b(capacity expansion|expand(ing|ed)? (our )?(production|manufacturing) capacity|new (manufacturing|production) (line|facility|plant)|second (fab|facility|production line))\b", "PRODUCTION_RAMP"),
    "FIRST_COMMERCIAL_PRODUCTION": (r"\b(first commercial (production|shipment|sale|delivery|units?)|commenced commercial (production|operations|sales)|began commercial (production|shipments))\b", "EARLY_COMMERCIAL"),
    "COMMERCIAL_SCALE": (r"\b(commercial[- ]scale (production|manufacturing|facility|plant))\b", "PRODUCTION_RAMP"),
    "PILOT_PRODUCTION": (r"\b(pilot (production|line|plant|facility|manufacturing)|low[- ]rate initial production|\blrip\b)\b", "PILOT"),
    "QUALIFICATION": (r"\b((customer |product )?qualification (completed|complete|process|testing|program)|(completed|passed|achieved) qualification|qualified (supplier|vendor|by))\b", "CUSTOMER_QUALIFICATION"),
    "DESIGN_WIN": (r"\bdesign[- ]wins?\b", "CUSTOMER_QUALIFICATION"),
    "MASS_PRODUCTION": (r"\b(mass production|high[- ]volume (manufacturing|production)|volume production)\b", "MASS_PRODUCTION"),
    "PRODUCTION_RAMP": (r"\b(production ramp|ramp(ing|ed)? (up )?(production|manufacturing)|yield improvement)\b", "PRODUCTION_RAMP"),
    "LONG_TERM_SUPPLY": (r"\b(long[- ]term supply agreement|multi[- ]year supply (agreement|contract)|take[- ]or[- ]pay|offtake agreement|minimum purchase commitment|capacity reservation)\b", "EARLY_COMMERCIAL"),
    "STRATEGIC_INVESTMENT": (r"\b(strategic investment|equity investment) (from|by)\b", None),
    "GOVERNMENT_BACKED": (r"\b(government[- ]backed|defense production act|dpa title iii|chips act|department of energy loan|loan programs office|critical mineral|national security|domestic supply chain|reshoring)\b", None),
    "FIRST_OF_KIND": (r"\b(first[- ]of[- ]a[- ]kind|first[- ]of[- ]its[- ]kind|industry[- ]first|world'?s first)\b", None),
    "CLINICAL": (r"\b(phase (1|2|3|i|ii|iii)[ab]? (trial|study)|clinical trial|investigational new drug|\bind\b application)\b", "RESEARCH"),
    "PRE_REVENUE": (r"\b(have not generated (any )?(significant )?revenue|pre[- ]revenue|no revenue from product sales)\b", "PRE-COMMERCIAL"),
    "RESEARCH_STAGE": (r"\b(research and development stage|early[- ]stage (technology|company)|prototype)\b", "RESEARCH"),
}

HEDGE_RE = re.compile(r"\b(may|might|could|potential(ly)?|expect(s|ed)? to|intend(s)? to|plan(s|ned)? to|anticipate(s)?|no assurance|there can be no|if we|seek(s|ing)? to|aim(s)? to|hope)\b", re.I)

STAGES = ["RESEARCH", "PRE-COMMERCIAL", "PILOT", "CUSTOMER_QUALIFICATION", "EARLY_COMMERCIAL", "PRODUCTION_RAMP",
          "MASS_PRODUCTION", "SCALED"]

# ------------------------------------------------------------------ counterparties
COUNTERPARTIES: dict[str, tuple[str, str]] = {  # alias -> (canonical, category)
    "tesla": ("Tesla", "AUTOMOTIVE_OEM"), "spacex": ("SpaceX", "SPACE_PRIME"), "nvidia": ("NVIDIA", "TOP_SEMICONDUCTOR"),
    "amd": ("AMD", "TOP_SEMICONDUCTOR"), "advanced micro devices": ("AMD", "TOP_SEMICONDUCTOR"),
    "tsmc": ("TSMC", "TOP_SEMICONDUCTOR"), "taiwan semiconductor": ("TSMC", "TOP_SEMICONDUCTOR"),
    "intel": ("Intel", "TOP_SEMICONDUCTOR"), "samsung": ("Samsung", "TOP_SEMICONDUCTOR"),
    "sk hynix": ("SK hynix", "TOP_SEMICONDUCTOR"), "micron": ("Micron", "TOP_SEMICONDUCTOR"),
    "apple": ("Apple", "BIG_TECH"), "microsoft": ("Microsoft", "HYPERSCALER"), "amazon": ("Amazon", "HYPERSCALER"),
    "aws": ("Amazon", "HYPERSCALER"), "amazon web services": ("Amazon", "HYPERSCALER"), "google": ("Google", "HYPERSCALER"),
    "alphabet": ("Google", "HYPERSCALER"), "meta platforms": ("Meta", "HYPERSCALER"), "meta": ("Meta", "HYPERSCALER"),
    "oracle": ("Oracle", "HYPERSCALER"), "openai": ("OpenAI", "LEADING_AI"), "anthropic": ("Anthropic", "LEADING_AI"),
    "xai": ("xAI", "LEADING_AI"),
    "general motors": ("GM", "AUTOMOTIVE_OEM"), "ford": ("Ford", "AUTOMOTIVE_OEM"), "toyota": ("Toyota", "AUTOMOTIVE_OEM"),
    "volkswagen": ("Volkswagen", "AUTOMOTIVE_OEM"), "stellantis": ("Stellantis", "AUTOMOTIVE_OEM"), "bmw": ("BMW", "AUTOMOTIVE_OEM"),
    "hyundai": ("Hyundai", "AUTOMOTIVE_OEM"), "honda": ("Honda", "AUTOMOTIVE_OEM"), "mercedes-benz": ("Mercedes-Benz", "AUTOMOTIVE_OEM"),
    "lockheed martin": ("Lockheed Martin", "DEFENSE_PRIME"), "rtx": ("RTX", "DEFENSE_PRIME"), "raytheon": ("RTX", "DEFENSE_PRIME"),
    "northrop grumman": ("Northrop Grumman", "DEFENSE_PRIME"), "general dynamics": ("General Dynamics", "DEFENSE_PRIME"),
    "boeing": ("Boeing", "DEFENSE_PRIME"), "l3harris": ("L3Harris", "DEFENSE_PRIME"), "anduril": ("Anduril", "DEFENSE_PRIME"),
    "department of defense": ("DoD", "US_GOVERNMENT"), "dod": ("DoD", "US_GOVERNMENT"), "department of war": ("DoD", "US_GOVERNMENT"),
    "u.s. army": ("DoD", "US_GOVERNMENT"), "u.s. navy": ("DoD", "US_GOVERNMENT"), "u.s. air force": ("DoD", "US_GOVERNMENT"),
    "u.s. space force": ("DoD", "US_GOVERNMENT"), "darpa": ("DoD", "US_GOVERNMENT"), "missile defense agency": ("DoD", "US_GOVERNMENT"),
    "department of energy": ("DOE", "US_GOVERNMENT"), "doe": ("DOE", "US_GOVERNMENT"), "nasa": ("NASA", "US_GOVERNMENT"),
    "pfizer": ("Pfizer", "LARGE_PHARMA"), "eli lilly": ("Eli Lilly", "LARGE_PHARMA"), "novo nordisk": ("Novo Nordisk", "LARGE_PHARMA"),
    "merck": ("Merck", "LARGE_PHARMA"), "johnson & johnson": ("Johnson & Johnson", "LARGE_PHARMA"), "abbvie": ("AbbVie", "LARGE_PHARMA"),
    "astrazeneca": ("AstraZeneca", "LARGE_PHARMA"), "roche": ("Roche", "LARGE_PHARMA"), "novartis": ("Novartis", "LARGE_PHARMA"),
    "bristol myers squibb": ("Bristol Myers Squibb", "LARGE_PHARMA"), "sanofi": ("Sanofi", "LARGE_PHARMA"), "gsk": ("GSK", "LARGE_PHARMA"),
    "applied materials": ("Applied Materials", "TOP_SEMICONDUCTOR"), "asml": ("ASML", "TOP_SEMICONDUCTOR"),
    "lam research": ("Lam Research", "TOP_SEMICONDUCTOR"), "broadcom": ("Broadcom", "TOP_SEMICONDUCTOR"),
}
_CP_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(sorted((re.escape(k) for k in COUNTERPARTIES), key=len, reverse=True)) + r")(?![A-Za-z])", re.I)
# short / ambiguous aliases only count in these exact spellings ("Doe", "meta-analysis", "apple juice" excluded)
_EXACT_CASE = {"dod": {"DoD", "DOD"}, "doe": {"DOE"}, "amd": {"AMD"}, "aws": {"AWS"}, "meta": {"Meta"},
               "ford": {"Ford"}, "rtx": {"RTX"}, "gsk": {"GSK"}, "bmw": {"BMW"}, "xai": {"xAI"}, "tsmc": {"TSMC"},
               "nasa": {"NASA"}, "darpa": {"DARPA"}, "intel": {"Intel"}, "apple": {"Apple"}, "amazon": {"Amazon"},
               "oracle": {"Oracle"}, "merck": {"Merck"}, "micron": {"Micron"}, "google": {"Google"}, "tesla": {"Tesla"}}

# relation rules evaluated in order on the sentence text around a counterparty mention
RELATION_RULES: list[tuple[str, str]] = [
    ("competitor", r"\b(compet(e|es|ing|itor|itors|ition) (with|from|against)|competitors (include|such as)|compete)\b"),
    ("investor", r"\b(invest(ed|ment) (by|from)|led by .{0,40} invest|strategic investor|equity stake (from|by))\b"),
    ("supplier", r"\b(we (purchase|source|buy|procure)|supplied by|our supplier|sources? .{0,30} from)\b"),
    ("testing_partner", r"\b(testing|evaluat(e|es|ing|ion)|sample(s)?|trial|pilot)\b"),
    ("customer", r"\b(customer|customers|purchase order|order(s|ed)? from|sells? to|supply agreement|awarded (by|to)|contract (with|from)|delivered to|shipments? to)\b"),
    ("partner", r"\b(partner(ship|s|ed)?|collaborat\w+|joint development|teaming)\b"),
]
AGREEMENT_SENT_RULES: list[tuple[str, str]] = [
    ("mou", r"\b(memorandum of understanding|\bmou\b|non[- ]binding)\b"),
    ("loi", r"\b(letter of intent|\bloi\b)\b"),
    ("production_agreement", r"\b(production (agreement|contract|order)|supply agreement|offtake|take[- ]or[- ]pay|purchase order|firm order)\b"),
    # no leading \b: "$" is not a word character, so \b before it never matches after a space
    ("paid_contract", r"(\bcontract (valued|worth|for \$)|\bawarded .{0,30}\$|\$[0-9.,]+ ?(million|billion) (contract|order|award))"),
    ("development_agreement", r"\b(development (agreement|contract)|joint development|\bnre\b)\b"),
    ("evaluation", r"\b(evaluation|testing|sample|pilot|proof of concept)\b"),
]


@dataclass
class Relation:
    counterparty: str
    category: str
    relation: str | None
    agreement: str | None
    hedged: bool
    strength: float


def counterparties_in(sentence: str) -> list[tuple[str, str]]:
    out = []
    for m in _CP_RE.finditer(sentence):
        alias = m.group(1)
        key = alias.lower()
        if key in _EXACT_CASE and alias not in _EXACT_CASE[key]:
            continue
        canon, cat = COUNTERPARTIES[key]
        if (canon, cat) not in out:
            out.append((canon, cat))
    return out


def classify_relation(sentence: str) -> tuple[str | None, str | None, bool]:
    s = sentence.lower()
    rel = next((name for name, rx in RELATION_RULES if re.search(rx, s)), None)
    agr = next((name for name, rx in AGREEMENT_SENT_RULES if re.search(rx, s)), None)
    return rel, agr, bool(HEDGE_RE.search(sentence))


REL_STRENGTH = {"production_agreement": 1.0, "paid_contract": 1.0, "development_agreement": 0.6, "evaluation": 0.3,
                "loi": 0.2, "mou": 0.15, None: 0.4}


def relations_in(sentence: str) -> list[Relation]:
    cps = counterparties_in(sentence)
    if not cps:
        return []
    rel, agr, hedged = classify_relation(sentence)
    out = []
    for canon, cat in cps:
        strength = REL_STRENGTH.get(agr, 0.4)
        if rel in ("competitor", "supplier"):
            strength = 0.0  # naming a competitor/supplier is not customer validation
        if hedged:
            strength *= 0.3  # "X is a potential customer" != "X signed a five-year supply agreement"
        out.append(Relation(canon, cat, rel, agr, hedged, round(strength, 3)))
    return out


def sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "")
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\"'])", text)
    return [p.strip() for p in parts if 30 <= len(p.strip()) <= 900]


def sentence_hash(s: str) -> str:
    return hashlib.sha1(re.sub(r"\W+", " ", s.lower()).strip().encode()).hexdigest()


def signals_in(sentence: str) -> list[str]:
    s = sentence.lower()
    return [label for label, (rx, _stage) in SIGNALS.items() if re.search(rx, s)]


# ------------------------------------------------------------------ themes
THEMES: dict[str, list[str]] = {
    "AI_INFRASTRUCTURE": ["data center", "data-center", "ai infrastructure", "hyperscale", "gpu", "accelerator", "inference"],
    "SEMICONDUCTOR_MATERIALS": ["wafer", "photoresist", "specialty gas", "semiconductor materials", "substrate", "cmp slurr"],
    "ADVANCED_PACKAGING": ["advanced packaging", "chiplet", "hybrid bonding", "interposer", "co-packaged"],
    "PHOTONICS_OPTICS": ["photonic", "optical interconnect", "laser", "optical transceiver", "silicon photonics", "optics"],
    "NETWORKING": ["networking", "ethernet", "switch", "interconnect", "serdes"],
    "DATA_CENTER_COOLING": ["liquid cooling", "immersion cooling", "thermal management", "cooling"],
    "POWER_SYSTEMS": ["power system", "transformer", "switchgear", "grid", "power electronics", "power conversion", "microgrid"],
    "NUCLEAR": ["nuclear", "reactor", "smr", "small modular reactor", "haleu", "uranium", "enrichment", "fuel fabrication"],
    "ROBOTICS": ["robot", "actuator", "servo", "encoder", "torque sensor", "humanoid", "harmonic drive", "motor"],
    "SENSING_VISION": ["lidar", "machine vision", "image sensor", "radar", "infrared"],
    "PRECISION_COMPONENTS": ["bearing", "precision component", "rare earth magnet", "rare-earth magnet", "permanent magnet"],
    "ADVANCED_MATERIALS": ["advanced material", "specialty metal", "titanium", "tungsten", "gallium", "germanium", "graphene", "silicon carbide", "gallium nitride"],
    "BATTERIES": ["battery", "solid-state", "silicon anode", "lithium", "cathode", "anode", "energy storage"],
    "HYDROGEN": ["hydrogen", "electrolyzer", "fuel cell"],
    "AEROSPACE_DEFENSE": ["aerospace", "defense", "missile", "hypersonic", "munition"],
    "DRONES_EVTOL": ["drone", "unmanned", "uas", "evtol", "air taxi"],
    "AUTONOMY": ["autonomous", "self-driving", "autonomy"],
    "SPACE": ["satellite", "launch vehicle", "spacecraft", "orbit", "space infrastructure", "lunar"],
    "BIOTECH": ["clinical", "therapy", "therapeutic", "oncology", "biologic", "antibody", "gene therapy", "rna", "oral formulation", "glp-1"],
    "MEDTECH": ["medical device", "diagnostic", "surgical", "implant", "imaging system"],
    "INDUSTRIAL_AUTOMATION": ["industrial automation", "factory automation", "motion control", "plc"],
    "ADVANCED_MANUFACTURING": ["additive manufacturing", "3d printing", "advanced manufacturing"],
    "QUANTUM": ["quantum"],
}


def themes_in(text: str) -> list[str]:
    t = (text or "").lower()
    scored = []
    for theme, kws in THEMES.items():
        n = sum(t.count(k) for k in kws)
        if n:
            scored.append((n, theme))
    return [th for n, th in sorted(scored, reverse=True) if n >= 2][:4]


_KNOWN_TERMS = {k for kws in THEMES.values() for k in kws}
_NGRAM_STOP = {"the", "and", "our", "for", "with", "that", "this", "which", "from", "such", "have", "has", "are", "was",
               "were", "will", "its", "their", "into", "other", "these", "those", "company", "products", "product",
               "customers", "customer", "production", "commercial", "supply", "agreement", "capacity", "first"}


def candidate_terms(sentence: str) -> list[str]:
    """2-3 word technical noun phrases from an evidence sentence (for emerging-theme tracking)."""
    words = re.findall(r"[a-z][a-z\-]{2,}", sentence.lower())
    out = []
    for n in (2, 3):
        for i in range(len(words) - n + 1):
            g = words[i:i + n]
            if any(w in _NGRAM_STOP for w in g):
                continue
            term = " ".join(g)
            if any(k in term for k in _KNOWN_TERMS):
                continue
            out.append(term)
    return out


def emerging_terms(recent: list[str], baseline: list[str], min_count: int = 4, top: int = 15) -> list[tuple[str, int, float]]:
    """Terms whose frequency in recent evidence sentences rose sharply vs a baseline period."""
    rc = Counter(t for s in recent for t in set(candidate_terms(s)))
    bc = Counter(t for s in baseline for t in set(candidate_terms(s)))
    scale = max(1, len(baseline)) / max(1, len(recent))
    rows = []
    for term, n in rc.items():
        if n < min_count:
            continue
        lift = n * scale / (bc.get(term, 0) + 1)
        if lift >= 3:
            rows.append((term, n, round(lift, 1)))
    return sorted(rows, key=lambda r: (-r[2], -r[1]))[:top]


# ------------------------------------------------------------------ stage
def classify_stage(signal_labels: list[str], ttm_revenue: float | None, yoy_growth: float | None) -> tuple[str, list[str]]:
    """Evidence-based commercialization stage. Returns (stage, reasons)."""
    reasons = []
    hints = [SIGNALS[l][1] for l in signal_labels if l in SIGNALS and SIGNALS[l][1]]
    text_stage = max(hints, key=STAGES.index) if hints else None
    if text_stage:
        reasons.append(f"text evidence: {text_stage}")
    rev = ttm_revenue or 0.0
    if rev >= 1e9:
        return "SCALED", reasons + [f"TTM revenue {rev / 1e9:.1f}B"]
    if rev < 1e6:
        # without revenue a company cannot be past pilot, whatever the press releases say
        stage = text_stage if text_stage in ("RESEARCH", "PRE-COMMERCIAL", "PILOT", "CUSTOMER_QUALIFICATION") else "PRE-COMMERCIAL"
        if text_stage and text_stage != stage:
            reasons.append(f"capped at {stage}: TTM revenue < $1M")
        return stage, reasons + ["TTM revenue < $1M"]
    if text_stage in ("MASS_PRODUCTION", "PRODUCTION_RAMP"):
        return text_stage, reasons
    if rev >= 250e6 and (yoy_growth or 0) < 0.4:
        return "MASS_PRODUCTION", reasons + ["revenue > $250M"]
    if text_stage in ("EARLY_COMMERCIAL",) or rev >= 1e6:
        return ("PRODUCTION_RAMP" if (yoy_growth or 0) >= 0.5 and rev >= 20e6 else "EARLY_COMMERCIAL"), \
            reasons + [f"TTM revenue {rev / 1e6:.1f}M, YoY {((yoy_growth or 0) * 100):.0f}%"]
    return text_stage or "PRE-COMMERCIAL", reasons
