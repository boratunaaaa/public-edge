"""10-K section extraction, boilerplate-proof sentence fingerprints and semantic change classification.

Only compact fingerprints are stored per filing (normalized sentence hashes + the topic-tagged
sentences), so consecutive 10-Ks can be compared without keeping megabytes of text.

Boilerplate: every sentence is normalized before hashing - numbers, years, dates and dollar amounts
become tokens - so "as of December 31, 2025, we had $41.2 million" and the same sentence with 2024 /
$38.9 million are the SAME sentence. A yearly roll-forward therefore produces no change; only
genuinely new or removed statements count, and a change type needs a minimum number of them.
"""
from __future__ import annotations

import hashlib
import html as htmllib
import re
from dataclasses import dataclass, field

# ------------------------------------------------------------------ items

# Headings only count at the START of a line: prose cross-references ("see Item 7. Management's
# Discussion ...", "Item 1A of this report") must not cut a section short.
_LS = r"(?im)^[ \t]*(?:part\s+i{1,2}\s*[,.\-–—]?\s*)?"
ITEM_PATTERNS = {
    "BUSINESS": _LS + r"item\s*1\s*[.:\-–—]?\s*business\b",
    "RISK_FACTORS": _LS + r"item\s*1a\s*[.:\-–—]?\s*risk\s+factors",
    "CYBERSECURITY": _LS + r"item\s*1c\s*[.:\-–—]?\s*cybersecurity",
    "LEGAL_PROCEEDINGS": _LS + r"item\s*3\s*[.:\-–—]?\s*legal\s+proceedings",
    "MD&A": _LS + r"item\s*7\s*[.:\-–—]?\s*management['’`s]*\s+discussion",
}
BOUNDARY = re.compile(_LS + r"item\s*(1a|1b|1c|1|2|3|4|5|6|7a|7|8|9a|9b|9)\s*[.:\-–—]?\s*[A-Za-z]")
MIN_SECTION_CHARS = 400


def html_to_text(raw: str) -> str:
    s = re.sub(r"(?is)<ix:header>.*?</ix:header>", " ", raw)
    s = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    return re.sub(r"[ \t\xa0]+", " ", s)


def extract_items(text: str) -> dict[str, str]:
    """{item: text}. The table of contents repeats every heading, so for each item the occurrence
    followed by the LONGEST span up to the next item heading is taken as the real section."""
    bounds = sorted(m.start() for m in BOUNDARY.finditer(text))
    out: dict[str, str] = {}
    for name, pat in ITEM_PATTERNS.items():
        best = ""
        for m in re.finditer(pat, text):
            nxt = next((b for b in bounds if b > m.end() + 20), len(text))
            span = text[m.end():nxt]
            if len(span) > len(best):
                best = span
        if len(best) >= MIN_SECTION_CHARS:
            out[name] = best
    return out


# ------------------------------------------------------------------ sentences

def sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "")
    parts = re.split(r"(?<=[.!?;])\s+(?=[A-Z(\"'•])", text)
    return [p.strip() for p in parts if len(p.split()) >= 8 and len(p) <= 1500]


NUM_RE = re.compile(r"\$?\(?\d[\d,]*(\.\d+)?\)?\s*(%|percent|million|billion|thousand)?")
MONTH_RE = re.compile(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b")


def normalize(s: str) -> str:
    s = s.lower()
    s = MONTH_RE.sub(" <m> ", s)
    s = NUM_RE.sub(" <n> ", s)
    s = re.sub(r"\b(fiscal|calendar)\s+<n>", " <y> ", s)
    s = re.sub(r"[^a-z<> ]+", " ", s)
    return " ".join(s.split())


def signature(s: str, k: int = 8) -> list[int]:
    """MinHash over word bigrams: a lightly edited sentence keeps most of its signature."""
    w = normalize(s).split()
    grams = {" ".join(w[i:i + 2]) for i in range(max(1, len(w) - 1))}
    return [min(int.from_bytes(hashlib.blake2b(g.encode(), digest_size=4, salt=bytes([j])).digest(), "big") for g in grams)
            for j in range(k)]


def similar(a: list[int] | None, b: list[int] | None, need: int = 5) -> bool:
    return bool(a and b) and sum(x == y for x, y in zip(a, b)) >= need


def fingerprint(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(normalize(s).encode(), digest_size=8).digest(), "big") >> 1


# ------------------------------------------------------------------ topics

TOPICS: dict[str, re.Pattern] = {
    "GOING_CONCERN": re.compile(r"going concern|substantial doubt", re.I),
    "LIQUIDITY": re.compile(r"\bliquidity\b|capital resources|working capital|cash (and cash equivalents )?(will|would|may) (be|not be) sufficient|"
                            r"fund (our )?operations", re.I),
    "CAPITAL_REQUIREMENTS": re.compile(r"(raise|obtain|need|require)\w* (substantial )?additional (capital|financing|funds)", re.I),
    "COMPETITION": re.compile(r"\bcompet(e|ition|itors?|itive)\b", re.I),
    "SUPPLY_CHAIN": re.compile(r"supply chain|sole[- ]source|single[- ]source|\bsuppliers?\b|raw materials?|component shortages?", re.I),
    "CUSTOMER_CONCENTRATION": re.compile(r"(one|two|three|a (small|limited) number of|few) (of our )?customers?.{0,80}(accounted|represent|comprised)|"
                                         r"(largest|significant|major) customers?.{0,80}\d+ ?%|\d+ ?% of (our )?(total |net )?(revenue|sales)", re.I),
    "CYBERSECURITY": re.compile(r"cyber|ransomware|data breach|security incident", re.I),
    "LEGAL": re.compile(r"litigation|lawsuit|legal proceedings?|class action|complaint (was )?filed|subpoena|indictment", re.I),
    "REGULATORY": re.compile(r"\bregulat\w+|\bfda\b|\bftc\b|\bsec\b investigation|export control|tariff|sanction", re.I),
    "CRITICAL_ACCOUNTING": re.compile(r"critical accounting|significant (judgments?|estimates)", re.I),
    "CAPACITY": re.compile(r"(expand|increas|add)\w* (our )?(manufacturing|production) capacity|new (manufacturing|production) (facility|line|plant)", re.I),
    "COMMERCIALIZATION": re.compile(r"commercial (launch|production|sales|shipments)|(began|commenced|started) (selling|shipping|commercial)|"
                                    r"first commercial", re.I),
    "MAJOR_CUSTOMER": re.compile(r"(agreement|contract|purchase order|selected|awarded).{0,60}(u\.s\. (army|navy|air force)|department of "
                                 r"(defense|war|energy)|nasa|apple|microsoft|amazon|google|nvidia|tesla|general motors|ford|lockheed|"
                                 r"boeing|northrop|raytheon|pfizer|merck)", re.I),
}
NEGATIVE = re.compile(r"not (be )?sufficient|insufficient|may not be able|unable to|substantial doubt|default|breach|deficit|"
                      r"going concern|material weakness|delist", re.I)
SUFFICIENT = re.compile(r"(sufficient|adequate) to (fund|meet|satisfy).{0,120}(at least|next) (the )?(next )?(<n>|twelve|12) months", re.I)


def topics_of(sentence: str) -> list[str]:
    return [t for t, rx in TOPICS.items() if rx.search(sentence)]


# ------------------------------------------------------------------ profiles + diff

def build_profile(items: dict[str, str]) -> dict:
    """{section: {"n": sentence count, "hashes": [...], "tagged": {hash: [topics, negative, sufficient]},
    "text_hash": sha1 of the normalized section}}"""
    prof = {}
    for sec, txt in items.items():
        sents = sentences(txt)
        hashes, tagged = [], {}
        for s in sents:
            h = fingerprint(s)
            hashes.append(h)
            tp = topics_of(s)
            if tp:
                tagged[str(h)] = [tp, bool(NEGATIVE.search(s)), bool(SUFFICIENT.search(normalize(s))), signature(s)]
        prof[sec] = {"n": len(sents), "hashes": sorted(set(hashes)),
                     "tagged": tagged, "text_hash": hashlib.sha1(normalize(txt).encode()).hexdigest()}
    return prof


@dataclass
class Change:
    section: str
    change_type: str
    materiality: str  # HIGH | MEDIUM | LOW
    n_new: int = 0
    n_removed: int = 0
    old_text_hash: str | None = None
    new_text_hash: str | None = None
    examples: list[str] = field(default_factory=list)


MIN_TOPIC_SENTENCES = 2
TOPIC_SHIFT_SHARE = 0.25
RISK_SHIFT_MIN = 10
RISK_SHIFT_SHARE = 0.15


def compare(old: dict, new: dict, new_texts: dict[str, str] | None = None) -> list[Change]:
    """Semantic changes between two 10-K fingerprints (old = previous annual report)."""
    new_texts = new_texts or {}
    out: list[Change] = []

    def tagged_set(prof: dict, sec: str | None, topic: str, negative: bool | None = None, sufficient: bool | None = None) -> set[str]:
        res = set()
        for s, p in prof.items():
            if sec and s != sec:
                continue
            for h, (tp, neg, suf, *_) in p.get("tagged", {}).items():
                if topic in tp and (negative is None or neg == negative) and (sufficient is None or suf == sufficient):
                    res.add(h)
        return res

    def all_hashes(prof: dict) -> set[str]:
        return {str(h) for p in prof.values() for h in p.get("hashes", [])}

    old_all, new_all = all_hashes(old), all_hashes(new)

    def examples(hs: set[str]) -> list[str]:
        want = {int(h) for h in hs}
        ex = []
        for txt in new_texts.values():
            for s in sentences(txt):
                if fingerprint(s) in want:
                    ex.append(s[:300])
                    if len(ex) >= 3:
                        return ex
        return ex

    def th(sec: str) -> tuple[str | None, str | None]:
        return (old.get(sec) or {}).get("text_hash"), (new.get(sec) or {}).get("text_hash")

    # going concern: presence flips are always material
    gc_old, gc_new = tagged_set(old, None, "GOING_CONCERN"), tagged_set(new, None, "GOING_CONCERN")
    if gc_new and not gc_old:
        out.append(Change("GOING_CONCERN", "NEW_GOING_CONCERN", "HIGH", len(gc_new), 0, None, None, examples(gc_new)))
    elif gc_old and not gc_new:
        out.append(Change("GOING_CONCERN", "REMOVED_GOING_CONCERN", "HIGH", 0, len(gc_old)))

    # overall risk-factor shift (count of genuinely new vs removed risk statements)
    if "RISK_FACTORS" in old and "RISK_FACTORS" in new:
        o, n = {str(h) for h in old["RISK_FACTORS"]["hashes"]}, {str(h) for h in new["RISK_FACTORS"]["hashes"]}
        added, removed = n - o, o - n
        base = max(1, len(o))
        net = len(added) - len(removed)
        if net >= max(RISK_SHIFT_MIN, RISK_SHIFT_SHARE * base):
            mat = "HIGH" if net >= 0.3 * base else "MEDIUM"
            out.append(Change("RISK_FACTORS", "RISK_INCREASE", mat, len(added), len(removed), *th("RISK_FACTORS")))
        elif -net >= max(RISK_SHIFT_MIN, RISK_SHIFT_SHARE * base):
            mat = "HIGH" if -net >= 0.3 * base else "MEDIUM"
            out.append(Change("RISK_FACTORS", "RISK_DECREASE", mat, len(added), len(removed), *th("RISK_FACTORS")))

    # topic-level additions / removals
    def sigs(prof: dict) -> dict[str, list[int]]:
        return {h: v[3] for p in prof.values() for h, v in p.get("tagged", {}).items() if len(v) > 3}

    old_sig, new_sig = sigs(old), sigs(new)

    def drop_edits(added: set[str], gone: set[str]) -> tuple[set[str], set[str]]:
        """A reworded sentence is an edit, not a new statement: pair it with a similar removed one."""
        gone_left = set(gone)
        real_added = set()
        for h in added:
            match = next((g for g in gone_left if similar(new_sig.get(h), old_sig.get(g))), None)
            if match is None:
                real_added.add(h)
            else:
                gone_left.discard(match)
        return real_added, gone_left

    def topic_change(topic: str, change_type: str, section: str, *, negative: bool | None = None, sufficient: bool | None = None,
                     removed_type: str | None = None, high_at: int = 4) -> None:
        o = tagged_set(old, None, topic, negative, sufficient)
        n = tagged_set(new, None, topic, negative, sufficient)
        added = {h for h in n - o if h not in old_all}  # truly new statements, not moved ones
        gone = {h for h in o - n if h not in new_all}
        added, gone = drop_edits(added, gone)
        net = len(added) - len(gone)
        # a change must be a NET shift that is large relative to how much the report already said
        if net >= max(MIN_TOPIC_SENTENCES, TOPIC_SHIFT_SHARE * len(o)):
            high = net >= high_at and net >= 0.5 * max(1, len(o))
            out.append(Change(section, change_type, "HIGH" if high else "MEDIUM", len(added), len(gone),
                              *th(section), examples(added)))
        elif removed_type and -net >= max(MIN_TOPIC_SENTENCES, TOPIC_SHIFT_SHARE * len(o)):
            out.append(Change(section, removed_type, "MEDIUM", len(added), len(gone), *th(section)))

    topic_change("LIQUIDITY", "LIQUIDITY_DETERIORATION", "MD&A", negative=True, removed_type="LIQUIDITY_IMPROVEMENT")
    suf_old = tagged_set(old, None, "LIQUIDITY", sufficient=True)
    suf_new = tagged_set(new, None, "LIQUIDITY", sufficient=True)
    if suf_new and not suf_old and not any(c.change_type == "LIQUIDITY_DETERIORATION" for c in out):
        out.append(Change("MD&A", "LIQUIDITY_IMPROVEMENT", "MEDIUM", len(suf_new), 0, *th("MD&A"), examples(suf_new)))
    topic_change("CUSTOMER_CONCENTRATION", "NEW_CUSTOMER_CONCENTRATION", "BUSINESS")
    topic_change("SUPPLY_CHAIN", "NEW_SUPPLY_RISK", "RISK_FACTORS", negative=None, high_at=8)
    topic_change("LEGAL", "NEW_LEGAL_RISK", "LEGAL_PROCEEDINGS", high_at=6)
    topic_change("REGULATORY", "NEW_REGULATORY_RISK", "RISK_FACTORS", high_at=8)
    topic_change("CAPACITY", "NEW_CAPACITY_EXPANSION", "BUSINESS", high_at=3)
    topic_change("COMMERCIALIZATION", "NEW_COMMERCIALIZATION_LANGUAGE", "BUSINESS", high_at=3)
    topic_change("MAJOR_CUSTOMER", "NEW_MAJOR_CUSTOMER", "BUSINESS", high_at=3)

    # a near-total rewrite of MD&A that none of the rules explains
    if "MD&A" in old and "MD&A" in new and not out:
        o, n = {str(h) for h in old["MD&A"]["hashes"]}, {str(h) for h in new["MD&A"]["hashes"]}
        if o and len(n - o) / max(1, len(n)) >= 0.6:
            out.append(Change("MD&A", "OTHER_MATERIAL_CHANGE", "LOW", len(n - o), len(o - n), *th("MD&A")))
    return out
