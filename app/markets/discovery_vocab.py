"""Market-specific early-discovery vocabulary (feature extraction, never a classification by itself).

A theme hit is only a FEATURE with evidence. The shared discovery engine's rule still applies: a HIGH level
needs business evidence (contract, capacity, resource, approval, customer...), so `max_level_from_keywords`
is capped at MEDIUM whatever the vocabulary finds.
"""
from __future__ import annotations

import re

THEMES = {
    "TR": {
        "defense": r"savunma|SSB\b|mühimmat|İHA|insansız|füze|zırhlı|askeri",
        "energy": r"enerji santrali|YEKA|elektrik üretim|doğal ?gaz|rüzgar enerji|JES|jeotermal",
        "battery": r"batarya|akü|enerji depolama|lityum",
        "solar": r"güneş (enerjisi|paneli|santrali)|GES\b|fotovoltaik|solar",
        "chemicals": r"kimya|petrokimya|gübre|boya|polimer",
        "automation": r"otomasyon|robot|endüstri 4\.0",
        "ai_infra": r"yapay zeka|veri merkezi|sunucu|GPU|bulut",
        "mining": r"maden|ruhsat sahası|altın|bakır|bor|nadir toprak|rezerv",
        "biotech": r"ilaç|biyoteknoloji|klinik|ruhsat.*TİTCK|aşı",
        "export": r"ihracat|yurt ?dışı sipariş|ihracat sözleşmesi",
    },
    "CA": {
        "uranium": r"uranium|U3O8", "rare_earth": r"rare earth|NdPr|REE\b", "lithium": r"lithium|spodumene",
        "copper": r"copper|Cu\b", "nickel": r"nickel", "gold": r"\bgold\b|Au\b|g/t Au", "biotech": r"clinical|phase [123]|FDA|Health Canada",
        "cleantech": r"hydrogen|carbon capture|battery|electric vehicle|EV\b|SMR|small modular reactor",
    },
    "AU": {
        "rare_earth": r"rare earth|NdPr|REE\b|ionic clay", "uranium": r"uranium|U3O8", "lithium": r"lithium|spodumene",
        "tungsten": r"tungsten|WO3", "copper": r"copper", "critical_minerals": r"critical mineral|antimony|gallium|germanium|graphite|vanadium|scandium",
        "defense": r"defence|defense|AUKUS", "battery_materials": r"battery|anode|cathode|precursor|pCAM",
    },
}
BUSINESS_EVIDENCE_FAMILIES = {"NEW_CONTRACT", "GOVERNMENT_CONTRACT", "EXPORT_CONTRACT", "CAPACITY_EXPANSION", "NEW_FACTORY",
                              "NEW_PRODUCTION_LINE", "MAJOR_CUSTOMER", "COMMERCIALIZATION", "RESOURCE_UPDATE", "MINING_DISCOVERY",
                              "REGULATORY_APPROVAL", "OFFTAKE_AGREEMENT", "PRODUCTION_START", "FEASIBILITY_STUDY", "GOVERNMENT_SUPPORT"}


def themes(country: str, text: str) -> dict[str, list[str]]:
    out = {}
    for name, rx in THEMES.get(country, {}).items():
        hits = sorted(set(m.group(0).lower() for m in re.finditer(rx, text or "", re.I)))
        if hits:
            out[name] = hits
    return out


def discovery_features(country: str, events: list) -> dict:
    """Theme hits + business-evidence count over a company's canonical events."""
    th: dict[str, set] = {}
    evidence = [e for e in events if e.family in BUSINESS_EVIDENCE_FAMILIES]
    for e in events:
        for k, v in themes(country, f"{e.title} {e.raw.get('text', '')}").items():
            th.setdefault(k, set()).update(v)
    level = "NONE"
    if th:
        level = "LOW"
    if th and evidence:
        level = "MEDIUM"  # keywords + business evidence; HIGH is decided by the shared engine with scale checks
    return {"themes": {k: sorted(v) for k, v in th.items()}, "business_evidence": [e.source_id for e in evidence],
            "max_level_from_keywords": level}
